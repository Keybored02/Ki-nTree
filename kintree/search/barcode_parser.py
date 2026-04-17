#!/usr/bin/env python3
"""Barcode parser for rapid part import from multiple suppliers.

Primary Support (ECIA/ISO-IEC 15434 Standard):
  - Mouser: GS1-128 QR codes with manufacturer PN only
  - Digi-Key: GS1-128 QR codes with supplier PN (-ND suffix) and manufacturer PN

Legacy Support:
  - LCSC: Custom QR field format (pc, pm, qty, on) - not ECIA standard
  - TME: Custom QR field format (PN, CPO, PO, MPN, QTY) - not ECIA standard

The barcode scanning GUI uses Digi-Key and Mouser. LCSC and TME parsers are
retained for backward compatibility and other potential uses.

All parsers return a normalized dict with:
  - supplier: supplier key
  - barcode: part number for API lookup (manufacturer_pn or supplier_pn)
  - supplier_pn: supplier-specific part number (if available)
  - manufacturer_pn: manufacturer part number
  - quantity: scanned quantity
  - raw_data: raw parsed fields
"""

import re
from typing import Dict


class BarcodeParser:
    """Parse and extract supplier-specific barcode data.

    Uses ECIA/ISO-IEC 15434 standard for 2D barcodes from Mouser and Digi-Key.
    Provides fallback to regex-based parsing for robustness.
    """

    CUSTOMER_ORDER_NUMBER = "customer_order_number"
    SUPPLIER_ORDER_NUMBER = "supplier_order_number"
    PACKING_LIST_NUMBER = "packing_list_number"
    INVOICE_NUMBER = "invoice_number"
    SHIP_DATE = "ship_date"
    DATE_CODE = "date_code"
    PURCHASE_ORDER_LINE = "purchase_order_line"
    SUPPLIER_PART_NUMBER = "supplier_part_number"
    MANUFACTURER_PART_NUMBER = "manufacturer_part_number"
    COUNTRY_OF_ORIGIN = "country_of_origin"
    LOT_CODE = "lot_code"
    MANUFACTURER = "manufacturer"
    QUANTITY = "quantity"

    @classmethod
    def ecia_field_map(cls):
        """Return a dict mapping ECIA field names to internal field names.

        Ref: https://www.ecianow.org/assets/docs/ECIA_Specifications.pdf

        Note that a particular plugin may need to reimplement this method,
        if it does not use the standard field names.
        """
        return {
            "K": cls.CUSTOMER_ORDER_NUMBER,
            "1K": cls.SUPPLIER_ORDER_NUMBER,
            "11K": cls.PACKING_LIST_NUMBER,
            "10K": cls.INVOICE_NUMBER,
            "6D": cls.SHIP_DATE,
            "9D": cls.DATE_CODE,
            "10D": cls.DATE_CODE,
            "4K": cls.PURCHASE_ORDER_LINE,
            "14K": cls.PURCHASE_ORDER_LINE,
            "P": cls.SUPPLIER_PART_NUMBER,
            "1P": cls.MANUFACTURER_PART_NUMBER,
            "30P": cls.SUPPLIER_PART_NUMBER,
            "1T": cls.LOT_CODE,
            "4L": cls.COUNTRY_OF_ORIGIN,
            "1V": cls.MANUFACTURER,
            "Q": cls.QUANTITY,
        }

    @staticmethod
    def _normalize_gs1_input(barcode: str) -> str:
        """Normalize scanner output when leading GS1 prefix chars are missing.

        Some scanners strip control characters and can also drop leading prefix
        characters (e.g. ``[`` or ``[)``), yielding forms like ``)>06...`` or
        ``>06...``. This helper restores a canonical ``[)>06`` prefix when
        possible so downstream detection/parsing remains reliable.
        """
        data = str(barcode or "").strip()
        if not data:
            return data

        if data.startswith("[)>\x1e06\x1d") or data.startswith("[)>06"):
            return data

        # If canonical marker exists later in the payload, trim leading noise.
        idx = data.find("[)>06")
        if idx > 0:
            return data[idx:]

        # Recover commonly truncated variants.
        if ")>06" in data:
            i = data.find(")>06")
            return "[" + data[i:]

        if ">06" in data:
            i = data.find(">06")
            return "[)" + data[i:]

        # Last-resort recovery: if stream starts with 06 and looks GS1-like.
        if data.startswith("06") and re.search(r"06[KPQ1]", data):
            return "[)>" + data

        return data

    @staticmethod
    def detect_supplier(barcode: str) -> str:
        """Detect supplier from barcode format and markers.

        Args:
            barcode: Raw barcode string from scanner or paste

        Returns:
            Supplier key: 'lcsc', 'tme', 'mouser', 'digikey', or 'unknown'
        """
        barcode = BarcodeParser._normalize_gs1_input(barcode)

        # LCSC: JSON-like format with curly braces
        if barcode.strip().startswith("{") and ("pm:" in barcode or "pc:" in barcode):
            return "lcsc"

        # GS1-128 format (starts with [)>06)
        if barcode.startswith("[)>06"):
            # Digi-Key: Contains -ND suffix (part-dependent marker)
            if re.search(r"-ND", barcode):
                return "digikey"

            # Mouser: GS1-128 without -ND marker
            if re.search(r"\[?\)?>?06K", barcode):
                return "mouser"

        # TME: Key-value pairs format (QTY:, PN:, etc.)
        if "QTY:" in barcode or "PN:" in barcode or "tme.eu" in barcode:
            return "tme"

        return "unknown"

    @staticmethod
    def parse_tme(barcode: str) -> Dict:
        """Parse TME barcode using TME-specific QR field format.

        TME QR format uses field identifiers: PN, CPO, PO, MPN, QTY
        (Not ECIA standard - TME's own custom format)

        Field mapping:
            PN  → supplier_part_number
            CPO → customer_order_number
            PO  → supplier_order_number
            MPN → manufacturer_part_number
            QTY → quantity
        """
        # TME field name map
        TME_FIELD_MAP = {
            "PN": "supplier_part_number",
            "CPO": "customer_order_number",
            "PO": "supplier_order_number",
            "MPN": "manufacturer_part_number",
            "QTY": "quantity",
        }

        result = {"supplier": "tme"}

        # Extract TME QR fields (delimited by field names)
        # Pattern: FIELDNAME:value with fields separated by spaces or specific delimiters
        for field_id, field_name in TME_FIELD_MAP.items():
            pattern = rf"{field_id}[:=]([^\s]+)"
            match = re.search(pattern, barcode)
            if match:
                value = match.group(1)
                if field_id == "QTY":
                    result[field_name] = int(value)
                else:
                    result[field_name] = value

        # Normalize to output format
        normalized = {
            "supplier": "tme",
            "barcode": result.get("manufacturer_part_number", "")
            or result.get("supplier_part_number", ""),
            "supplier_pn": result.get("supplier_part_number", ""),
            "manufacturer_pn": result.get("manufacturer_part_number", ""),
            "quantity": result.get("quantity", 0),
            "raw_data": result,
        }

        return normalized

    @staticmethod
    def parse_lcsc(barcode: str) -> Dict:
        """Parse LCSC barcode using LCSC-specific QR field format.

        LCSC QR format uses field identifiers: pc, pm, qty, on, pbn, etc.
        (Not ECIA standard - LCSC's own custom format)

        Field mapping:
            pc   → supplier_part_number (LCSC part code)
            pm   → manufacturer_part_number
            qty  → quantity
            on   → supplier_order_number
            pbn  → pick bin number (internal)

        Example: {pbn:PICK2603230135,on:WM2603240072,pc:C2922211,pm:DB2EKN-3.5-3P-GN,qty:65,...}
        """
        # LCSC field name map
        LCSC_FIELD_MAP = {
            "pc": "supplier_part_number",
            "pm": "manufacturer_part_number",
            "qty": "quantity",
            "on": "supplier_order_number",
            "pbn": "pick_bin_number",
        }

        result = {"supplier": "lcsc"}

        # Extract LCSC QR fields from JSON-like format
        # Pattern: fieldname:value with fields separated by commas
        for field_id, field_name in LCSC_FIELD_MAP.items():
            # Match field_id followed by : and then value until comma or }
            pattern = rf"{field_id}:([^,}}]+)"
            match = re.search(pattern, barcode)
            if match:
                value = match.group(1).strip()
                if field_id == "qty":
                    try:
                        result[field_name] = int(value)
                    except (ValueError, TypeError):
                        result[field_name] = 0
                else:
                    result[field_name] = value

        # Normalize to output format
        normalized = {
            "supplier": "lcsc",
            "barcode": result.get("manufacturer_part_number", "")
            or result.get("supplier_part_number", ""),
            "supplier_pn": result.get("supplier_part_number", ""),
            "manufacturer_pn": result.get("manufacturer_part_number", ""),
            "quantity": result.get("quantity", 0),
            "raw_data": result,
        }

        return normalized

    # ------------------------------------------------------------------
    # Fixed-order sequence parsing
    # ------------------------------------------------------------------
    # When scanners strip \x1d (GS) delimiters, walking any-identifier-
    # anywhere is ambiguous: MPNs starting with K/P or ending with -P get
    # misread as new ECIA fields. Instead we walk the payload positionally,
    # only looking for the *next expected identifier in the documented
    # supplier sequence*. Each field captures everything until the next
    # expected identifier starts, or end-of-string.
    #
    # Each entry is (identifier, field_name, required).
    # `required=False` means the field may be skipped if absent; we try to
    # match it and advance only on success.
    _DIGIKEY_SEQUENCE = [
        ("P", SUPPLIER_PART_NUMBER, True),  # P<dk_pn>-ND
        ("1P", MANUFACTURER_PART_NUMBER, True),  # 1P<mpn>
        ("30P", SUPPLIER_PART_NUMBER, False),  # 30P<dk_pn>-ND (repeat)
        ("K", CUSTOMER_ORDER_NUMBER, False),  # K<cust_order>
        ("1K", SUPPLIER_ORDER_NUMBER, False),  # 1K<sales_order>
        ("10K", INVOICE_NUMBER, False),  # 10K<invoice>
        ("9D", DATE_CODE, False),  # 9D<YYWW>
        ("1T", LOT_CODE, False),  # 1T<lot>
        ("11K", PACKING_LIST_NUMBER, False),  # 11K<packing>
        ("4L", COUNTRY_OF_ORIGIN, False),  # 4L<country>
        ("Q", QUANTITY, False),  # Q<qty>
        ("11Z", "_trailer", False),  # 11Z<pick> — terminates Q
    ]

    _MOUSER_SEQUENCE = [
        ("K", CUSTOMER_ORDER_NUMBER, True),  # K<cust_order>
        ("14K", PURCHASE_ORDER_LINE, False),  # 14K<po_line>
        (
            "1P",
            MANUFACTURER_PART_NUMBER,
            True,
        ),  # 1P<mpn> (Mouser: MPN, not supplier PN)
        ("Q", QUANTITY, True),  # Q<qty>
        ("11K", PACKING_LIST_NUMBER, False),  # 11K<pack>
        ("10K", INVOICE_NUMBER, False),  # 10K<invoice>
        ("4L", COUNTRY_OF_ORIGIN, False),  # 4L<country>
        ("1V", MANUFACTURER, False),  # 1V<manufacturer>
        ("1T", LOT_CODE, False),  # 1T<lot>
    ]

    @staticmethod
    def _quantity_to_int(value: str) -> int:
        """Coerce a raw Q-field value to int, tolerating trailing non-digits."""
        match = re.match(r"\d+", str(value or ""))
        try:
            return int(match.group(0)) if match else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_sequence(data: str, sequence: list) -> Dict[str, str]:
        """Parse a delimiter-stripped GS1 payload against a fixed field sequence.

        Walks `sequence` in order. For each (identifier, field_name, required)
        entry, checks whether the payload at `index` starts with `identifier`.
        If so, consumes the value up to the next upcoming identifier in the
        remainder of the sequence (or end-of-string). If the identifier isn't
        present and the field is optional, it's skipped; required fields that
        are missing abort the walk (the caller can fall back).

        This avoids the ambiguity of "any K/P anywhere is a new field":
        `K` is only treated as an identifier at positions where the schema
        says `K` is the next expected field.
        """
        result: Dict[str, str] = {}
        index = 0
        n = len(data)

        for i, (identifier, field_name, required) in enumerate(sequence):
            if index >= n:
                break
            if not data.startswith(identifier, index):
                if required:
                    # Required field missing — stop and let caller fall back.
                    break
                continue

            value_start = index + len(identifier)
            # Find the earliest next identifier from the remaining schema.
            # Only identifiers that appear *later in the sequence* are valid
            # terminators — this is what eliminates the ambiguity.
            value_end = n
            for next_id, _, _ in sequence[i + 1 :]:
                pos = data.find(next_id, value_start)
                if pos != -1 and pos < value_end:
                    value_end = pos

            value = data[value_start:value_end]
            # Don't overwrite a required field that was already captured
            # (e.g. Digi-Key SUPPLIER_PART_NUMBER from P then 30P — keep P).
            if field_name not in result:
                result[field_name] = value
            index = value_end

        return result

    @staticmethod
    def _strip_gs1_prefix(barcode: str) -> str:
        """Remove the GS1 header and any stray GS/RS control characters."""
        data = str(barcode or "")
        for prefix in ("[)>\x1e06\x1d", "[)>06\x1d", "[)>06"):
            if data.startswith(prefix):
                data = data[len(prefix) :]
                break
        # Drop any remaining GS (0x1d) / RS (0x1e) / EOT (0x04) chars — we're
        # parsing the delimiter-stripped form regardless of whether some
        # survived the scanner.
        return data.replace("\x1d", "").replace("\x1e", "").replace("\x04", "")

    @staticmethod
    def parse_mouser(barcode: str) -> Dict:
        """Parse Mouser GS1-128 barcode using fixed-order sequence parsing.

        Mouser schema (delimiter-stripped):
            K<cust_order> [14K<po_line>] 1P<mpn> Q<qty> [11K<pack>]
            [10K<invoice>] [4L<country>] [1V<manufacturer>] [1T<lot>]

        Important: Mouser QR encodes only the manufacturer PN — it has no
        Mouser-catalog part number. `1P` carries the MPN.
        """
        data = BarcodeParser._strip_gs1_prefix(barcode)
        fields = BarcodeParser._parse_sequence(data, BarcodeParser._MOUSER_SEQUENCE)

        # Mouser uses `K` for both customer and supplier order number.
        order_number = fields.get(BarcodeParser.CUSTOMER_ORDER_NUMBER, "")
        fields.setdefault(BarcodeParser.SUPPLIER_ORDER_NUMBER, order_number)

        mpn = fields.get(BarcodeParser.MANUFACTURER_PART_NUMBER, "")
        quantity = BarcodeParser._quantity_to_int(fields.get(BarcodeParser.QUANTITY, ""))

        raw = {"supplier": "mouser", **fields, "quantity": quantity}

        return {
            "supplier": "mouser",
            "barcode": mpn,
            "supplier_pn": "",  # Mouser QR does NOT include supplier PN
            "manufacturer_pn": mpn,
            "quantity": quantity,
            "order_number": order_number,
            "supplier_order_number": order_number,
            "customer_order_number": order_number,
            "manufacturer": fields.get(BarcodeParser.MANUFACTURER, "").strip(),
            "raw_data": raw,
        }

    @staticmethod
    def parse_digikey(barcode: str) -> Dict:
        """Parse Digi-Key GS1-128 barcode using fixed-order sequence parsing.

        Digi-Key schema (delimiter-stripped):
            P<dk_pn>-ND 1P<mpn> [30P<dk_pn>-ND] [K<cust_order>]
            [1K<sales_order>] [10K<invoice>] [9D<date>] [1T<lot>]
            [11K<pack>] [4L<country>] [Q<qty>] [11Z<pick>]

        Fixed-order walking eliminates the ambiguity caused by stripped GS
        delimiters: an MPN ending in `-P` is no longer misread as a new `P`
        field, and an MPN containing `K` is no longer misread as a new `K`
        field, because each identifier is only searched for at the position
        where the schema expects it.
        """
        data = BarcodeParser._strip_gs1_prefix(barcode)
        fields = BarcodeParser._parse_sequence(data, BarcodeParser._DIGIKEY_SEQUENCE)
        fields.pop("_trailer", None)

        supplier_pn = fields.get(BarcodeParser.SUPPLIER_PART_NUMBER, "")
        manufacturer_pn = fields.get(BarcodeParser.MANUFACTURER_PART_NUMBER, "")
        customer_order = fields.get(BarcodeParser.CUSTOMER_ORDER_NUMBER, "")
        supplier_order = fields.get(BarcodeParser.SUPPLIER_ORDER_NUMBER, "") or customer_order

        quantity = BarcodeParser._quantity_to_int(fields.get(BarcodeParser.QUANTITY, ""))

        raw = {"supplier": "digikey", **fields, "quantity": quantity}

        return {
            "supplier": "digikey",
            "barcode": manufacturer_pn or supplier_pn,
            "supplier_pn": supplier_pn,
            "digikey_pn": supplier_pn,
            "manufacturer_pn": manufacturer_pn,
            "quantity": quantity,
            "order_number": supplier_order,
            "supplier_order_number": supplier_order,
            "customer_order_number": customer_order,
            "raw_data": raw,
        }

    @classmethod
    def parse(cls, barcode: str) -> Dict:
        """Parse barcode by detecting supplier and delegating to parser.

        Args:
            barcode: Raw barcode string from scanner or paste

        Returns:
            Normalized dict with: supplier, supplier_pn, manufacturer_pn,
            quantity, and supplier-specific fields. On error, includes
            'supplier': 'unknown' and an 'error' message.
        """
        barcode = cls._normalize_gs1_input(barcode)
        barcode = barcode.strip()
        supplier = cls.detect_supplier(barcode)

        if supplier == "lcsc":
            return cls.parse_lcsc(barcode)
        elif supplier == "tme":
            return cls.parse_tme(barcode)
        elif supplier == "mouser":
            return cls.parse_mouser(barcode)
        elif supplier == "digikey":
            return cls.parse_digikey(barcode)
        else:
            return {
                "supplier": "unknown",
                "barcode": "",
                "error": f"Could not identify supplier from: {barcode[:50]}...",
            }


def main() -> None:
    """Test parser with barcode examples from supported suppliers.

    Barcode scanning GUI uses Digi-Key and Mouser. LCSC and TME shown for reference.
    """
    test_barcodes = [
        ("Mouser", "[)>06K3828825514K0011P67C18-8-M-PQ511K0895610514LUS1VGrayhill"),
        (
            "Mouser (P-only MPN)",
            "[)>06K3878883614K0011PKTSC-21RQ10011K0896618144LVN1VDiptronics",
        ),
        (
            "Digi-Key",
            "[)>06PGH7880-ND1P67C18-8-M-P30PGH7880-NDK1K9818346510K1228880039D25491T000043333911K14LUSQ1511Z",
        ),
        (
            "LCSC",
            "{pbn:PICK2603230135,on:WM2603240072,pc:C2922211,pm:DB2EKN-3.5-3P-GN,qty:65,mc:,cc:1,pdi:204696597,hp:null,wc:ZH}",
        ),
        (
            "TME (reference)",
            "PN:M3X10/D7985B CPO:12345 PO:33388984 MPN:M3X10/D7985B QTY:100",
        ),
    ]

    parser = BarcodeParser()
    print(f"\n{'=' * 70}")
    print("BARCODE PARSER TEST (ECIA/ISO-IEC 15434 Standard)")
    print("Barcode Scanning GUI: Digi-Key and Mouser only")
    print(f"{'=' * 70}")

    for supplier_name, barcode in test_barcodes:
        result = parser.parse(barcode)
        print(f"\n{supplier_name}:")
        print(f"  Barcode (API lookup): {result.get('barcode', '(none)')}")
        print(f"  Part: {result.get('supplier_pn', '(none)')}")
        print(f"  Manufacturer PN: {result.get('manufacturer_pn', '(none)')}")
        print(f"  Quantity: {result.get('quantity', 0)}")

    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
