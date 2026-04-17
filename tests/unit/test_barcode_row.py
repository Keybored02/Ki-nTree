"""Unit tests for BarcodeScannedRow field derivation."""

import unittest

from kintree.gui.views.barcode import BarcodeScannedRow


def _make_parsed(
    supplier="digikey",
    supplier_pn="ABC-ND",
    manufacturer_pn="MPN123",
    quantity=10,
    barcode="MPN123",
    **extra,
):
    d = {
        "supplier": supplier,
        "supplier_pn": supplier_pn,
        "manufacturer_pn": manufacturer_pn,
        "quantity": quantity,
        "barcode": barcode,
        "raw_data": {},
    }
    d.update(extra)
    return d


class TestBarcodeScannedRowInit(unittest.TestCase):
    def test_digikey_fields(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        self.assertEqual(row.supplier, "digikey")
        self.assertEqual(row.supplier_pn, "ABC-ND")
        self.assertEqual(row.manufacturer_pn, "MPN123")
        self.assertEqual(row.barcode, "MPN123")
        self.assertEqual(row.quantity, 10)

    def test_mouser_search_name_is_mpn(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(supplier="mouser", supplier_pn="", manufacturer_pn="MPN-X"),
        )
        self.assertEqual(row.search_name, "MPN-X")

    def test_digikey_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(supplier="digikey", supplier_pn="DK-PN", manufacturer_pn="MPN-Y"),
        )
        self.assertEqual(row.search_name, "DK-PN")

    def test_lcsc_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(supplier="lcsc", supplier_pn="C123456", manufacturer_pn="ABC"),
        )
        self.assertEqual(row.search_name, "C123456")

    def test_tme_search_name_is_supplier_pn(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(supplier="tme", supplier_pn="TME-PN", manufacturer_pn="MFG"),
        )
        self.assertEqual(row.search_name, "TME-PN")

    def test_barcode_value_matches_barcode(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        self.assertEqual(row.barcode_value, row.barcode)

    def test_default_status(self):
        row = BarcodeScannedRow("raw", _make_parsed())
        self.assertEqual(row.status, "Checking...")

    def test_default_stock_quantity_from_parsed(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=25))
        self.assertEqual(row.stock_quantity, 25)

    def test_default_stock_quantity_fallback(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=0))
        self.assertEqual(row.stock_quantity, 1)

    def test_order_number_from_parsed(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(order_number="ORD-001", raw_data={"supplier_order_number": "ORD-001"}),
        )
        self.assertEqual(row.order_number, "ORD-001")

    def test_tme_order_number_truncates_slash(self):
        row = BarcodeScannedRow(
            "raw",
            _make_parsed(supplier="tme", supplier_order_number="34210324/5", raw_data={}),
        )
        self.assertEqual(row.order_number, "34210324")


class TestBarcodeScannedRowProperties(unittest.TestCase):
    def test_effective_quantity_default(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=5))
        self.assertEqual(row.effective_quantity, 5)

    def test_effective_quantity_edited(self):
        row = BarcodeScannedRow("raw", _make_parsed(quantity=5))
        row._edited_quantity = 20
        self.assertEqual(row.effective_quantity, 20)

    def test_fallback_mpn_from_product_code(self):
        parsed = {
            "supplier": "lcsc",
            "supplier_pn": "",
            "manufacturer_pn": "",
            "product_code": "FALLBACK-PN",
            "quantity": 1,
            "barcode": "",
            "raw_data": {},
        }
        row = BarcodeScannedRow("raw", parsed)
        self.assertEqual(row.manufacturer_pn, "FALLBACK-PN")
