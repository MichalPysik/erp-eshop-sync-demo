"""Pydantic models for ERP data validation and e-shop payload construction."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.conf import settings
from pydantic import BaseModel, Field, field_validator

VAT_PERCENT = getattr(settings, "VAT_PERCENT", False)
VAT_MULTIPLIER = 1.0 + (VAT_PERCENT / 100.0)


class ERPProduct(BaseModel):
    """Raw product as it comes from erp_data.json – used for validation only."""

    id: str
    title: str
    price_vat_excl: float = None
    stocks: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] | None = None

    @field_validator("price_vat_excl")
    @classmethod
    def price_must_be_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("price_vat_excl must be non-negative")
        return v


class EshopProduct(BaseModel):
    """Transformed product ready to be sent to the e-shop API."""

    sku: str
    title: str
    price_vat_incl: float
    stock_total: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    active: bool = True

    @classmethod
    def from_erp(cls, erp: ERPProduct, active: bool = True) -> "EshopProduct":
        price_vat_incl = round(erp.price_vat_excl * VAT_MULTIPLIER, 2)

        stock_total = 0
        for val in erp.stocks.values():
            if isinstance(val, (int, float)):
                stock_total += int(val)

        raw_attrs = erp.attributes or {}
        # Carry all attributes through; ensure color always has a value
        attrs: dict[str, Any] = {**raw_attrs}
        attrs["color"] = attrs.get("color") or "N/A"

        return cls(
            sku=erp.id,
            title=erp.title,
            price_vat_incl=price_vat_incl,
            stock_total=stock_total,
            attributes=attrs,
            active=active,
        )

    def compute_hash(self) -> str:
        """Deterministic hash of the payload for delta-sync comparison (includes `active` field)."""
        payload = self.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def api_payload(self) -> dict:
        """Dict to send to the e-shop API."""
        return self.model_dump(mode="json")
