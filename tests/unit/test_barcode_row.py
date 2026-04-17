"""Unit tests for BarcodeScannedRow field derivation."""

import pytest

from kintree.gui.views.barcode import BarcodeScannedRow


def _make_parsed(supplier='digikey', supplier_pn='ABC-ND', manufacturer_pn='MPN123',
                 quantity=10, barcode='MPN123', **extra):
    d = {
        'supplier': supplier,
        'supplier_pn': supplier_pn,
        'manufacturer_pn': manufacturer_pn,
        'quantity': quantity,
        'barcode': barcode,
        'raw_data': {},
    }
    d.update(extra)
    return d


class TestBarcodeScannedRowInit:
    def test_digikey_fields(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        assert row.supplier == 'digikey'
        assert row.supplier_pn == 'ABC-ND'
        assert row.manufacturer_pn == 'MPN123'
        assert row.barcode == 'MPN123'
        assert row.quantity == 10

    def test_mouser_search_name_is_mpn(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            supplier='mouser', supplier_pn='', manufacturer_pn='MPN-X'))
        assert row.search_name == 'MPN-X'

    def test_digikey_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            supplier='digikey', supplier_pn='DK-PN', manufacturer_pn='MPN-Y'))
        assert row.search_name == 'DK-PN'

    def test_lcsc_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            supplier='lcsc', supplier_pn='C123456', manufacturer_pn='ABC'))
        assert row.search_name == 'C123456'

    def test_tme_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            supplier='tme', supplier_pn='TME-PN', manufacturer_pn='MFG'))
        assert row.search_name == 'TME-PN'

    def test_barcode_value_matches_barcode(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        assert row.barcode_value == row.barcode

    def test_default_status(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        assert row.status == 'Checking...'

    def test_default_stock_quantity_from_parsed(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=25))
        assert row.stock_quantity == 25

    def test_default_stock_quantity_fallback(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=0))
        assert row.stock_quantity == 1

    def test_order_number_from_parsed(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            order_number='ORD-001',
            raw_data={'supplier_order_number': 'ORD-001'}))
        assert row.order_number == 'ORD-001'

    def test_tme_order_number_truncates_slash(self):
        row = BarcodeScannedRow("raw", _make_parsed(
            supplier='tme',
            supplier_order_number='34210324/5',
            raw_data={}))
        assert row.order_number == '34210324'


class TestBarcodeScannedRowProperties:
    def test_effective_quantity_default(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=5))
        assert row.effective_quantity == 5

    def test_effective_quantity_edited(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=5))
        row._edited_quantity = 20
        assert row.effective_quantity == 20

    def test_fallback_mpn_from_product_code(self):
        parsed = {
            'supplier': 'lcsc',
            'supplier_pn': '',
            'manufacturer_pn': '',
            'product_code': 'FALLBACK-PN',
            'quantity': 1,
            'barcode': '',
            'raw_data': {},
        }
        row = BarcodeScannedRow("raw", parsed)
        assert row.manufacturer_pn == 'FALLBACK-PN'
