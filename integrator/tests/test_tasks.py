"""Tests for the sync task: parse/validate, delta sync, API mocking, 429 retry, deactivation."""

import json
import tempfile

import pytest
import responses

from integrator.models import SyncedProduct, SyncStatus
from integrator.schemas import ERPProduct, EshopProduct
from integrator.tasks import (ESHOP_BASE_URL, load_erp_data,
                              parse_and_validate, send_to_eshop, sync_products)

pytestmark = pytest.mark.django_db

PRODUCTS_URL = f"{ESHOP_BASE_URL}/products/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_erp_file(data: list[dict]) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


def _mock_eshop_post(status=201):
    responses.add(responses.POST, PRODUCTS_URL, json={"ok": True}, status=status)


def _mock_eshop_patch(sku, status=200):
    responses.add(
        responses.PATCH, f"{PRODUCTS_URL}{sku}/", json={"ok": True}, status=status
    )


VALID_ERP = [
    {
        "id": "SKU-001",
        "title": "Kávovar",
        "price_vat_excl": 12400.5,
        "stocks": {"a": 5, "b": 3},
        "attributes": {"color": "stříbrná"},
    },
    {
        "id": "SKU-003",
        "title": "Mlýnek",
        "price_vat_excl": 1500,
        "stocks": {"x": 50},
        "attributes": None,
    },
]


# ---------------------------------------------------------------------------
# parse_and_validate
# ---------------------------------------------------------------------------


class TestParseAndValidate:
    def test_skips_negative_price(self):
        data = [
            {
                "id": "BAD",
                "title": "X",
                "price_vat_excl": -10,
                "stocks": {},
                "attributes": {},
            }
        ]
        assert parse_and_validate(data) == []

    def test_skips_null_price(self):
        data = [
            {
                "id": "X",
                "title": "X",
                "price_vat_excl": None,
                "stocks": {},
                "attributes": {},
            }
        ]
        assert parse_and_validate(data) == []

    def test_uses_last_duplicate(self):
        item_a = {
            "id": "SKU-006",
            "title": "Tablety",
            "price_vat_excl": 250,
            "stocks": {"a": 100},
            "attributes": {},
        }
        item_b = {
            "id": "SKU-006",
            "title": "Lepsi_Tablety",
            "price_vat_excl": 250,
            "stocks": {"a": 100},
            "attributes": {},
        }
        item_c = {
            "id": "SKU-006",
            "title": "Nejlepsi_Tablety",
            "price_vat_excl": 260,
            "stocks": {"a": 100},
            "attributes": {},
        }
        result = parse_and_validate([item_a, item_b, item_c])
        assert len(result) == 1
        assert result[0].id == "SKU-006"
        assert result[0].price_vat_excl == 260

    def test_valid_items_pass(self):
        result = parse_and_validate(VALID_ERP)
        assert len(result) == 2
        assert result[0].id == "SKU-001"


# ---------------------------------------------------------------------------
# Full sync task (with mocked API)
# ---------------------------------------------------------------------------


class TestSyncProducts:
    @responses.activate
    def test_first_sync_creates_products(self):
        path = _write_erp_file(VALID_ERP)
        _mock_eshop_post(201)
        _mock_eshop_post(201)

        stats = sync_products(erp_path=path)

        assert stats["created"] == 2
        assert stats["failed"] == 0
        assert SyncedProduct.objects.count() == 2
        for p in SyncedProduct.objects.all():
            assert p.status == SyncStatus.SUCCESS
            assert p.active is True
            assert p.last_hash != ""

    @responses.activate
    def test_second_sync_unchanged(self):
        """Running sync twice with the same data should yield 'unchanged' on the second run."""
        path = _write_erp_file(VALID_ERP)
        # First run
        _mock_eshop_post(201)
        _mock_eshop_post(201)
        sync_products(erp_path=path)

        # Second run – no API calls expected
        stats = sync_products(erp_path=path)
        assert stats["unchanged"] == 2
        assert stats["created"] == 0
        assert stats["updated"] == 0

    @responses.activate
    def test_delta_sync_detects_change(self):
        """If ERP data changes, the product should be updated (PATCH)."""
        path1 = _write_erp_file(VALID_ERP)
        _mock_eshop_post(201)
        _mock_eshop_post(201)
        sync_products(erp_path=path1)

        # Change the price of SKU-001
        modified = [dict(VALID_ERP[0], price_vat_excl=9999), VALID_ERP[1]]
        path2 = _write_erp_file(modified)
        _mock_eshop_patch("SKU-001", 200)

        stats = sync_products(erp_path=path2)
        assert stats["updated"] == 1
        assert stats["unchanged"] == 1

    @responses.activate
    def test_deactivation_when_product_disappears(self):
        """Products removed from ERP should be deactivated via PATCH with active=False."""
        path_full = _write_erp_file(VALID_ERP)
        _mock_eshop_post(201)
        _mock_eshop_post(201)
        sync_products(erp_path=path_full)
        assert SyncedProduct.objects.count() == 2

        # Now ERP only has SKU-001
        path_partial = _write_erp_file([VALID_ERP[0]])
        _mock_eshop_patch("SKU-003", 200)  # deactivation PATCH

        stats = sync_products(erp_path=path_partial)
        assert stats["deactivated"] == 1

        deactivated = SyncedProduct.objects.get(sku="SKU-003")
        assert deactivated.active is False
        assert deactivated.status == SyncStatus.SUCCESS

    @responses.activate
    def test_api_failure_sets_failed_status(self):
        path = _write_erp_file([VALID_ERP[0]])
        responses.add(responses.POST, PRODUCTS_URL, json={"error": "boom"}, status=500)

        stats = sync_products(erp_path=path)
        assert stats["failed"] == 1
        p = SyncedProduct.objects.get(sku="SKU-001")
        assert p.status == SyncStatus.FAILED

    @responses.activate
    def test_failed_product_retried_on_next_sync(self):
        """A previously FAILED product should be retried even if data hasn't changed."""
        path = _write_erp_file([VALID_ERP[0]])
        # First sync fails
        responses.add(responses.POST, PRODUCTS_URL, json={"error": "boom"}, status=500)
        sync_products(erp_path=path)
        assert SyncedProduct.objects.get(sku="SKU-001").status == SyncStatus.FAILED

        # Second sync succeeds
        responses.add(responses.POST, PRODUCTS_URL, json={"ok": True}, status=201)
        stats = sync_products(erp_path=path)
        assert stats["created"] == 1
        assert SyncedProduct.objects.get(sku="SKU-001").status == SyncStatus.SUCCESS


# ---------------------------------------------------------------------------
# 429 rate-limit retry
# ---------------------------------------------------------------------------


class TestRateLimitRetry:
    @responses.activate
    def test_429_retry_then_success(self):
        """send_to_eshop should retry on 429 and eventually succeed."""
        erp = ERPProduct(id="SKU-001", title="T", price_vat_excl=100, stocks={"a": 1})
        product = EshopProduct.from_erp(erp)

        # Two 429s then success
        responses.add(responses.POST, PRODUCTS_URL, status=429)
        responses.add(responses.POST, PRODUCTS_URL, status=429)
        responses.add(responses.POST, PRODUCTS_URL, json={"ok": True}, status=201)

        resp = send_to_eshop(product, exists_in_eshop=False)
        assert resp.status_code == 201
        assert len(responses.calls) == 3

    @responses.activate
    def test_429_exhausted_returns_last_response(self):
        """If all retries get 429, the last 429 response is returned."""
        erp = ERPProduct(id="SKU-001", title="T", price_vat_excl=100, stocks={"a": 1})
        product = EshopProduct.from_erp(erp)

        for _ in range(5):
            responses.add(responses.POST, PRODUCTS_URL, status=429)

        resp = send_to_eshop(product, exists_in_eshop=False)
        assert resp.status_code == 429

    @responses.activate
    def test_patch_uses_correct_url(self):
        erp = ERPProduct(id="SKU-001", title="T", price_vat_excl=100, stocks={"a": 1})
        product = EshopProduct.from_erp(erp)
        responses.add(
            responses.PATCH, f"{PRODUCTS_URL}SKU-001/", json={"ok": True}, status=200
        )

        resp = send_to_eshop(product, exists_in_eshop=True)
        assert resp.status_code == 200
        assert "SKU-001" in responses.calls[0].request.url


# ---------------------------------------------------------------------------
# load_erp_data
# ---------------------------------------------------------------------------


class TestLoadErpData:
    def test_loads_json_file(self):
        data = [
            {
                "id": "X",
                "title": "Y",
                "price_vat_excl": 1,
                "stocks": {},
                "attributes": {},
            }
        ]
        path = _write_erp_file(data)
        loaded = load_erp_data(path)
        assert loaded == data
