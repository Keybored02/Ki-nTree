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
from typing import Dict, List


class BarcodeParser:
    """Parse and extract supplier-specific barcode data.

    Uses ECIA/ISO-IEC 15434 standard for 2D barcodes from Mouser and Digi-Key.
    Provides fallback to regex-based parsing for robustness.
    """

    CUSTOMER_ORDER_NUMBER = 'customer_order_number'
    SUPPLIER_ORDER_NUMBER = 'supplier_order_number'
    PACKING_LIST_NUMBER = 'packing_list_number'
    INVOICE_NUMBER = 'invoice_number'
    SHIP_DATE = 'ship_date'
    DATE_CODE = 'date_code'
    PURCHASE_ORDER_LINE = 'purchase_order_line'
    SUPPLIER_PART_NUMBER = 'supplier_part_number'
    MANUFACTURER_PART_NUMBER = 'manufacturer_part_number'
    COUNTRY_OF_ORIGIN = 'country_of_origin'
    LOT_CODE = 'lot_code'
    MANUFACTURER = 'manufacturer'
    QUANTITY = 'quantity'

    @classmethod
    def ecia_field_map(cls):
        """Return a dict mapping ECIA field names to internal field names.

        Ref: https://www.ecianow.org/assets/docs/ECIA_Specifications.pdf

        Note that a particular plugin may need to reimplement this method,
        if it does not use the standard field names.
        """
        return {
            'K': cls.CUSTOMER_ORDER_NUMBER,
            '1K': cls.SUPPLIER_ORDER_NUMBER,
            '11K': cls.PACKING_LIST_NUMBER,
            '10K': cls.INVOICE_NUMBER,
            '6D': cls.SHIP_DATE,
            '9D': cls.DATE_CODE,
            '10D': cls.DATE_CODE,
            '4K': cls.PURCHASE_ORDER_LINE,
            '14K': cls.PURCHASE_ORDER_LINE,
            'P': cls.SUPPLIER_PART_NUMBER,
            '1P': cls.MANUFACTURER_PART_NUMBER,
            '30P': cls.SUPPLIER_PART_NUMBER,
            '1T': cls.LOT_CODE,
            '4L': cls.COUNTRY_OF_ORIGIN,
            '1V': cls.MANUFACTURER,
            'Q': cls.QUANTITY,
        }

    @staticmethod
    def _normalize_gs1_input(barcode: str) -> str:
        """Normalize scanner output when leading GS1 prefix chars are missing.

        Some scanners strip control characters and can also drop leading prefix
        characters (e.g. ``[`` or ``[)``), yielding forms like ``)>06...`` or
        ``>06...``. This helper restores a canonical ``[)>06`` prefix when
        possible so downstream detection/parsing remains reliable.
        """
        data = str(barcode or '').strip()
        if not data:
            return data

        if data.startswith('[)>\x1e06\x1d') or data.startswith('[)>06'):
            return data

        # If canonical marker exists later in the payload, trim leading noise.
        idx = data.find('[)>06')
        if idx > 0:
            return data[idx:]

        # Recover commonly truncated variants.
        if ')>06' in data:
            i = data.find(')>06')
            return '[' + data[i:]

        if '>06' in data:
            i = data.find('>06')
            return '[)' + data[i:]

        # Last-resort recovery: if stream starts with 06 and looks GS1-like.
        if data.startswith('06') and re.search(r'06[KPQ1]', data):
            return '[)>' + data

        return data

    @staticmethod
    def _extract_compact_quantity(data: str) -> int:
        """Extract quantity from compact GS1 text where delimiters may be missing.

        Some scanner outputs omit group separators and collapse fields into a
        single string, e.g. ``Q511K...`` where quantity is ``5`` and ``11K``
        starts the next field. This helper prioritizes those boundaries and
        falls back to the broader Q<digits> pattern.
        """
        # Prefer quantity followed by known compact field starts (11K / 11Z).
        qty_match = re.search(r'Q(\d+?)(?=11[ZK])', data)
        if not qty_match:
            # Fallback: stop at next alpha field marker.
            qty_match = re.search(r'Q(\d+?)(?=[A-Z])', data)

        if not qty_match:
            return 0

        try:
            return int(qty_match.group(1))
        except (TypeError, ValueError):
            return 0

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
        if barcode.strip().startswith('{') and ('pm:' in barcode or 'pc:' in barcode):
            return 'lcsc'

        # GS1-128 format (starts with [)>06)
        if barcode.startswith('[)>06'):
            # Digi-Key: Contains -ND suffix (part-dependent marker)
            if re.search(r'-ND', barcode):
                return 'digikey'

            # Mouser: GS1-128 without -ND marker
            if re.search(r'\[?\)?>?06K', barcode):
                return 'mouser'

        # TME: Key-value pairs format (QTY:, PN:, etc.)
        if 'QTY:' in barcode or 'PN:' in barcode or 'tme.eu' in barcode:
            return 'tme'

        return 'unknown'

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
            'PN': 'supplier_part_number',
            'CPO': 'customer_order_number',
            'PO': 'supplier_order_number',
            'MPN': 'manufacturer_part_number',
            'QTY': 'quantity',
        }

        result = {'supplier': 'tme'}

        # Extract TME QR fields (delimited by field names)
        # Pattern: FIELDNAME:value with fields separated by spaces or specific delimiters
        for field_id, field_name in TME_FIELD_MAP.items():
            pattern = rf'{field_id}[:=]([^\s]+)'
            match = re.search(pattern, barcode)
            if match:
                value = match.group(1)
                if field_id == 'QTY':
                    result[field_name] = int(value)
                else:
                    result[field_name] = value

        # Normalize to output format
        normalized = {
            'supplier': 'tme',
            'barcode': result.get('manufacturer_part_number', '') or result.get('supplier_part_number', ''),
            'supplier_pn': result.get('supplier_part_number', ''),
            'manufacturer_pn': result.get('manufacturer_part_number', ''),
            'quantity': result.get('quantity', 0),
            'raw_data': result
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
            'pc': 'supplier_part_number',
            'pm': 'manufacturer_part_number',
            'qty': 'quantity',
            'on': 'supplier_order_number',
            'pbn': 'pick_bin_number',
        }

        result = {'supplier': 'lcsc'}

        # Extract LCSC QR fields from JSON-like format
        # Pattern: fieldname:value with fields separated by commas
        for field_id, field_name in LCSC_FIELD_MAP.items():
            # Match field_id followed by : and then value until comma or }
            pattern = rf'{field_id}:([^,}}]+)'
            match = re.search(pattern, barcode)
            if match:
                value = match.group(1).strip()
                if field_id == 'qty':
                    try:
                        result[field_name] = int(value)
                    except (ValueError, TypeError):
                        result[field_name] = 0
                else:
                    result[field_name] = value

        # Normalize to output format
        normalized = {
            'supplier': 'lcsc',
            'barcode': result.get('manufacturer_part_number', '') or result.get('supplier_part_number', ''),
            'supplier_pn': result.get('supplier_part_number', ''),
            'manufacturer_pn': result.get('manufacturer_part_number', ''),
            'quantity': result.get('quantity', 0),
            'raw_data': result
        }

        return normalized

    @staticmethod
    def parse_isoiec_15434_barcode(barcode_data: str) -> List[str]:
        """Parse ISO/IEC 15434 barcode, returning split fields.

        ISO/IEC 15434 format:
            - Header: [)>\x1e06\x1d
            - Fields: separated by \x1d (GS - Group Separator)
            - Trailer: \x1e\x04 (RS/EOT)

        Also handles old Mouser barcode format which starts with >[)>06\x1d
        """
        OLD_MOUSER_HEADER = '>[)>06\x1d'
        STANDARD_HEADER = '[)>\x1e06\x1d'
        TRAILER = '\x1e\x04'
        DELIMITER = '\x1d'

        # Handle old Mouser format
        if barcode_data.startswith(OLD_MOUSER_HEADER):
            barcode_data = barcode_data.replace(OLD_MOUSER_HEADER, STANDARD_HEADER, 1)

        # Check for standard header
        if not barcode_data.startswith(STANDARD_HEADER):
            return []

        # Strip header and trailer
        data = barcode_data[len(STANDARD_HEADER):]
        if data.endswith(TRAILER):
            data = data[:-len(TRAILER)]

        return data.split(DELIMITER) if data else []

    @staticmethod
    def parse_ecia_fields(fields: List[str]) -> Dict[str, str]:
        """Parse ECIA field identifiers and extract values.

        Args:
            fields: List of field strings with identifiers (e.g., ['K123', 'P456', '1P789'])

        Returns:
            Dict mapping field names to values (e.g., {'customer_order_number': '123', ...})
        
        Note: When searching for next identifier, only looks for multi-char identifiers (1K, 1P, etc)
        and the critical single-char 'Q' (quantity) to avoid false matches with single chars in part 
        numbers (e.g., 'K' in 'K64F'). 'Q' is always a field delimiter and should end MFN values.
        """
        barcode_fields = {}
        field_map = BarcodeParser.ecia_field_map()
        identifiers = sorted(field_map.keys(), key=len, reverse=True)
        
        # Multi-char identifiers + critical single-char 'Q' (quantity field always ends a value in ECIA)
        boundary_identifiers = [i for i in identifiers if len(i) > 1 or i == 'Q']
        
        # Pattern for finding next field boundary
        boundary_pattern = re.compile('|'.join(re.escape(identifier) for identifier in boundary_identifiers)) if boundary_identifiers else None

        for field in fields:
            text = re.sub(r'^[\x1d\x1e]+|[\x1d\x1e]+$', '', str(field or '').strip())
            if not text:
                continue

            index = 0
            while index < len(text):
                matched_identifier = None
                for identifier in identifiers:
                    if text.startswith(identifier, index):
                        matched_identifier = identifier
                        break

                if not matched_identifier:
                    index += 1
                    continue

                value_start = index + len(matched_identifier)
                remaining = text[value_start:]
                
                # Search for next field boundary (multi-char identifiers + 'Q')
                next_match = boundary_pattern.search(remaining) if boundary_pattern else None

                if next_match:
                    value_end = value_start + next_match.start()
                else:
                    value_end = len(text)

                field_name = field_map[matched_identifier]
                barcode_fields[field_name] = text[value_start:value_end]
                index = value_end if value_end > index else value_start

        return barcode_fields

    @staticmethod
    def _extract_ecia_field_value(barcode: str, identifiers: List[str]) -> str:
        """Extract the first matching ECIA field value from raw barcode text."""
        data = str(barcode or '')
        for identifier in identifiers:
            pattern = rf'(?:^|\x1d|\x1e){re.escape(identifier)}([^\x1d\x1e]+)'
            match = re.search(pattern, data)
            if match:
                return match.group(1).strip()
        return ''

    @staticmethod
    def parse_mouser(barcode: str) -> Dict:
        """Parse Mouser GS1-128 barcode using ISO/IEC 15434 standard.

        Format: [)>\x1e06\x1dK<id>\x1dK<field>\x1dP<mfn>\x1dQ<qty>\x1dK<invoice>...\x1e\x04

        Mouser uses the custom order number ('K') field for both the order number
        and the customer order number, so we set supplier_order_number to match
        customer_order_number if not explicitly provided.

        Important: Mouser QR contains ONLY manufacturer_pn, no supplier PN.
        """
        result = {'supplier': 'mouser'}

        # Parse ISO/IEC 15434 format
        fields = BarcodeParser.parse_isoiec_15434_barcode(barcode)

        if not fields:
            # Fallback to regex parsing if ISO/IEC parsing fails
            return BarcodeParser._parse_mouser_fallback(barcode)

        # Extract ECIA fields
        barcode_fields = BarcodeParser.parse_ecia_fields(fields)

        # Mouser special case: if only customer_order_number is present,
        # use it for supplier_order_number as well
        if order_number := barcode_fields.get('customer_order_number'):
            barcode_fields.setdefault('supplier_order_number', order_number)

        # Extract normalized fields
        result.update(barcode_fields)

        # Use extracted supplier_part_number if available (from field 'P')
        supplier_pn = result.get('supplier_part_number', '')
        manufacturer_pn = result.get('manufacturer_part_number', '')
        
        normalized = {
            'supplier': 'mouser',
            'barcode': manufacturer_pn or supplier_pn or '',
            'supplier_pn': supplier_pn,  # Use extracted 'P' field if present
            'manufacturer_pn': manufacturer_pn,
            'quantity': int(result.get('quantity', 0)) if result.get('quantity') else 0,
            'order_number': result.get('supplier_order_number', '') or result.get('customer_order_number', ''),
            'supplier_order_number': result.get('supplier_order_number', ''),
            'customer_order_number': result.get('customer_order_number', ''),
            'raw_data': result
        }

        return normalized

    @staticmethod
    def _parse_mouser_fallback(barcode: str) -> Dict:
        """Fallback regex-based parser for Mouser if ISO/IEC 15434 parsing fails.

        This provides robustness against non-standard barcode formats.
        """
        result = {'supplier': 'mouser'}

        # Remove GS1 prefix
        data = barcode[5:] if barcode.startswith('[)>06') else barcode

        barcode_fields = BarcodeParser.parse_ecia_fields([data])
        order_number = (
            barcode_fields.get('supplier_order_number')
            or barcode_fields.get('customer_order_number')
            or ''
        )

        result.update(barcode_fields)

        # Manufacturer PN: preserve terminal '-P' before next ECIA token.
        mfn_match = re.search(r'1?P([A-Z0-9\-]+?-P)(?=(?:30P|1K|10K|11K|4L|1V|Q|$))', data)
        if mfn_match:
            result['manufacturer_part_number'] = mfn_match.group(1)

        result['quantity'] = BarcodeParser._extract_compact_quantity(data)
        if order_number:
            result['customer_order_number'] = order_number
            result['supplier_order_number'] = order_number

        # Use extracted supplier_part_number if available
        supplier_pn = result.get('supplier_part_number', '')
        manufacturer_pn = result.get('manufacturer_part_number', '')

        normalized = {
            'supplier': 'mouser',
            'barcode': manufacturer_pn or supplier_pn or '',
            'supplier_pn': supplier_pn,  # Use extracted value if present
            'manufacturer_pn': manufacturer_pn,
            'quantity': int(result.get('quantity', 0)) if result.get('quantity') else 0,
            'order_number': order_number,
            'supplier_order_number': result.get('supplier_order_number', ''),
            'customer_order_number': result.get('customer_order_number', ''),
            'raw_data': result
        }

        return normalized

    @staticmethod
    def parse_digikey(barcode: str) -> Dict:
        """Parse Digi-Key GS1-128 barcode using ISO/IEC 15434 standard.

        Format: [)>\x1e06\x1dP<digikey_id>\x1d1P<mfn>\x1dQ<qty>\x1d...

        Digi-Key part numbers have -ND suffix and are included in the QR.
        Includes both supplier_pn (Digi-Key ID) and manufacturer_pn.
        """
        result = {'supplier': 'digikey'}

        # Parse ISO/IEC 15434 format
        fields = BarcodeParser.parse_isoiec_15434_barcode(barcode)

        if not fields:
            # Fallback to regex parsing if ISO/IEC parsing fails
            return BarcodeParser._parse_digikey_fallback(barcode)

        # Extract ECIA fields
        barcode_fields = BarcodeParser.parse_ecia_fields(fields)
        result.update(barcode_fields)

        # Always include supplier_order_number from 1K/K field if present
        supplier_order_number = result.get('supplier_order_number', '')
        if not supplier_order_number:
            # Try to extract from ECIA fields if missing
            supplier_order_number = result.get('customer_order_number', '')

        supplier_part_number = result.get('supplier_part_number', '') or result.get('customer_order_number', '')

        normalized = {
            'supplier': 'digikey',
            'barcode': result.get('manufacturer_part_number', '') or supplier_part_number,
            'supplier_pn': supplier_part_number,
            'digikey_pn': supplier_part_number,
            'manufacturer_pn': result.get('manufacturer_part_number', ''),
            'quantity': int(result.get('quantity', 0)) if result.get('quantity') else 0,
            'order_number': supplier_order_number or result.get('customer_order_number', ''),
            'supplier_order_number': supplier_order_number,
            'customer_order_number': result.get('customer_order_number', ''),
            'raw_data': result
        }

        return normalized

    @staticmethod
    def _parse_digikey_fallback(barcode: str) -> Dict:
        """Fallback regex-based parser for Digi-Key if ISO/IEC 15434 parsing fails.

        This provides robustness against non-standard barcode formats.
        """
        result = {'supplier': 'digikey'}

        # Remove GS1 prefix
        data = barcode[5:] if barcode.startswith('[)>06') else barcode

        barcode_fields = BarcodeParser.parse_ecia_fields([data])
        order_number = (
            barcode_fields.get('supplier_order_number')
            or barcode_fields.get('customer_order_number')
            or ''
        )

        result.update(barcode_fields)

        # Digi-Key part number: P followed by alphanumerics ending with -ND
        dk_pn_match = re.search(r'P([A-Z0-9]{2,}-ND)', data)
        if dk_pn_match:
            result['supplier_part_number'] = dk_pn_match.group(1)
            result['digikey_pn'] = dk_pn_match.group(1)

        # Manufacturer PN: preserve terminal '-P' before next ECIA token.
        mfn_match = re.search(r'1P([A-Z0-9\-]+?-P)(?=(?:30P|1K|10K|11K|4L|1V|Q|$))', data)
        if mfn_match:
            result['manufacturer_part_number'] = mfn_match.group(1)

        result['quantity'] = BarcodeParser._extract_compact_quantity(data)
        if order_number:
            result['supplier_order_number'] = order_number
            result['customer_order_number'] = order_number

        normalized = {
            'supplier': 'digikey',
            'barcode': result.get('manufacturer_part_number', '') or result.get('supplier_part_number', ''),
            'supplier_pn': result.get('supplier_part_number', ''),
            'digikey_pn': result.get('supplier_part_number', ''),
            'manufacturer_pn': result.get('manufacturer_part_number', ''),
            'quantity': int(result.get('quantity', 0)) if result.get('quantity') else 0,
            'order_number': result.get('supplier_order_number', '') or result.get('customer_order_number', ''),
            'supplier_order_number': result.get('supplier_order_number', ''),
            'customer_order_number': result.get('customer_order_number', ''),
            'raw_data': result
        }

        return normalized

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

        if supplier == 'lcsc':
            return cls.parse_lcsc(barcode)
        elif supplier == 'tme':
            return cls.parse_tme(barcode)
        elif supplier == 'mouser':
            return cls.parse_mouser(barcode)
        elif supplier == 'digikey':
            return cls.parse_digikey(barcode)
        else:
            return {
                'supplier': 'unknown',
                'barcode': '',
                'error': f'Could not identify supplier from: {barcode[:50]}...'
            }


def main() -> None:
    """Test parser with barcode examples from supported suppliers.
    
    Barcode scanning GUI uses Digi-Key and Mouser. LCSC and TME shown for reference.
    """
    test_barcodes = [
        ("Mouser", "[)>06K3828825514K0011P67C18-8-M-PQ511K0895610514LUS1VGrayhill"),
        ("Digi-Key", "[)>06PGH7880-ND1P67C18-8-M-P30PGH7880-NDK1K9818346510K1228880039D25491T000043333911K14LUSQ1511Z"),
        ("LCSC", "{pbn:PICK2603230135,on:WM2603240072,pc:C2922211,pm:DB2EKN-3.5-3P-GN,qty:65,mc:,cc:1,pdi:204696597,hp:null,wc:ZH}"),
        ("TME (reference)", "PN:M3X10/D7985B CPO:12345 PO:33388984 MPN:M3X10/D7985B QTY:100"),
    ]

    parser = BarcodeParser()
    print(f"\n{'='*70}")
    print("BARCODE PARSER TEST (ECIA/ISO-IEC 15434 Standard)")
    print("Barcode Scanning GUI: Digi-Key and Mouser only")
    print(f"{'='*70}")

    for supplier_name, barcode in test_barcodes:
        result = parser.parse(barcode)
        print(f"\n{supplier_name}:")
        print(f"  Barcode (API lookup): {result.get('barcode', '(none)')}")
        print(f"  Supplier PN: {result.get('supplier_pn', '(none)')}")
        print(f"  Manufacturer PN: {result.get('manufacturer_pn', '(none)')}")
        print(f"  Quantity: {result.get('quantity', 0)}")

    print(f"\n{'='*70}\n")


if __name__ == '__main__':
    main()
