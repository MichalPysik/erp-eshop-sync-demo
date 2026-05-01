"""Tests for Pydantic validation and transformation logic."""

import pytest
from pydantic import ValidationError

from integrator.schemas import ERPProduct, EshopProduct


# ---------------------------------------------------------------------------
# ERPProduct validation
# ---------------------------------------------------------------------------

class TestERPProductValidation:
    def test_valid_product(self):
        p = ERPProduct(id="SKU-001", title="Test", price_vat_excl=100.0, stocks={"a": 1})
        assert p.id == "SKU-001"
        assert p.price_vat_excl == 100.0

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            ERPProduct(id="SKU-002", title="Bad", price_vat_excl=-10.0, stocks={})

    def test_null_price_not_allowed(self):
        with pytest.raises(ValidationError):
            _ = ERPProduct(id="SKU-004", title="NullPrice", price_vat_excl=None, stocks={})

    def test_null_attributes_allowed(self):
        p = ERPProduct(id="SKU-003", title="NoAttr", price_vat_excl=100, attributes=None)
        assert p.attributes is None


# ---------------------------------------------------------------------------
# EshopProduct transformation
# ---------------------------------------------------------------------------

class TestEshopProductTransformation:
    def test_vat_calculation(self):
        erp = ERPProduct(id="SKU-001", title="Item", price_vat_excl=100.0, stocks={})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.price_vat_incl == 121.0

    def test_vat_rounding(self):
        erp = ERPProduct(id="SKU-001", title="Item", price_vat_excl=12400.5, stocks={})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.price_vat_incl == round(12400.5 * 1.21, 2)

    def test_stock_summation(self):
        erp = ERPProduct(id="SKU-001", title="Item", price_vat_excl=100, stocks={"a": 5, "b": 3})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.stock_total == 8

    def test_stock_ignores_non_numeric(self):
        erp = ERPProduct(id="SKU-008", title="Filtry", price_vat_excl=300, stocks={"a": "N/A"})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.stock_total == 0

    def test_stock_mixed_numeric_and_non_numeric(self):
        erp = ERPProduct(id="X", title="X", price_vat_excl=10, stocks={"a": 5, "b": "N/A", "c": 3})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.stock_total == 8

    def test_color_default_when_missing(self):
        erp = ERPProduct(id="SKU-006", title="Tablety", price_vat_excl=250, attributes={})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.color == "N/A"

    def test_color_default_when_null_attributes(self):
        erp = ERPProduct(id="SKU-003", title="Mlýnek", price_vat_excl=1500, attributes=None)
        eshop = EshopProduct.from_erp(erp)
        assert eshop.color == "N/A"

    def test_color_preserved_when_present(self):
        erp = ERPProduct(id="SKU-001", title="Kávovar", price_vat_excl=100, attributes={"color": "stříbrná"})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.color == "stříbrná"

    def test_active_flag_default_true(self):
        erp = ERPProduct(id="X", title="X", price_vat_excl=10, stocks={})
        eshop = EshopProduct.from_erp(erp)
        assert eshop.active is True

    def test_active_flag_can_be_false(self):
        erp = ERPProduct(id="X", title="X", price_vat_excl=10, stocks={})
        eshop = EshopProduct.from_erp(erp, active=False)
        assert eshop.active is False

    def test_compute_hash_deterministic(self):
        erp = ERPProduct(id="X", title="X", price_vat_excl=10, stocks={"a": 1})
        p1 = EshopProduct.from_erp(erp)
        p2 = EshopProduct.from_erp(erp)
        assert p1.compute_hash() == p2.compute_hash()

    def test_compute_hash_changes_on_different_data(self):
        e1 = ERPProduct(id="X", title="X", price_vat_excl=10, stocks={"a": 1})
        e2 = ERPProduct(id="X", title="X", price_vat_excl=20, stocks={"a": 1})
        assert EshopProduct.from_erp(e1).compute_hash() != EshopProduct.from_erp(e2).compute_hash()

    def test_api_payload_contains_all_fields(self):
        erp = ERPProduct(id="SKU-001", title="T", price_vat_excl=100, stocks={"a": 2}, attributes={"color": "red"})
        payload = EshopProduct.from_erp(erp).api_payload()
        assert set(payload.keys()) == {"sku", "title", "price_vat_incl", "stock_total", "color", "active"}
        assert payload["sku"] == "SKU-001"
        assert payload["active"] is True
