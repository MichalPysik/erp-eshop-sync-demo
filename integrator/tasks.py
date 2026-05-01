"""Celery tasks for ERP → e-shop synchronization."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from integrator.models import SyncedProduct, SyncStatus
from integrator.schemas import ERPProduct, EshopProduct

logger = logging.getLogger(__name__)

ESHOP_BASE_URL = getattr(settings, "ESHOP_API_BASE_URL", "https://api.fake-eshop.cz/v1")
ESHOP_API_KEY = getattr(
    settings, "ESHOP_API_KEY", "symma-secret-token"
)  # Again, the api key should not be hardcoded here
RATE_LIMIT = getattr(settings, "ESHOP_API_RATE_LIMIT", 5)
MOCK_ESHOP = getattr(settings, "MOCK_ESHOP", False)

MAX_RETRIES_429 = 5
RETRY_BACKOFF = 1.0  # seconds to wait after first 429 (then exponential backoff)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_erp_data(path: str | None = None) -> list[dict[str, Any]]:
    """Read erp_data.json from disk."""
    path = path or str(settings.ERP_DATA_FILE)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_and_validate(raw_items: list[dict]) -> list[ERPProduct]:
    """
    Parse raw JSON dicts into validated ERPProduct models.

    Internally deduplicates by product id (SKU) using a dict.
    Returns a list of ERPProduct.
    """
    products: dict[str, ERPProduct] = {}

    for item in raw_items:
        try:
            erp = ERPProduct.model_validate(item)
        except Exception as exc:
            logger.warning(f"Skipping invalid ERP item {item.get('id', '?')}: {exc}")
            continue

        if erp.id in products:
            logger.info(f"Replacing duplicate SKU {erp.id}")

        products[erp.id] = erp

    return list(products.values())


def transform(erp_products: list[ERPProduct]) -> list[EshopProduct]:
    """Apply business transformations (VAT, stock sum, default color)."""
    return [EshopProduct.from_erp(p) for p in erp_products]


def _api_headers() -> dict[str, str]:
    return {
        "X-Api-Key": ESHOP_API_KEY,
        "Content-Type": "application/json",
    }


def send_to_eshop(product: EshopProduct, exists_in_eshop: bool) -> requests.Response:
    """
    POST (new) or PATCH (existing) a single product to the e-shop API.
    Handles 429 rate-limit responses with exponential back-off.
    """
    url = f"{ESHOP_BASE_URL}/products/"
    if exists_in_eshop:
        url = f"{ESHOP_BASE_URL}/products/{product.sku}/"

    method = "PATCH" if exists_in_eshop else "POST"

    # Mocked responses for easier manual testing
    if MOCK_ESHOP:
        response = requests.Response()
        response.status_code = 200 if exists_in_eshop else 201
        response._content = b'{"ok": true}'
        logger.info(
            f"Mock Eshop received product for {'creation' if not exists_in_eshop else 'update'}: {product}"
        )
        return response

    for attempt in range(1, MAX_RETRIES_429 + 1):
        resp = requests.request(
            method,
            url,
            json=product.api_payload(),
            headers=_api_headers(),
            timeout=10,
        )
        if resp.status_code != 429:
            return resp

        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = RETRY_BACKOFF * attempt
        else:
            wait = RETRY_BACKOFF * attempt
        logger.warning(
            f"429 rate-limited on {product.sku} (attempt {attempt}), sleeping {wait:.1f}s"
        )
        if attempt < MAX_RETRIES_429:
            time.sleep(wait)

    # All retries exhausted – return last response
    return resp


# ---------------------------------------------------------------------------
# Rate-limiter (token-bucket style, simple)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple token-bucket rate limiter: max `rate` calls per second."""

    def __init__(self, rate: int = RATE_LIMIT):
        self.rate = rate
        self.tokens = rate
        self.last = time.monotonic()

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens < 1:
            sleep_time = (1 - self.tokens) / self.rate
            time.sleep(sleep_time)
            self.tokens = 0
            self.last = time.monotonic()
        else:
            self.tokens -= 1


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------


@shared_task(name="integrator.sync_products")
def sync_products(erp_path: str | None = None) -> dict:
    """
    Full synchronization cycle:
    1. Load ERP data from disk.
    2. Validate & transform.
    3. Deactivate products no longer present in ERP.
    4. Delta-sync – only push changed products to the e-shop.
    5. Respect rate-limit (5 req/s).
    """
    logger.info("=== sync_products START ===")

    # 1. Load & parse
    raw = load_erp_data(erp_path)
    erp_products = parse_and_validate(raw)
    eshop_products = transform(erp_products)
    erp_skus = {p.sku for p in eshop_products}

    stats = {"created": 0, "updated": 0, "unchanged": 0, "deactivated": 0, "failed": 0}
    limiter = RateLimiter()

    # 2. Mark products no longer in ERP as inactive
    disappeared = SyncedProduct.objects.filter(active=True).exclude(sku__in=erp_skus)
    for db_prod in disappeared:
        stored = db_prod.payload or {}
        deactivation_product = EshopProduct(
            sku=db_prod.sku,
            title=stored.get("title", "Unknown"),
            price_vat_incl=stored.get("price_vat_incl", 0),
            stock_total=stored.get("stock_total", 0),
            attributes=stored.get("attributes", {"color": "N/A"}),
            active=False,
        )
        db_prod.payload = deactivation_product.api_payload()
        db_prod.fetched_at = timezone.now()
        db_prod.save()
        limiter.wait()
        try:
            # Edge case: if db_prod.last_hash == "", we send archived (inactive) product to Eshop,
            # which encountered eshop communication error when it was about to get created there
            resp = send_to_eshop(
                deactivation_product, exists_in_eshop=(db_prod.last_hash != "")
            )
            if resp.status_code in (200, 201, 202, 204):
                db_prod.active = False
                db_prod.status = SyncStatus.SUCCESS
                db_prod.synced_at = timezone.now()
                db_prod.last_hash = deactivation_product.compute_hash()
                db_prod.save()
                stats["deactivated"] += 1
            else:
                db_prod.status = SyncStatus.FAILED
                db_prod.save()
                stats["failed"] += 1
                logger.error(f"Failed to deactivate {db_prod.sku}: {resp.status_code}")
        except Exception as exc:
            db_prod.status = SyncStatus.FAILED
            db_prod.save()
            stats["failed"] += 1
            logger.exception(f"Error deactivating {db_prod.sku}: {exc}")

    # 3. Upsert active products (delta sync)
    for product in eshop_products:
        new_hash = product.compute_hash()

        db_prod, created = SyncedProduct.objects.get_or_create(
            sku=product.sku,
            defaults={
                "last_hash": "",
                "status": SyncStatus.PENDING,
                "active": True,
                "payload": {},
            },
        )

        if (
            not created
            and db_prod.last_hash == new_hash
            and db_prod.status == SyncStatus.SUCCESS
        ):
            stats["unchanged"] += 1
            continue

        # Mark as pending before attempting to send
        db_prod.status = SyncStatus.PENDING
        db_prod.active = True
        db_prod.payload = product.api_payload()
        db_prod.fetched_at = timezone.now()
        db_prod.save()

        exists_in_eshop = not created and db_prod.last_hash != ""

        limiter.wait()
        try:
            resp = send_to_eshop(product, exists_in_eshop=exists_in_eshop)
            if resp.status_code in (200, 201, 202, 204):
                db_prod.last_hash = new_hash
                db_prod.synced_at = timezone.now()
                db_prod.status = SyncStatus.SUCCESS
                db_prod.save()
                stats["created" if not exists_in_eshop else "updated"] += 1
            else:
                db_prod.status = SyncStatus.FAILED
                db_prod.save()
                stats["failed"] += 1
                logger.error(
                    f"E-shop API error for {product.sku}: HTTP {resp.status_code} – {resp.text}"
                )
        except Exception as exc:
            db_prod.status = SyncStatus.FAILED
            db_prod.save()
            stats["failed"] += 1
            logger.exception(f"Error for {product.sku}: {exc}")

    logger.info(f"=== sync_products END === stats={stats}")
    return stats
