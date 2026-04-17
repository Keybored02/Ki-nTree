"""Unit tests for kintree.search.barcode_parser."""

import pytest

from kintree.search.barcode_parser import BarcodeParser


# ---------------------------------------------------------------------------
# Supplier detection
# ---------------------------------------------------------------------------
class TestDetectSupplier:
    def test_digikey(self):
        bc = "[)>06PGH7880-ND1P67C18-8-M-P30PGH7880-NDK1K9818346510K1228880039D25491T000043333911K14LUSQ1511Z"
        assert BarcodeParser.detect_supplier(bc) == "digikey"

    def test_mouser(self):
        bc = "[)>06K3828825514K0011P67C18-8-M-PQ511K0895610514LUS1VGrayhill"
        assert BarcodeParser.detect_supplier(bc) == "mouser"

    def test_lcsc(self):
        bc = "{pbn:PICK2603230135,on:WM2603240072,pc:C2922211,pm:DB2EKN-3.5-3P-GN,qty:65,mc:,cc:1,pdi:204696597,hp:null,wc:ZH}"
        assert BarcodeParser.detect_supplier(bc) == "lcsc"

    def test_tme(self):
        bc = "PN:M3X10/D7985B CPO:12345 PO:33388984 MPN:M3X10/D7985B QTY:100"
        assert BarcodeParser.detect_supplier(bc) == "tme"

    def test_unknown(self):
        assert BarcodeParser.detect_supplier("random string") == "unknown"

    def test_empty(self):
        assert BarcodeParser.detect_supplier("") == "unknown"


# ---------------------------------------------------------------------------
# GS1 prefix normalisation
# ---------------------------------------------------------------------------
class TestNormalizeGs1Input:
    def test_canonical_unchanged(self):
        bc = "[)>06Ktest"
        assert BarcodeParser._normalize_gs1_input(bc) == bc

    def test_missing_bracket(self):
        assert BarcodeParser._normalize_gs1_input(")>06Ktest").startswith("[)>06")

    def test_missing_bracket_paren(self):
        assert BarcodeParser._normalize_gs1_input(">06Ktest").startswith("[)>06")

    def test_starts_with_06(self):
        assert BarcodeParser._normalize_gs1_input("06Ktest").startswith("[)>06")

    def test_empty(self):
        assert BarcodeParser._normalize_gs1_input("") == ""

    def test_none(self):
        assert BarcodeParser._normalize_gs1_input(None) == ""

    def test_with_rs_gs_chars(self):
        bc = "[)>\x1e06\x1dKtest"
        assert BarcodeParser._normalize_gs1_input(bc) == bc


# ---------------------------------------------------------------------------
# _strip_gs1_prefix
# ---------------------------------------------------------------------------
class TestStripGs1Prefix:
    def test_canonical(self):
        assert BarcodeParser._strip_gs1_prefix("[)>06Ptest") == "Ptest"

    def test_with_control_chars(self):
        assert BarcodeParser._strip_gs1_prefix("[)>\x1e06\x1dPtest") == "Ptest"

    def test_strips_embedded_gs_rs(self):
        result = BarcodeParser._strip_gs1_prefix("[)>06P\x1dtest\x1emore\x04end")
        assert "\x1d" not in result
        assert "\x1e" not in result
        assert "\x04" not in result

    def test_no_prefix(self):
        assert BarcodeParser._strip_gs1_prefix("Ptest") == "Ptest"


# ---------------------------------------------------------------------------
# _quantity_to_int
# ---------------------------------------------------------------------------
class TestQuantityToInt:
    def test_plain_number(self):
        assert BarcodeParser._quantity_to_int("150") == 150

    def test_trailing_text(self):
        assert BarcodeParser._quantity_to_int("15abc") == 15

    def test_empty(self):
        assert BarcodeParser._quantity_to_int("") == 0

    def test_none(self):
        assert BarcodeParser._quantity_to_int(None) == 0

    def test_non_numeric(self):
        assert BarcodeParser._quantity_to_int("abc") == 0


# ---------------------------------------------------------------------------
# _parse_sequence
# ---------------------------------------------------------------------------
class TestParseSequence:
    def test_simple_digikey(self):
        data = "PABC-ND1PMPN123Q50"
        seq = [
            ('P', BarcodeParser.SUPPLIER_PART_NUMBER, True),
            ('1P', BarcodeParser.MANUFACTURER_PART_NUMBER, True),
            ('Q', BarcodeParser.QUANTITY, False),
        ]
        result = BarcodeParser._parse_sequence(data, seq)
        assert result[BarcodeParser.SUPPLIER_PART_NUMBER] == "ABC-ND"
        assert result[BarcodeParser.MANUFACTURER_PART_NUMBER] == "MPN123"
        assert result[BarcodeParser.QUANTITY] == "50"

    def test_optional_field_absent(self):
        data = "P123-ND1PMPN"
        seq = [
            ('P', BarcodeParser.SUPPLIER_PART_NUMBER, True),
            ('1P', BarcodeParser.MANUFACTURER_PART_NUMBER, True),
            ('Q', BarcodeParser.QUANTITY, False),
        ]
        result = BarcodeParser._parse_sequence(data, seq)
        assert BarcodeParser.QUANTITY not in result

    def test_required_missing_stops(self):
        data = "1PMPN123Q5"
        seq = [
            ('P', BarcodeParser.SUPPLIER_PART_NUMBER, True),
            ('1P', BarcodeParser.MANUFACTURER_PART_NUMBER, True),
        ]
        result = BarcodeParser._parse_sequence(data, seq)
        assert result == {}

    def test_empty_data(self):
        assert BarcodeParser._parse_sequence("", BarcodeParser._DIGIKEY_SEQUENCE) == {}


# ---------------------------------------------------------------------------
# Digi-Key full parse
# ---------------------------------------------------------------------------
class TestParseDigikey:
    SAMPLE = "[)>06PGH7880-ND1P67C18-8-M-P30PGH7880-NDK1K9818346510K1228880039D25491T000043333911K14LUSQ1511Z"

    def test_supplier(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["supplier"] == "digikey"

    def test_supplier_pn(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["supplier_pn"] == "GH7880-ND"

    def test_manufacturer_pn(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["manufacturer_pn"] == "67C18-8-M-P"

    def test_quantity(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["quantity"] == 15

    def test_barcode_is_mpn(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["barcode"] == "67C18-8-M-P"

    def test_country(self):
        r = BarcodeParser.parse_digikey(self.SAMPLE)
        assert r["raw_data"].get(BarcodeParser.COUNTRY_OF_ORIGIN) == "US"


# ---------------------------------------------------------------------------
# Mouser full parse
# ---------------------------------------------------------------------------
class TestParseMouser:
    SAMPLE = "[)>06K3828825514K0011P67C18-8-M-PQ511K0895610514LUS1VGrayhill"

    def test_supplier(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["supplier"] == "mouser"

    def test_manufacturer_pn(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["manufacturer_pn"] == "67C18-8-M-P"

    def test_supplier_pn_empty(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["supplier_pn"] == ""

    def test_quantity(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["quantity"] == 5

    def test_manufacturer(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["manufacturer"] == "Grayhill"

    def test_barcode_is_mpn(self):
        r = BarcodeParser.parse_mouser(self.SAMPLE)
        assert r["barcode"] == "67C18-8-M-P"

    def test_p_only_mpn(self):
        """Mouser barcode where MPN starts with K and ends with -P."""
        bc = "[)>06K3878883614K0011PKTSC-21RQ10011K0896618144LVN1VDiptronics"
        r = BarcodeParser.parse_mouser(bc)
        assert r["manufacturer_pn"] == "KTSC-21R"
        assert r["quantity"] == 100


# ---------------------------------------------------------------------------
# LCSC full parse
# ---------------------------------------------------------------------------
class TestParseLcsc:
    SAMPLE = "{pbn:PICK2603230135,on:WM2603240072,pc:C2922211,pm:DB2EKN-3.5-3P-GN,qty:65,mc:,cc:1,pdi:204696597,hp:null,wc:ZH}"

    def test_supplier(self):
        r = BarcodeParser.parse_lcsc(self.SAMPLE)
        assert r["supplier"] == "lcsc"

    def test_supplier_pn(self):
        r = BarcodeParser.parse_lcsc(self.SAMPLE)
        assert r["supplier_pn"] == "C2922211"

    def test_manufacturer_pn(self):
        r = BarcodeParser.parse_lcsc(self.SAMPLE)
        assert r["manufacturer_pn"] == "DB2EKN-3.5-3P-GN"

    def test_quantity(self):
        r = BarcodeParser.parse_lcsc(self.SAMPLE)
        assert r["quantity"] == 65

    def test_barcode_is_mpn(self):
        r = BarcodeParser.parse_lcsc(self.SAMPLE)
        assert r["barcode"] == "DB2EKN-3.5-3P-GN"


# ---------------------------------------------------------------------------
# TME full parse
# ---------------------------------------------------------------------------
class TestParseTme:
    SAMPLE = "PN:M3X10/D7985B CPO:12345 PO:33388984 MPN:M3X10/D7985B QTY:100"

    def test_supplier(self):
        r = BarcodeParser.parse_tme(self.SAMPLE)
        assert r["supplier"] == "tme"

    def test_supplier_pn(self):
        r = BarcodeParser.parse_tme(self.SAMPLE)
        assert r["supplier_pn"] == "M3X10/D7985B"

    def test_manufacturer_pn(self):
        r = BarcodeParser.parse_tme(self.SAMPLE)
        assert r["manufacturer_pn"] == "M3X10/D7985B"

    def test_quantity(self):
        r = BarcodeParser.parse_tme(self.SAMPLE)
        assert r["quantity"] == 100


# ---------------------------------------------------------------------------
# Top-level parse() dispatcher
# ---------------------------------------------------------------------------
class TestParse:
    def test_routes_to_digikey(self):
        bc = "[)>06PGH7880-ND1P67C18-8-M-P30PGH7880-NDQ15"
        assert BarcodeParser.parse(bc)["supplier"] == "digikey"

    def test_routes_to_mouser(self):
        bc = "[)>06K12314K0011PMPNQ10"
        assert BarcodeParser.parse(bc)["supplier"] == "mouser"

    def test_routes_to_lcsc(self):
        bc = "{pc:C123,pm:MPN,qty:10}"
        assert BarcodeParser.parse(bc)["supplier"] == "lcsc"

    def test_routes_to_tme(self):
        bc = "PN:ABC QTY:5 MPN:XYZ"
        assert BarcodeParser.parse(bc)["supplier"] == "tme"

    def test_unknown_returns_error(self):
        r = BarcodeParser.parse("garbage")
        assert r["supplier"] == "unknown"
        assert "error" in r
