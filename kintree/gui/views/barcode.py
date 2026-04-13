"""Barcode scanner GUI view for rapid part import into InvenTree.

This module provides the main GUI interface for barcode scanning and bulk part
import. Users can scan multiple barcodes, configure import settings (category,
location, stock creation), and import all items to InvenTree in one operation.

Supports: LCSC (key-value), TME (key-value), Mouser (GS1-128), Digi-Key (GS1-128) barcodes.
"""

import flet as ft
import threading
import requests
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from ...common.tools import cprint
from ...database import inventree_interface
from ...search.barcode_parser import BarcodeParser
from .common import CommonView, DialogType, DropdownWithSearch, GUI_PARAMS
from .main import MainView


class BarcodeScannedRow:
    """Data container for parsed barcode and user configuration.

    Attributes:
        raw_barcode: Raw barcode text from scanner
        barcode: Normalized barcode value for API/linking (MFN PN)
        supplier: Detected supplier ('tme', 'mouser', 'digikey', 'lcsc')
        supplier_pn: Supplier-specific part number (empty for Mouser)
        manufacturer_pn: Standard manufacturer part number (used for search)
        search_name: Best available part number (mfn or supplier_pn)
        quantity: Quantity from barcode (default 1)
        barcode_value: Value to attach as barcode in InvenTree
        category: User-selected category for import
        location: User-selected stock location
        create_stock: Whether to create stock for this item
        stock_quantity: Quantity of stock to create
    """

    def __init__(self, barcode: str, parsed: Dict):
        """Initialize from raw barcode and parsed data.

        Args:
            barcode: Raw barcode string from scanner
            parsed: Normalized dict from BarcodeParser: {supplier, supplier_pn,
                    manufacturer_pn, quantity, ...}
        """
        self.raw_barcode = barcode
        self.parsed = parsed
        self.supplier = parsed.get('supplier', 'unknown')
        self.supplier_pn = parsed.get('supplier_pn', '')  # May be empty for Mouser
        self.manufacturer_pn = parsed.get('manufacturer_pn', '') or parsed.get('product_code', '') or parsed.get('digikey_pn', '')
        
        # Determine search term based on supplier type.
        # Mouser: search by MFN (no supplier PN available in QR).
        # TME, LCSC, Digi-Key: search by supplier PN (preferred identifier).
        self.search_name = self.manufacturer_pn or self.supplier_pn if self.supplier == 'mouser' else self.supplier_pn or self.manufacturer_pn

        # Match Assign display behavior: show resolved lookup for known suppliers.
        self.lookup_value = parsed.get('barcode', '') or self.manufacturer_pn or self.supplier_pn or barcode
        self.display_code = self.lookup_value if self.supplier != 'unknown' and self.lookup_value else barcode
        
        self.quantity = parsed.get('quantity', 1)
        
        # API barcode value must be the normalized parser output (MFN PN).
        # Fallbacks are only used if a barcode field is missing.
        self.barcode = parsed.get('barcode', '') or self.manufacturer_pn or self.supplier_pn
        self.barcode_value = self.barcode
        
        # Fields to be assigned in flow
        self.category = ''
        self.location = ''
        self.create_stock = False
        self.stock_quantity = self.quantity or 1

        # Assign-style validation state for live table feedback.
        self.status = 'Checking...'
        self.part_pk: Optional[int] = None
        self.part_name = ''
        self.default_location_pk = 0
        self.has_barcode = False
        self.barcode_hash = ''
        self.current_barcodes: List[str] = []
        raw_data = parsed.get('raw_data') if isinstance(parsed.get('raw_data'), dict) else {}
        order_number = str(
            parsed.get('order_number')
            or parsed.get('supplier_order_number')
            or parsed.get('customer_order_number')
            or raw_data.get('order_number')
            or raw_data.get('supplier_order_number')
            or raw_data.get('customer_order_number')
            or raw_data.get('po')
            or raw_data.get('on')
            or raw_data.get('cpo')
            or ''
        ).strip()

        # For TME, truncate at '/' (e.g., '34210324/5' -> '34210324')
        if self.supplier == 'tme' and order_number:
            order_number = order_number.split('/', 1)[0].strip()

        if not order_number:
            raw_barcode_upper = str(barcode or '').upper()
            ref_match = re.search(r'\b(?:CPO|PO|ON)\s*[:=]\s*([A-Z0-9\-_/\.]+)', raw_barcode_upper)
            if ref_match:
                order_number = ref_match.group(1).strip()

        self.order_number = order_number
        self.supplier_order_reference = order_number

        # User-edited overrides (None = use parsed value)
        self._edited_quantity: Optional[int] = None
        self._edited_order_number: Optional[str] = None

    @property
    def effective_quantity(self) -> int:
        if self._edited_quantity is not None:
            return self._edited_quantity
        return int(self.quantity or 1)

    @property
    def effective_order_number(self) -> str:
        if self._edited_order_number is not None:
            return self._edited_order_number
        return str(self.order_number or '')


class ExistingPartScanRow:
    """Data row for existing-part assignment workflow."""

    def __init__(self, row_id: int, raw_code: str, supplier: str, lookup_value: str):
        self.row_id = row_id
        self.raw_code = raw_code
        self.supplier = supplier
        self.lookup_value = lookup_value
        self.display_code = lookup_value if supplier != 'unknown' and lookup_value else raw_code
        self.status = 'Checking...'
        self.part_pk: Optional[int] = None
        self.part_name = ''
        self.default_location_pk = 0
        self.location = ''
        self.has_barcode = False
        self.current_barcodes: List[str] = []
        self.barcode_hash = ''


class _BarcodeApiHelpers:
    """Shared API helper methods used by Barcode and Assign workflows."""

    PO_SUPPLIER_NAME_MAP = {
        'digikey': ['Digi-Key'],
        'mouser': ['Mouser'],
        'tme': ['TME'],
        'lcsc': ['LCSC'],
    }

    @staticmethod
    def normalize_stock_location_value(location: str) -> str:
        return '/'.join(
            part.strip()
            for part in re.sub(r'^-+\s+', '', str(location or '').strip()).split('/')
            if part.strip()
        )

    @staticmethod
    def find_part_by_lookup(ctx, lookup_value: str) -> Optional[Dict]:
        cache_key = str(lookup_value or '').strip().lower()
        if not cache_key:
            return None

        with ctx._part_lookup_lock:
            if cache_key in ctx._part_lookup_cache:
                return ctx._part_lookup_cache[cache_key]

            wait_event = ctx._part_lookup_inflight.get(cache_key)
            is_leader = wait_event is None
            if is_leader:
                wait_event = threading.Event()
                ctx._part_lookup_inflight[cache_key] = wait_event

        if not is_leader:
            wait_event.wait(timeout=20.0)
            with ctx._part_lookup_lock:
                return ctx._part_lookup_cache.get(cache_key)

        result = None
        try:
            api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
            if not api_obj:
                result = None
            else:
                token = getattr(api_obj, 'token', None)
                base_url = getattr(api_obj, 'base_url', '')
                if not token or not base_url:
                    result = None
                else:
                    endpoint = f"{base_url.rstrip('/')}/api/part/"
                    headers = {
                        'Authorization': f'Token {token}',
                        'Accept': 'application/json',
                    }
                    response = ctx._request_with_retries(
                        method='GET',
                        url=endpoint,
                        headers=headers,
                        params={'search': lookup_value, 'limit': 5},
                        timeout=20,
                    )
                    if response is not None:
                        payload = response.json()

                        if isinstance(payload, dict):
                            rows = payload.get('results') or []
                        elif isinstance(payload, list):
                            rows = payload
                        else:
                            rows = []

                        if rows:
                            needle = lookup_value.strip().lower()
                            for candidate in rows:
                                ipn = str(candidate.get('IPN') or '').strip().lower()
                                name = str(candidate.get('name') or '').strip().lower()
                                if needle and (needle == ipn or needle == name):
                                    result = candidate
                                    break

                            if result is None:
                                # Avoid fuzzy fallback to an arbitrary first search hit,
                                # which can bind scans to a wrong, similarly named part.
                                result = None
        except Exception:
            result = None
        finally:
            with ctx._part_lookup_lock:
                # Do not cache misses forever; transient API failures would otherwise
                # require restarting the UI session to recover.
                if result is not None:
                    ctx._part_lookup_cache[cache_key] = result
                else:
                    ctx._part_lookup_cache.pop(cache_key, None)
                inflight_event = ctx._part_lookup_inflight.pop(cache_key, None)
                if inflight_event is not None:
                    inflight_event.set()

        return result

    @staticmethod
    def resolve_location_string(part: Dict) -> str:
        location_name = str(part.get('default_location_name') or '').strip()
        if location_name:
            return location_name

        location_id = part.get('default_location')
        if location_id in [None, '', 'None']:
            return '-'

        try:
            location_id = int(location_id)
        except (TypeError, ValueError):
            return str(location_id)

        try:
            location_tree = inventree_interface.inventree_api.get_stock_location_tree(location_id)
            names = [str(name) for name in reversed(list(location_tree.values())) if str(name).strip()]
            if names:
                return '/'.join(names)
        except Exception:
            pass

        return str(location_id)

    @staticmethod
    def fetch_part_barcodes(ctx, part_pk: int) -> List[str]:
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return []

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return []

        endpoint = f"{base_url.rstrip('/')}/api/barcode/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        if getattr(ctx, '_barcode_endpoint_available', None) is None:
            try:
                probe_response = ctx._http.get(
                    endpoint,
                    headers=headers,
                    params={'limit': 1},
                    timeout=6,
                )
                ctx._barcode_endpoint_available = probe_response.status_code not in [404, 405]
            except Exception:
                ctx._barcode_endpoint_available = False

        if not ctx._barcode_endpoint_available:
            return []

        def _extract_values(payload) -> List[str]:
            if isinstance(payload, dict):
                rows = payload.get('results') or []
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []

            values: List[str] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                barcode_value = item.get('data') or item.get('barcode') or item.get('value')
                if barcode_value:
                    values.append(str(barcode_value))
            return values

        try:
            response = ctx._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={'part': part_pk, 'limit': 100},
                timeout=20,
            )
            if response is None:
                return []
            values = _extract_values(response.json())
            if values:
                return values
        except Exception:
            pass

        try:
            response = ctx._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={'limit': 200},
                timeout=20,
            )
            if response is None:
                return []
            payload = response.json()
            if isinstance(payload, dict):
                rows = payload.get('results') or []
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []

            values: List[str] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                part_ref = item.get('part')
                item_part_pk = None
                if isinstance(part_ref, dict):
                    item_part_pk = part_ref.get('pk') or part_ref.get('id')
                else:
                    item_part_pk = part_ref

                try:
                    if int(item_part_pk) != int(part_pk):
                        continue
                except Exception:
                    continue

                barcode_value = item.get('data') or item.get('barcode') or item.get('value')
                if barcode_value:
                    values.append(str(barcode_value))
            return values
        except Exception:
            return []

    @staticmethod
    def set_part_default_location(ctx, part_pk: int, location_pk: int) -> bool:
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return False

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return False

        endpoint = f"{base_url.rstrip('/')}/api/part/{int(part_pk)}/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        response = ctx._request_with_retries(
            method='PATCH',
            url=endpoint,
            headers=headers,
            json={'default_location': int(location_pk)},
            timeout=20,
        )
        return response is not None and response.status_code in [200, 202]

    @staticmethod
    def link_part_barcode(ctx, part_pk: int, barcode_value: str) -> bool:
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return False

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return False

        endpoint = f"{base_url.rstrip('/')}/api/barcode/link/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        payload = {
            'barcode': str(barcode_value or '').strip(),
            'part': int(part_pk),
        }
        if not payload['barcode']:
            return False

        response = ctx._request_with_retries(
            method='POST',
            url=endpoint,
            headers=headers,
            json=payload,
            timeout=20,
        )
        return response is not None and response.status_code in [200, 201, 202]

    @staticmethod
    def collect_transfer_items_for_part(ctx, part_pk: int, location_pk: int) -> tuple[List[Dict], str]:
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return [], 'Missing InvenTree API object'

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return [], 'Missing InvenTree auth context'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        transfer_items: List[Dict] = []

        try:
            url = f"{base_url.rstrip('/')}/api/stock/"
            params = {'part': part_pk, 'limit': 250}

            while url:
                response = ctx._request_with_retries(
                    method='GET',
                    url=url,
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                if response is None:
                    return transfer_items, 'Failed to list stock items after retries'
                payload = response.json()

                if isinstance(payload, dict):
                    rows = payload.get('results') or []
                    next_url = payload.get('next')
                elif isinstance(payload, list):
                    rows = payload
                    next_url = None
                else:
                    rows = []
                    next_url = None

                for item in rows:
                    item_pk = item.get('pk') or item.get('id')
                    if not item_pk:
                        continue

                    current_loc = item.get('location')
                    try:
                        if current_loc is not None and int(current_loc) == int(location_pk):
                            continue
                    except Exception:
                        pass

                    try:
                        transfer_items.append(
                            {
                                'pk': int(item_pk),
                                'quantity': str(item.get('quantity') or '0'),
                                'batch': str(item.get('batch') or ''),
                                'packaging': str(item.get('packaging') or ''),
                                'status': int(item.get('status') or 0),
                            }
                        )
                    except Exception:
                        continue

                url = next_url
                params = {}

            return transfer_items, ''
        except Exception as exc:
            return transfer_items, str(exc)

    @staticmethod
    def transfer_stock_items_bulk(ctx, transfer_items: List[Dict], location_pk: int) -> tuple[bool, str]:
        if not transfer_items:
            return True, ''

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return False, 'Missing InvenTree API object'

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return False, 'Missing InvenTree auth context'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        transfer_endpoint = f"{base_url.rstrip('/')}/api/stock/transfer/"
        transfer_payload = {
            'items': transfer_items,
            'location': int(location_pk),
            'notes': 'Ki-nTree bulk location update',
        }
        transfer_response = ctx._request_with_retries(
            method='POST',
            url=transfer_endpoint,
            headers=headers,
            json=transfer_payload,
            timeout=45,
        )

        if transfer_response is not None and transfer_response.status_code in [200, 201, 202]:
            return True, ''

        return False, 'Bulk stock transfer request failed'

    @staticmethod
    def _get_auth_context() -> tuple[Any, Optional[str], str]:
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        return api_obj, token, base_url

    @staticmethod
    def _extract_rows(payload) -> List[Dict]:
        if isinstance(payload, dict):
            rows = payload.get('results') or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        return [item for item in rows if isinstance(item, dict)]

    @staticmethod
    def _extract_pk(entity: Optional[Dict]) -> int:
        if not isinstance(entity, dict):
            return 0
        value = entity.get('pk') or entity.get('id') or 0
        try:
            return int(value)
        except Exception:
            return 0

    @staticmethod
    def _match_reference(entity: Dict, reference: str) -> bool:
        probe = str(reference or '').strip().lower()
        if not probe:
            return False
        for key in ['reference', 'order_reference', 'external_reference', 'supplier_reference']:
            value = str(entity.get(key) or '').strip().lower()
            if value and value == probe:
                return True
        return False

    @staticmethod
    def _is_open_po_status(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in ['complete', 'completed', 'closed', 'cancelled', 'canceled']
        try:
            status_num = int(value)
            return status_num < 40
        except Exception:
            return True

    @staticmethod
    def _response_excerpt(response: Optional[requests.Response], limit: int = 220) -> str:
        if response is None:
            return 'no response'
        try:
            text = (response.text or '').strip().replace('\n', ' ')
        except Exception:
            text = ''
        if not text:
            return f'HTTP {response.status_code}'
        if len(text) > limit:
            text = text[:limit] + '...'
        return f'HTTP {response.status_code} body={text}'

    @staticmethod
    def _po_summary(po_data: Dict) -> str:
        po_pk = _BarcodeApiHelpers._extract_pk(po_data)
        ref = str(po_data.get('reference') or '').strip() or '-'
        supplier_ref = str(
            po_data.get('supplier_reference')
            or po_data.get('order_reference')
            or po_data.get('external_reference')
            or ''
        ).strip() or '-'
        status = str(po_data.get('status') or '-').strip()
        return f'pk={po_pk} ref={ref} supplier_ref={supplier_ref} status={status}'

    @staticmethod
    def list_open_purchase_orders(ctx, supplier_pk: int, limit: int = 50) -> List[Dict]:
        if supplier_pk <= 0:
            return []

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=False)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return []

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        response = ctx._request_with_retries(
            method='GET',
            url=endpoint,
            headers=headers,
            params={'supplier': int(supplier_pk), 'limit': int(max(1, limit))},
            timeout=20,
        )
        if response is None:
            return []

        rows = []
        for item in _BarcodeApiHelpers._extract_rows(response.json()):
            supplier_ref = item.get('supplier')
            if isinstance(supplier_ref, dict):
                item_supplier = _BarcodeApiHelpers._extract_pk(supplier_ref)
            else:
                try:
                    item_supplier = int(supplier_ref)
                except Exception:
                    item_supplier = 0

            if item_supplier != int(supplier_pk):
                continue
            if not _BarcodeApiHelpers._is_open_po_status(item.get('status')):
                continue
            rows.append(item)

        return rows

    @staticmethod
    def resolve_supplier_company_pk(ctx, supplier_key: str) -> int:
        supplier_norm = str(supplier_key or '').strip().lower()
        if not supplier_norm:
            return 0

        cache = getattr(ctx, '_supplier_company_cache', None)
        if isinstance(cache, dict) and supplier_norm in cache:
            cached_pk = int(cache.get(supplier_norm) or 0)
            if cached_pk > 0:
                try:
                    current_companies = inventree_interface.inventree_api.get_all_companies()
                    if cached_pk in current_companies.values():
                        cprint(f'[BARCODE][PO]\tSupplier cache hit validated: {supplier_norm} -> pk={cached_pk}', silent=False)
                        return cached_pk
                    cprint(f'[BARCODE][PO]\tSupplier cache stale: {supplier_norm} -> pk={cached_pk}', silent=False)
                except Exception as exc:
                    cprint(f'[BARCODE][PO]\tSupplier cache validation failed for {supplier_norm}: {str(exc)[:80]}', silent=False)

        api_obj, token, base_url = _BarcodeApiHelpers._get_auth_context()
        if not api_obj or not token or not base_url:
            return 0

        company_map: Dict[str, int] = {}
        try:
            company_map = inventree_interface.inventree_api.get_all_companies() or {}
        except Exception:
            company_map = {}

        if not company_map:
            return 0

        aliases = _BarcodeApiHelpers.PO_SUPPLIER_NAME_MAP.get(supplier_norm, [])
        probe_names = [supplier_key, supplier_norm.upper(), supplier_norm.title(), *aliases]
        probe_names = [str(name).strip() for name in probe_names if str(name).strip()]

        def _pick_company(probe: str) -> int:
            probe_lower = str(probe or '').strip().lower()
            if not probe_lower:
                return 0

            exact_matches = []
            contains_matches = []
            for company_name, company_pk in company_map.items():
                company_lower = str(company_name or '').strip().lower()
                if not company_lower:
                    continue
                if company_lower == probe_lower:
                    exact_matches.append((company_name, int(company_pk or 0)))
                elif probe_lower in company_lower:
                    contains_matches.append((company_name, int(company_pk or 0)))

            if exact_matches:
                company_name, company_pk = exact_matches[0]
                cprint(f'[BARCODE][PO]\tSupplier exact match: {probe} -> {company_name} pk={company_pk}', silent=False)
                return company_pk

            if contains_matches:
                contains_matches.sort(key=lambda item: (len(str(item[0] or '')), str(item[0] or '').lower()))
                company_name, company_pk = contains_matches[0]
                cprint(f'[BARCODE][PO]\tSupplier contains match: {probe} -> {company_name} pk={company_pk}', silent=False)
                return company_pk

            return 0

        supplier_pk = 0
        for probe in probe_names:
            supplier_pk = _pick_company(probe)
            if supplier_pk > 0:
                break

        if supplier_pk <= 0:
            sample_names = ', '.join(list(company_map.keys())[:12])
            cprint(
                f'[BARCODE][PO]\tNo supplier company match for {supplier_norm}; available companies sample: {sample_names}',
                silent=False,
            )

        if isinstance(cache, dict):
            cache[supplier_norm] = supplier_pk

        return supplier_pk

    @staticmethod
    def _pick_po_endpoint(ctx, for_lines: bool = False) -> str:
        cache = getattr(ctx, '_po_endpoint_cache', None)
        cache_key = 'po_line_list' if for_lines else 'po_list'
        if isinstance(cache, dict) and cache.get(cache_key):
            return str(cache[cache_key])

        api_obj, token, base_url = _BarcodeApiHelpers._get_auth_context()
        if not api_obj or not token or not base_url:
            return ''

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        candidates = ['/api/order/po-line/', '/api/order/purchase-line/'] if for_lines else ['/api/order/po/', '/api/order/purchase/']

        for path in candidates:
            endpoint = f"{base_url.rstrip('/')}{path}"
            response = ctx._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={'limit': 1},
                timeout=10,
            )
            if response is not None:
                if isinstance(cache, dict):
                    cache[cache_key] = endpoint
                cprint(f"[BARCODE][PO]\tResolved {'line' if for_lines else 'header'} endpoint: {endpoint}", silent=False)
                return endpoint

        cprint(
            f"[BARCODE][PO]\tFailed to resolve {'line' if for_lines else 'header'} endpoint from candidates={candidates}",
            silent=False,
        )
        return ''

    @staticmethod
    def find_open_purchase_order(ctx, supplier_pk: int, reference: str) -> Optional[Dict]:
        if supplier_pk <= 0 or not str(reference or '').strip():
            return None

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=False)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return None

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        response = ctx._request_with_retries(
            method='GET',
            url=endpoint,
            headers=headers,
            params={'supplier': int(supplier_pk), 'search': reference, 'limit': 25},
            timeout=20,
        )
        if response is None:
            return None

        for item in _BarcodeApiHelpers._extract_rows(response.json()):
            supplier_ref = item.get('supplier')
            if isinstance(supplier_ref, dict):
                item_supplier = _BarcodeApiHelpers._extract_pk(supplier_ref)
            else:
                try:
                    item_supplier = int(supplier_ref)
                except Exception:
                    item_supplier = 0

            if item_supplier != int(supplier_pk):
                continue
            if not _BarcodeApiHelpers._match_reference(item, reference):
                continue
            if not _BarcodeApiHelpers._is_open_po_status(item.get('status')):
                continue
            return item

        return None

    @staticmethod
    def get_po_location_pk(po_data: Optional[Dict]) -> int:
        if not isinstance(po_data, dict):
            return 0
        for key in ['destination', 'location', 'target_location']:
            value = po_data.get(key)
            if isinstance(value, dict):
                pk = _BarcodeApiHelpers._extract_pk(value)
            else:
                try:
                    pk = int(value)
                except Exception:
                    pk = 0
            if pk > 0:
                return pk
        return 0

    @staticmethod
    def create_purchase_order(ctx, supplier_pk: int, reference: str, location_pk: int = 0) -> tuple[Optional[Dict], str]:
        if supplier_pk <= 0 or not str(reference or '').strip():
            return None, 'Invalid supplier or order reference'

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=False)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return None, 'Purchase order endpoint unavailable'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        supplier_reference = str(reference).strip()

        base_payload = {
            'supplier': int(supplier_pk),
            'supplier_reference': supplier_reference,
            'description': 'Ki-nTree automatic creation',
        }
        payloads = [dict(base_payload)]
        if location_pk > 0:
            payloads.append({**base_payload, 'destination': int(location_pk)})
            payloads.append({**base_payload, 'location': int(location_pk)})
        payloads.append({**base_payload, 'supplier_name': supplier_reference})

        last_error = 'Failed to create purchase order'
        cprint(
            f'[BARCODE][PO]\tCreate PO: endpoint={endpoint} supplier_pk={supplier_pk} supplier_ref={supplier_reference} location_pk={location_pk}',
            silent=False,
        )
        for payload_index, payload in enumerate(payloads, start=1):
            response = None
            cprint(
                f"[BARCODE][PO]\tCreate PO attempt {payload_index}/{len(payloads)} payload={payload}",
                silent=False,
            )
            for request_try in range(1, 4):
                try:
                    response = ctx._http.request(
                        method='POST',
                        url=endpoint,
                        headers=headers,
                        json=payload,
                        timeout=25,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.HTTPError(f'HTTP {response.status_code}', response=response)

                    if response.status_code in [200, 201, 202]:
                        cprint(
                            f'[BARCODE][PO]\tCreate PO success via payload {payload_index} (HTTP {response.status_code})',
                            silent=False,
                        )
                        try:
                            created = response.json()
                            if isinstance(created, dict):
                                return created, ''
                        except Exception:
                            pass
                        return {'pk': 0}, ''

                    # Keep client errors for diagnostics and stop retrying this payload.
                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        break
                except requests.RequestException as exc:
                    response = getattr(exc, 'response', None)
                    if request_try < 3:
                        time.sleep(1.0)
                    else:
                        break

            detail = _BarcodeApiHelpers._response_excerpt(response)
            cprint(f'[BARCODE][PO]\tCreate PO attempt {payload_index} failed: {detail}', silent=False)
            last_error = f'PO create failed ({detail})'

        return None, last_error

    @staticmethod
    def find_po_line_for_part(ctx, po_pk: int, part_pk: int) -> Optional[Dict]:
        if po_pk <= 0 or part_pk <= 0:
            return None

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=True)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return None

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        for order_key in ['order', 'purchase_order', 'po']:
            response = ctx._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={order_key: int(po_pk), 'part': int(part_pk), 'limit': 100},
                timeout=20,
            )
            if response is None:
                continue

            for item in _BarcodeApiHelpers._extract_rows(response.json()):
                # Hard guard: never reuse a line from another PO.
                line_order_ref = item.get('order') or item.get('purchase_order') or item.get('po')
                if isinstance(line_order_ref, dict):
                    line_po_pk = _BarcodeApiHelpers._extract_pk(line_order_ref)
                else:
                    try:
                        line_po_pk = int(line_order_ref)
                    except Exception:
                        line_po_pk = 0
                if line_po_pk and int(line_po_pk) != int(po_pk):
                    continue

                part_ref = item.get('part') or item.get('part_detail')
                if isinstance(part_ref, dict):
                    item_part = _BarcodeApiHelpers._extract_pk(part_ref)
                else:
                    try:
                        item_part = int(part_ref)
                    except Exception:
                        item_part = 0
                if item_part == int(part_pk):
                    cprint(
                        f'[BARCODE][PO]\tMatched existing line line={_BarcodeApiHelpers._extract_pk(item)} po={po_pk} supplier_part={part_pk}',
                        silent=False,
                    )
                    return item

        return None

    @staticmethod
    def find_po_line_for_internal_part(ctx, po_pk: int, internal_part_pk: int) -> Optional[Dict]:
        if po_pk <= 0 or internal_part_pk <= 0:
            return None

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=True)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return None

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        for order_key in ['order', 'purchase_order', 'po']:
            response = ctx._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={order_key: int(po_pk), 'limit': 100},
                timeout=20,
            )
            if response is None:
                continue

            for item in _BarcodeApiHelpers._extract_rows(response.json()):
                line_order_ref = item.get('order') or item.get('purchase_order') or item.get('po')
                if isinstance(line_order_ref, dict):
                    line_po_pk = _BarcodeApiHelpers._extract_pk(line_order_ref)
                else:
                    try:
                        line_po_pk = int(line_order_ref)
                    except Exception:
                        line_po_pk = 0
                if line_po_pk and int(line_po_pk) != int(po_pk):
                    continue

                item_internal_part = 0
                part_detail = item.get('part_detail')
                if isinstance(part_detail, dict):
                    raw_internal = part_detail.get('part') or part_detail.get('part_pk') or part_detail.get('part_id')
                    if isinstance(raw_internal, dict):
                        item_internal_part = _BarcodeApiHelpers._extract_pk(raw_internal)
                    else:
                        try:
                            item_internal_part = int(raw_internal)
                        except Exception:
                            item_internal_part = 0

                if item_internal_part == int(internal_part_pk):
                    cprint(
                        f'[BARCODE][PO]\tMatched existing line by internal part line={_BarcodeApiHelpers._extract_pk(item)} po={po_pk} part_pk={internal_part_pk}',
                        silent=False,
                    )
                    return item

        return None

    @staticmethod
    def create_po_line(ctx, po_pk: int, part_pk: int, quantity: int) -> tuple[Optional[Dict], str]:
        if po_pk <= 0 or part_pk <= 0:
            return None, 'Invalid PO or part ID'

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=True)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return None, 'Purchase order line endpoint unavailable'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        qty = max(1, int(quantity or 1))
        payloads = [
            {'order': int(po_pk), 'part': int(part_pk), 'quantity': qty},
            {'purchase_order': int(po_pk), 'part': int(part_pk), 'quantity': qty},
            {'po': int(po_pk), 'part': int(part_pk), 'quantity': qty},
            {'order': int(po_pk), 'part': int(part_pk), 'target_quantity': qty},
            {'purchase_order': int(po_pk), 'part': int(part_pk), 'target_quantity': qty},
            {'po': int(po_pk), 'part': int(part_pk), 'target_quantity': qty},
        ]

        cprint(f'[BARCODE][PO]\tCreate line: endpoint={endpoint} po={po_pk} part={part_pk} qty={qty}', silent=False)

        for payload_index, payload in enumerate(payloads, start=1):
            response = None
            cprint(f'[BARCODE][PO]\tCreate line attempt {payload_index}/{len(payloads)} payload={payload}', silent=False)
            for request_try in range(1, 4):
                try:
                    response = ctx._http.request(
                        method='POST',
                        url=endpoint,
                        headers=headers,
                        json=payload,
                        timeout=25,
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.HTTPError(f'HTTP {response.status_code}', response=response)

                    if response.status_code in [200, 201, 202]:
                        cprint(
                            f'[BARCODE][PO]\tCreate line success via payload {payload_index} (HTTP {response.status_code})',
                            silent=False,
                        )
                        try:
                            created = response.json()
                            if isinstance(created, dict):
                                return created, ''
                        except Exception:
                            pass
                        return {'pk': 0, 'part': int(part_pk)}, ''

                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        break
                except requests.RequestException as exc:
                    response = getattr(exc, 'response', None)
                    if request_try < 3:
                        time.sleep(1.0)
                    else:
                        break

            detail = _BarcodeApiHelpers._response_excerpt(response)
            cprint(f'[BARCODE][PO]\tCreate line attempt {payload_index} failed: {detail}', silent=False)

        return None, 'Failed to create PO line item'

    @staticmethod
    def get_po_line_quantities(line_data: Optional[Dict]) -> tuple[int, int]:
        if not isinstance(line_data, dict):
            return 0, 0

        ordered = 0
        for key in ['quantity', 'target_quantity', 'ordered', 'ordered_quantity']:
            try:
                value = int(float(line_data.get(key) or 0))
                if value > ordered:
                    ordered = value
            except Exception:
                continue

        received = 0
        for key in ['received', 'received_quantity', 'received_qty']:
            try:
                value = int(float(line_data.get(key) or 0))
                if value > received:
                    received = value
            except Exception:
                continue

        return ordered, received

    @staticmethod
    def update_po_line_quantity(ctx, line_pk: int, quantity: int) -> tuple[bool, str]:
        if line_pk <= 0:
            return False, 'Invalid PO line ID'

        endpoint = _BarcodeApiHelpers._pick_po_endpoint(ctx, for_lines=True)
        api_obj, token, _ = _BarcodeApiHelpers._get_auth_context()
        if not endpoint or not api_obj or not token:
            return False, 'Purchase order line endpoint unavailable'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        qty = max(1, int(quantity or 1))
        line_endpoint = f"{endpoint.rstrip('/')}/{int(line_pk)}/"
        payloads = [
            {'quantity': qty},
            {'target_quantity': qty},
            {'quantity': qty, 'target_quantity': qty},
        ]

        cprint(f'[BARCODE][PO]\tUpdate line: endpoint={line_endpoint} line={line_pk} qty={qty}', silent=False)

        for payload in payloads:
            response = ctx._request_with_retries(
                method='PATCH',
                url=line_endpoint,
                headers=headers,
                json=payload,
                timeout=25,
            )
            if response is not None and response.status_code in [200, 202]:
                return True, ''

            if response is not None:
                cprint(
                    f"[BARCODE][PO]\tUpdate line attempt failed payload={payload}: {_BarcodeApiHelpers._response_excerpt(response)}",
                    silent=False,
                )

        return False, 'Failed to update PO line quantity'

    @staticmethod
    def receive_po_items(ctx, po_pk: int, items: List[Dict], location_pk: int = 0) -> tuple[bool, str]:
        if po_pk <= 0:
            return False, 'Invalid PO ID'

        api_obj, token, base_url = _BarcodeApiHelpers._get_auth_context()
        if not api_obj or not token or not base_url:
            return False, 'Missing InvenTree auth context'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        normalized_items: List[Dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            line_item = int(item.get('line_item') or item.get('line') or item.get('pk') or 0)
            qty = max(1, int(item.get('quantity') or 1))
            payload_item = {
                'line_item': line_item,
                'quantity': qty,
            }
            if location_pk > 0:
                payload_item['location'] = int(location_pk)
            if item.get('packaging'):
                payload_item['packaging'] = str(item.get('packaging'))
            if item.get('batch_code'):
                payload_item['batch_code'] = str(item.get('batch_code'))
            normalized_items.append(payload_item)

        if not normalized_items:
            return False, 'No PO items to receive'

        endpoint_candidates = [
            f"{base_url.rstrip('/')}/api/order/po/{int(po_pk)}/receive/",
            f"{base_url.rstrip('/')}/api/order/purchase/{int(po_pk)}/receive/",
        ]

        payloads = [
            {'items': normalized_items, 'location': int(location_pk)} if location_pk > 0 else {'items': normalized_items},
        ]

        for endpoint in endpoint_candidates:
            for payload in payloads:
                cprint(f'[BARCODE][PO]\tReceive PO attempt endpoint={endpoint} payload={payload}', silent=False)
                response = ctx._request_with_retries(
                    method='POST',
                    url=endpoint,
                    headers=headers,
                    json=payload,
                    timeout=45,
                )
                if response is None:
                    continue
                if response.status_code in [200, 201, 202]:
                    return True, ''
                cprint(f'[BARCODE][PO]\tReceive PO failed: {_BarcodeApiHelpers._response_excerpt(response)}', silent=False)

        return False, 'Failed to receive PO items'

    @staticmethod
    def trigger_po_action(ctx, po_pk: int, action: str) -> bool:
        if po_pk <= 0 or not str(action or '').strip():
            return False

        api_obj, token, base_url = _BarcodeApiHelpers._get_auth_context()
        if not api_obj or not token or not base_url:
            return False

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        endpoint_candidates = [
            f"{base_url.rstrip('/')}/api/order/po/{int(po_pk)}/{action}/",
            f"{base_url.rstrip('/')}/api/order/purchase/{int(po_pk)}/{action}/",
        ]

        for endpoint in endpoint_candidates:
            response = ctx._request_with_retries(
                method='POST',
                url=endpoint,
                headers=headers,
                json={},
                timeout=20,
            )
            if response is not None and response.status_code in [200, 201, 202, 204]:
                return True

        return False


class BarcodeImportView(MainView):
    """Barcode scanner and import view for Ki-nTree.

    Main GUI for rapid part import workflow:
    1. User scans/pastes barcodes (auto-detects supplier format)
    2. Reviews parsed items in table with preview of part numbers
    3. Configures category, location, stock settings
    4. Imports all items to InvenTree with optional stock creation

    Inherits from MainView for consistent navigation and layout.
    Reuses DropdownWithSearch for category/location with text input support.
    Integrates with inventree_interface for part creation and barcode assignment.

    Attributes:
        title: Display name for navigation rail
        fields: Dict of GUI controls (inherited pattern from MainView)
        parser: BarcodeParser instance for barcode detection/normalization
        scanned_rows: List of BarcodeScannedRow objects
        categories: Cached category tree from InvenTree
        stock_locations: Cached stock location tree from InvenTree
    """

    title = 'Barcode'
    fields = {}
    
    def __init__(self, page: ft.Page):
        """Initialize barcode import view.

        Args:
            page: Flet page object for GUI rendering
        """
        # Initialize instance attributes before super().__init__()
        self.parser = BarcodeParser()
        self.scanned_rows: List[BarcodeScannedRow] = []
        self.categories = {}
        self.stock_locations = {}
        self.stock_location_id_map = {}
        self._location_pk_to_path_cache: Dict[int, str] = {}
        self._last_scan_code = ''
        self._last_scan_ts = 0.0
        self._recent_scan_codes: Dict[str, float] = {}
        # Reuse one HTTP session for lookup requests, same as Assign view.
        self._http = requests.Session()
        self._barcode_endpoint_available: Optional[bool] = None
        self._connect_lock = threading.Lock()
        self._part_create_lock = threading.Lock()
        self._part_lookup_cache: Dict[str, Optional[Dict]] = {}
        self._part_lookup_lock = threading.Lock()
        self._part_lookup_inflight: Dict[str, threading.Event] = {}
        self._po_endpoint_cache: Dict[str, str] = {}
        self._supplier_company_cache: Dict[str, int] = {}

        # Call parent init
        super().__init__(page=page)

        # Build the UI
        self.build_page()

    def build_page(self) -> None:
        """Build the barcode import UI with all controls and layout.

        Creates 4-section layout:
        1. Input: Text field for scanning/pasting + Parse/Clear buttons
        2. Preview: DataTable showing parsed items with supplier/part info
        3. Config: Category and location dropdowns (searchable)
        4. Action: Import button + status message
        """
        
        # Input field for barcode scanning
        self.fields['barcode_input'] = ft.TextField(
            label='Scan barcode or paste text',
            multiline=True,
            min_lines=3,
            max_lines=6,
            on_submit=self._on_barcode_scanned,
            on_change=self._on_barcode_input_changed,
            autofocus=True,
            hint_text='Scan QR/barcode code or paste multiple codes (one per line)',
        )
        
        # Clear button
        self.fields['barcode_clear'] = ft.ElevatedButton(
            text='Clear Input',
            on_click=self._on_clear_input,
        )
        
        # Parse button (for multi-line pastes)
        self.fields['barcode_parse'] = ft.ElevatedButton(
            text='Parse Barcodes',
            on_click=self._parse_batch_barcodes,
        )

        self.fields['clear_all_rows'] = ft.IconButton(
            icon=ft.icons.DELETE_SWEEP,
            tooltip='Clear all scanned items',
            on_click=self._clear_all_rows,
        )
        
        # Results table
        self.fields['results_table'] = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('Input Code')),
                ft.DataColumn(ft.Text('Supplier')),
                ft.DataColumn(ft.Text('Order Number')),
                ft.DataColumn(ft.Text('Status')),
                ft.DataColumn(ft.Text('Part')),
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Barcode')),
                ft.DataColumn(ft.Text('Qty')),
                ft.DataColumn(ft.Text('Category')),
                ft.DataColumn(ft.Text('Create Stock')),
                ft.DataColumn(ft.Text('Remove')),
            ],
            rows=[],
            horizontal_lines=ft.border.BorderSide(1, ft.colors.OUTLINE),
            column_spacing=12,
            horizontal_margin=8,
        )
        
        # Category search/dropdown control (scan-friendly)
        self.fields['category_select'] = DropdownWithSearch(
            label='Category (All Items)',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_category_changed,
            on_submit=self.focus_barcode_input,
        )

        # Location search/dropdown control (scan-friendly)
        self.fields['location_select'] = DropdownWithSearch(
            label='Stock Location (All Items)',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_location_changed,
            on_submit=self.focus_barcode_input,
        )

        self.fields['reload_categories'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload categories from InvenTree',
            on_click=self._reload_categories,
        )

        self.fields['clear_category'] = ft.IconButton(
            icon=ft.icons.CLEAR,
            tooltip='Clear global category selection',
            on_click=self._clear_category,
        )

        self.fields['reload_locations'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload stock locations from InvenTree',
            on_click=self._reload_locations,
        )

        self.fields['clear_location'] = ft.IconButton(
            icon=ft.icons.CLEAR,
            tooltip='Clear global location selection',
            on_click=self._clear_location,
        )
        
        # Create stock checkbox
        self.fields['create_stock_check'] = ft.Checkbox(
            label='Create Stock for All Items',
            value=False,
            on_change=self._on_create_stock_changed,
        )

        self.fields['use_manufacturer_barcode_check'] = ft.Checkbox(
            label='Use manufacturer PN as barcode',
            value=True,
            on_change=lambda _: (self._update_results_table(), self.focus_barcode_input()),
        )

        self.fields['force_barcode_reassign_check'] = ft.Checkbox(
            label='Force barcode reassignments (overwrite existing)',
            value=False,
            on_change=lambda _: self.focus_barcode_input(),
        )

        self.fields['assign_all_stock_items_location_check'] = ft.Checkbox(
            label='Assign selected location to all stock items of the part',
            value=True,
            on_change=lambda _: self.focus_barcode_input(),
        )

        self.fields['po_flow_check'] = ft.Checkbox(
            label='Process Purchase Orders from supplier order references',
            value=False,
            on_change=self._on_po_flow_changed,
        )
        
        # Submit button — starts disabled; enabled once every row has a category.
        self.fields['barcode_submit'] = ft.ElevatedButton(
            text='Import All',
            on_click=self._on_submit,
            color='white',
            bgcolor='green',
            width=200,
            disabled=True,
        )
        
        # Status message
        self.fields['status_message'] = ft.Text(value='Ready to scan', size=12, color='blue')
        self.fields['import_progress'] = ft.ProgressBar(value=0, visible=False, height=8)
        self.fields['import_progress_message'] = ft.Text(value='', size=11, color='blue')
        
        # Build layout - set self.column instead of self.page.content.
        # GestureDetector catches taps on blank space and refocuses the barcode
        # input. Child controls consume their own taps first, so interactive
        # widgets (text fields, dropdowns, buttons) are unaffected.
        self.column = ft.Column(
            controls=[
                ft.GestureDetector(
                    on_tap=self.focus_barcode_input,
                    expand=True,
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                            ft.Row([ft.Text('Barcode Import', style=ft.TextThemeStyle.HEADLINE_MEDIUM)]),
                            ft.Divider(),
                            
                            # Input section
                            ft.Text('1. Scan or Paste Barcodes:', style=ft.TextThemeStyle.BODY_LARGE),
                            self.fields['barcode_input'],
                            ft.Row([
                                self.fields['barcode_parse'],
                                self.fields['barcode_clear'],
                                self.fields['clear_all_rows'],
                            ]),
                            
                            ft.Divider(),
                            
                            # Results section
                            ft.Text('2. Review Scanned Items:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Container(
                                content=self.fields['results_table'],
                                expand=True,
                            ),
                            
                            ft.Divider(),
                            
                            # Settings section
                            ft.Text('3. Configure Import:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Column(
                                controls=[
                                    ft.Row([
                                        self.fields['category_select'],
                                        self.fields['reload_categories'],
                                        self.fields['clear_category'],
                                    ]),
                                    ft.Row([
                                        self.fields['location_select'],
                                        self.fields['reload_locations'],
                                        self.fields['clear_location'],
                                    ]),
                                ],
                                spacing=8,
                            ),
                            self.fields['create_stock_check'],
                            
                            ft.Text('Barcode & Existing Part Options:', style=ft.TextThemeStyle.BODY_MEDIUM),
                            self.fields['use_manufacturer_barcode_check'],
                            self.fields['force_barcode_reassign_check'],
                            
                            ft.Text('Stock Item Location:', style=ft.TextThemeStyle.BODY_MEDIUM),
                            self.fields['assign_all_stock_items_location_check'],

                            ft.Text('Purchase Order Flow:', style=ft.TextThemeStyle.BODY_MEDIUM),
                            self.fields['po_flow_check'],
                            
                            ft.Divider(),
                            
                            # Action buttons
                            ft.Row([
                                self.fields['barcode_submit'],
                                ft.ElevatedButton(
                                    text='Back',
                                    on_click=lambda _: self._page.go('/main/create'),
                                ),
                            ]),
                            
                            self.fields['import_progress'],
                            self.fields['import_progress_message'],
                            self.fields['status_message'],
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=10,
                    ),
                    padding=20,
                    expand=True,
                ),
                ),
            ],
            expand=True,
        )

        self.focus_barcode_input()

    def did_mount(self):
        self._load_categories_and_locations()
        self.focus_barcode_input()
        return super().did_mount()

    def focus_barcode_input(self, *_, **__):
        """Focus scanner input so cursor is ready when entering this page."""
        try:
            self.fields['barcode_input'].focus()
            self.fields['barcode_input'].update()
        except AssertionError:
            # Control may not be mounted yet; caller can try again later.
            pass
    
    def _load_categories_and_locations(self):
        """Load available categories and stock locations."""
        try:
            ts_load = time.perf_counter()
            category_list = inventree_interface.build_category_tree(reload=False)

            location_list = inventree_interface.build_stock_location_tree(reload=False)

            # Keep full path->pk map lazy to avoid expensive startup fetches.
            self.stock_location_id_map = {}

            category_options = [ft.dropdown.Option(category) for category in category_list]
            location_options = [ft.dropdown.Option(location) for location in location_list]

            self.categories = category_list
            self.stock_locations = location_list
            self.fields['category_select'].options = category_options
            self.fields['location_select'].options = location_options

            # Ensure dropdown wrappers are interactive immediately after mount.
            self.fields['category_select'].disabled = False
            self.fields['location_select'].disabled = False
            self.fields['category_select'].done_search()
            self.fields['location_select'].done_search()
            try:
                self.fields['category_select'].update()
                self.fields['location_select'].update()
            except AssertionError:
                pass

            elapsed_load = (time.perf_counter() - ts_load) * 1000.0
            cprint(
                f'[BARCODE]\tLoaded category/location data ({elapsed_load:.1f} ms, locations={len(location_list)})',
                silent=False,
            )
            
            self._page.update()
        except Exception as e:
            cprint(f'[ERROR] Failed to load categories/locations: {e}', silent=False)

    def _reload_categories(self, _):
        """Reload the category tree from InvenTree."""
        try:
            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
                self.show_dialog(DialogType.ERROR, 'ERROR: Failed to connect to InvenTree server')
                return

            category_list = inventree_interface.build_category_tree(reload=True)
            self.fields['category_select'].options = [ft.dropdown.Option(category) for category in category_list]
            self._page.update()
            self._show_status('Categories reloaded', color='green')
        except Exception as e:
            cprint(f'[ERROR] Failed to reload categories: {e}', silent=False)

    def _reload_locations(self, _):
        """Reload the stock location tree from InvenTree."""
        try:
            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
                self.show_dialog(DialogType.ERROR, 'ERROR: Failed to connect to InvenTree server')
                return

            ts_reload = time.perf_counter()
            location_list = inventree_interface.build_stock_location_tree(reload=True)
            # Drop stale map after reload; it will be rebuilt lazily on first lookup.
            self.stock_location_id_map = {}
            self.fields['location_select'].options = [ft.dropdown.Option(location) for location in location_list]
            self._page.update()
            elapsed_reload = (time.perf_counter() - ts_reload) * 1000.0
            cprint(
                f'[BARCODE]\tReloaded locations ({elapsed_reload:.1f} ms, locations={len(location_list)})',
                silent=False,
            )
            self._show_status('Stock locations reloaded', color='green')
        except Exception as e:
            cprint(f'[ERROR] Failed to reload stock locations: {e}', silent=False)
    
    def _on_barcode_scanned(self, _):
        """Called when user scans a single barcode (Enter in input field)."""
        barcode = self.fields['barcode_input'].value.strip()
        if not barcode:
            return

        if self._append_parsed_barcode(barcode):
            self.fields['barcode_input'].value = ''
            try:
                self.fields['barcode_input'].update()
                self.fields['barcode_input'].focus()
            except AssertionError:
                pass

    def _on_barcode_input_changed(self, _):
        """Auto-parse scanner input when newline terminator is received."""
        text = self.fields['barcode_input'].value or ''
        normalized_text = text.replace('\r', '\n')

        if '\n' not in normalized_text:
            return

        # Only parse completed, newline-terminated lines.
        # Keep the trailing fragment in the field to avoid parsing partial scans.
        has_trailing_newline = normalized_text.endswith('\n')
        raw_lines = normalized_text.split('\n')
        completed_lines = raw_lines if has_trailing_newline else raw_lines[:-1]
        pending_tail = '' if has_trailing_newline else (raw_lines[-1] if raw_lines else '')

        lines = [line.strip() for line in completed_lines if line.strip()]
        if not lines:
            self.fields['barcode_input'].value = pending_tail
            try:
                self.fields['barcode_input'].update()
                self.fields['barcode_input'].focus()
            except AssertionError:
                pass
            return

        success = 0
        failed = 0
        for line in lines:
            if self._append_parsed_barcode(line, update_table=False, update_status=False):
                success += 1
            else:
                failed += 1

        self._update_results_table()
        self.fields['barcode_input'].value = pending_tail
        try:
            self.fields['barcode_input'].update()
            self.fields['barcode_input'].focus()
        except AssertionError:
            pass

        if success:
            self._show_status(
                f'Scanned {success} item(s)' + (f' ({failed} failed)' if failed else ''),
                color='green' if failed == 0 else 'orange',
            )
            if failed:
                self.show_dialog(DialogType.ERROR, f'Unrecognized barcode format for {failed} item(s)')
        else:
            self._show_status('Unknown barcode format', color='red')
            self.show_dialog(DialogType.ERROR, 'Unrecognized barcode format')

    def _append_parsed_barcode(self, barcode: str, update_table: bool = True, update_status: bool = True) -> bool:
        """Parse and append one barcode row without removing existing entries."""
        normalized = str(barcode or '').strip()
        if not normalized:
            return False

        now = time.monotonic()
        recent_ts = self._recent_scan_codes.get(normalized)
        if recent_ts is not None and (now - recent_ts) < 1.5:
            delta_ms = (now - recent_ts) * 1000.0
            cprint(
                f'[BARCODE]\tIgnored duplicate scan ({delta_ms:.1f} ms since previous, len={len(normalized)}): {normalized}',
                silent=False,
            )
            return False

        self._recent_scan_codes[normalized] = now
        self._last_scan_code = normalized
        self._last_scan_ts = now

        parsed = self.parser.parse(normalized)
        if parsed.get('supplier') == 'unknown':
            # Accept raw codes so existing parts can still be resolved server-side.
            parsed = {
                'supplier': 'unknown',
                'supplier_pn': normalized,
                'manufacturer_pn': normalized,
                'barcode': normalized,
                'quantity': 1,
            }

        row = BarcodeScannedRow(normalized, parsed)
        self.scanned_rows.append(row)

        # Run Assign-style resolution checks in background for live status updates.
        thread = threading.Thread(target=self._validate_import_row_async, args=(row,), daemon=True)
        thread.start()

        if update_table:
            self._update_results_table()
        if update_status:
            self._show_status(f'Scanned: {row.supplier.upper()} - {row.search_name}', color='green')
        return True
    
    def _parse_batch_barcodes(self, _):
        """Parse multiple barcodes from paste input."""
        text = self.fields['barcode_input'].value.strip()
        if not text:
            self._show_status('No input provided', color='red')
            return
        
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        success = 0
        failed = 0
        for line in lines:
            if not self._append_parsed_barcode(line, update_table=False, update_status=False):
                failed += 1
                continue
            success += 1
        
        self._update_results_table()
        self.fields['barcode_input'].value = ''
        self.fields['barcode_input'].update()
        self._show_status(f'Parsed {success} items ({failed} failed)', color='green' if success > 0 else 'red')
        if failed:
            self.show_dialog(DialogType.ERROR, f'Unrecognized barcode format for {failed} item(s)')
        self.focus_barcode_input()
    
    def _update_results_table(self):
        """Refresh the results table with current scanned items."""
        rows = []

        for idx, row in enumerate(self.scanned_rows):
            remove_btn = ft.IconButton(
                icon=ft.icons.DELETE,
                on_click=lambda _, i=idx: self._remove_row(i),
            )

            input_code = self._truncate_text(str(getattr(row, 'display_code', '') or row.raw_barcode or '-'), max_len=70)
            status_text = str(getattr(row, 'status', 'Queued') or 'Queued')
            part_display = str(getattr(row, 'part_name', '') or '').strip()
            part_pk = int(getattr(row, 'part_pk', 0) or 0)
            if part_display and part_pk > 0:
                part_text = f'{part_display} ({part_pk})'
            elif part_display:
                part_text = part_display
            elif part_pk > 0:
                part_text = f'Part ({part_pk})'
            else:
                if str(getattr(row, 'status', '')).lower().startswith('checking'):
                    part_text = '(resolving...)'
                else:
                    part_text = str(row.search_name or row.manufacturer_pn or row.supplier_pn or '-')
            location_text = self._truncate_text(str(row.location or '(default)'), max_len=70)
            if row.current_barcodes:
                barcode_display = ', '.join(row.current_barcodes[:2])
                if len(row.current_barcodes) > 2:
                    barcode_display += f' (+{len(row.current_barcodes) - 2})'
                barcode_text = self._truncate_text(barcode_display, max_len=90)
            else:
                barcode_text = ''
            # Quantity cell: editable for known suppliers
            if row.supplier != 'unknown':
                qty_cell_content = ft.TextField(
                    value=str(row._edited_quantity if row._edited_quantity is not None else (row.quantity or 1)),
                    width=60,
                    dense=True,
                    keyboard_type=ft.KeyboardType.NUMBER,
                    text_size=12,
                    content_padding=ft.padding.symmetric(horizontal=4, vertical=2),
                    on_change=lambda e, i=idx: self._set_row_quantity(i, e.control.value),
                )
            else:
                qty_cell_content = ft.Text('', size=12)

            # Order number cell: editable for known suppliers only
            if row.supplier != 'unknown':
                order_display = row._edited_order_number if row._edited_order_number is not None else (row.order_number or '')
                order_cell_content = ft.TextField(
                    value=order_display,
                    width=120,
                    dense=True,
                    text_size=12,
                    content_padding=ft.padding.symmetric(horizontal=4, vertical=2),
                    on_change=lambda e, i=idx: self._set_row_order_number(i, e.control.value),
                )
            else:
                order_cell_content = ft.Text(
                    self._truncate_text(str(row.order_number or ''), max_len=20),
                    size=12, no_wrap=True,
                )

            part_text = self._truncate_text(part_text, max_len=100)
            status_text = self._truncate_text(status_text, max_len=45)
            status_lower = status_text.lower()
            if status_lower.startswith('existing'):
                status_color = 'green'
            elif status_lower.startswith('new'):
                status_color = 'blue'
            elif status_lower.startswith('skipped') or status_lower.startswith('error') or status_lower.startswith('failed'):
                status_color = 'red'
            else:
                status_color = 'orange'

            # Category cell: show text if already set, otherwise show inline
            # search-enabled dropdown (mirrors the global DropdownWithSearch).
            if row.category:
                category_cell_content = ft.Text(
                    self._truncate_text(str(row.category), max_len=90),
                    size=12,
                    no_wrap=False,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                )
            else:
                all_cat_options = list(getattr(self, 'categories', []))
                category_cell_content = ft.Container(
                    width=200,
                    content=ft.AutoComplete(
                        suggestions=[
                            ft.AutoCompleteSuggestion(key=c, value=c)
                            for c in all_cat_options
                        ],
                        on_select=lambda e, i=idx: self._set_row_category(i, e.selection.value),
                    ),
                )

            rows.append(ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(input_code, size=12, no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(row.supplier.upper(), size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(order_cell_content),
                    ft.DataCell(ft.Text(status_text, size=12, color=status_color, no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(part_text, size=12, no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(location_text, size=12, no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(barcode_text, size=11, color='gray', no_wrap=False, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(qty_cell_content),
                    ft.DataCell(category_cell_content),
                    ft.DataCell(ft.Checkbox(
                        value=row.create_stock,
                        disabled=bool(self.fields.get('po_flow_check').value),
                        on_change=lambda _, i=idx: self._toggle_create_stock(i),
                    )),
                    ft.DataCell(remove_btn),
                ],
            ))

        self.fields['results_table'].rows = rows
        self._refresh_import_button_state()
        try:
            self._page.update()
        except AssertionError:
            pass

    @staticmethod
    def _truncate_text(value: str, max_len: int = 30) -> str:
        text = str(value or '')
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + '...'

    def _validate_part_pk(self, part_pk: int) -> bool:
        if int(part_pk or 0) <= 0:
            return False

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return False

        try:
            response = self._request_with_retries(
                method='GET',
                url=f"{base_url.rstrip('/')}/api/part/{int(part_pk)}/",
                headers={
                    'Authorization': f'Token {token}',
                    'Accept': 'application/json',
                },
                timeout=20,
            )
            return response is not None
        except Exception:
            return False

    def _fetch_part_detail(self, part_pk: int) -> Optional[Dict]:
        if int(part_pk or 0) <= 0:
            return None

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return None

        response = self._request_with_retries(
            method='GET',
            url=f"{base_url.rstrip('/')}/api/part/{int(part_pk)}/",
            headers={
                'Authorization': f'Token {token}',
                'Accept': 'application/json',
            },
            timeout=20,
        )
        if response is None:
            return None

        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _find_part_by_lookup(self, lookup_value: str) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_part_by_lookup(self, lookup_value)

    def _find_supplier_part_for_row(self, lookup_values: List[str], supplier_key: str) -> Optional[Dict]:
        probe_values: List[str] = []
        for value in lookup_values:
            text = str(value or '').strip()
            if text and text not in probe_values:
                probe_values.append(text)

        supplier_norm = str(supplier_key or '').strip().lower()
        supplier_aliases = _BarcodeApiHelpers.PO_SUPPLIER_NAME_MAP.get(supplier_norm, [supplier_key])
        supplier_aliases = [str(alias).strip() for alias in supplier_aliases if str(alias).strip()]

        supplier_pk = _BarcodeApiHelpers.resolve_supplier_company_pk(self, supplier_norm)
        if supplier_pk <= 0:
            cprint(f'[BARCODE][PO]\tUnable to resolve supplier company for {supplier_norm}', silent=False)
            return None

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return None

        try:
            headers = {
                'Authorization': f'Token {token}',
                'Accept': 'application/json',
            }
            endpoint = f"{base_url.rstrip('/')}/api/company/part/"

            for probe in probe_values:
                response = self._request_with_retries(
                    method='GET',
                    url=endpoint,
                    headers=headers,
                    params={'supplier': int(supplier_pk), 'search': probe, 'limit': 10},
                    timeout=20,
                )
                if response is None:
                    continue

                payload = response.json()
                if isinstance(payload, dict):
                    rows = payload.get('results') or []
                elif isinstance(payload, list):
                    rows = payload
                else:
                    rows = []

                for candidate in rows:
                    if not isinstance(candidate, dict):
                        continue

                    supplier_name = ''
                    supplier_detail = candidate.get('supplier_detail')
                    if isinstance(supplier_detail, dict):
                        supplier_name = str(supplier_detail.get('name') or '').strip().lower()
                    if not supplier_name:
                        supplier_ref = candidate.get('supplier')
                        if isinstance(supplier_ref, dict):
                            supplier_name = str(supplier_ref.get('name') or supplier_ref.get('display_name') or '').strip().lower()

                    if supplier_aliases and supplier_name:
                        if not any(alias.lower() == supplier_name or alias.lower() in supplier_name for alias in supplier_aliases):
                            continue

                    candidate_pk = 0
                    try:
                        candidate_pk = int(candidate.get('pk') or candidate.get('id') or 0)
                    except Exception:
                        candidate_pk = 0

                    part_ref = candidate.get('part') or candidate.get('part_detail')
                    if isinstance(part_ref, dict):
                        part_pk = part_ref.get('pk') or part_ref.get('id') or 0
                    else:
                        part_pk = part_ref or 0

                    try:
                        part_pk = int(part_pk)
                    except Exception:
                        part_pk = 0

                    if candidate_pk <= 0:
                        continue

                    supplier_part_number = str(
                        candidate.get('SKU')
                        or candidate.get('sku')
                        or candidate.get('supplier_part_number')
                        or candidate.get('supplier_sku')
                        or candidate.get('part_number')
                        or ''
                    ).strip().lower()

                    probe_lower = probe.lower()
                    if probe_lower and supplier_part_number and probe_lower in supplier_part_number:
                        return {
                            'supplier_part_pk': candidate_pk,
                            'part_pk': part_pk,
                            'supplier_name': supplier_name,
                            'supplier_part_number': supplier_part_number,
                        }

                    # If the supplier and part are already exact, accept the candidate
                    # even when the SKU text does not mirror the lookup string.
                    return {
                        'supplier_part_pk': candidate_pk,
                        'part_pk': part_pk,
                        'supplier_name': supplier_name,
                        'supplier_part_number': supplier_part_number,
                    }
        except Exception:
            return None

        return None

    def _resolve_supplier_part_pk(self, supplier_pk: int, part_pk: int, lookup_values: Optional[List[str]] = None) -> int:
        if int(supplier_pk or 0) <= 0 or int(part_pk or 0) <= 0:
            return 0

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return 0

        probes = [str(value or '').strip().lower() for value in (lookup_values or []) if str(value or '').strip()]
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        endpoint = f"{base_url.rstrip('/')}/api/company/part/"
        response = self._request_with_retries(
            method='GET',
            url=endpoint,
            headers=headers,
            params={'supplier': int(supplier_pk), 'part': int(part_pk), 'limit': 25},
            timeout=20,
        )
        if response is None:
            return 0

        try:
            payload = response.json()
            if isinstance(payload, dict):
                rows = payload.get('results') or []
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []
        except Exception:
            rows = []

        if not rows:
            return 0

        # If the supplier/part pair exists, prefer that exact match.
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            candidate_pk = 0
            try:
                candidate_pk = int(candidate.get('pk') or candidate.get('id') or 0)
            except Exception:
                candidate_pk = 0
            if candidate_pk > 0:
                return candidate_pk

        # Prefer SKU that matches any row probe values.
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            sku = str(
                candidate.get('SKU')
                or candidate.get('sku')
                or candidate.get('supplier_part_number')
                or candidate.get('supplier_sku')
                or ''
            ).strip().lower()
            if probes and sku and any(probe in sku or sku in probe for probe in probes):
                try:
                    return int(candidate.get('pk') or candidate.get('id') or 0)
                except Exception:
                    continue

        try:
            return int(rows[0].get('pk') or rows[0].get('id') or 0)
        except Exception:
            return 0

    def _create_supplier_part_link(self, supplier_pk: int, part_pk: int, lookup_values: Optional[List[str]] = None) -> int:
        if int(supplier_pk or 0) <= 0 or int(part_pk or 0) <= 0:
            return 0

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return 0

        endpoint = f"{base_url.rstrip('/')}/api/company/part/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        sku_candidates: List[str] = []
        for value in (lookup_values or []):
            sku = str(value or '').strip()
            if not sku:
                continue
            if sku.lower() in [item.lower() for item in sku_candidates]:
                continue
            sku_candidates.append(sku)

        if not sku_candidates:
            return 0

        for sku in sku_candidates:
            payloads = [
                {'supplier': int(supplier_pk), 'part': int(part_pk), 'SKU': sku},
                {'supplier': int(supplier_pk), 'part': int(part_pk), 'supplier_sku': sku},
                {'supplier': int(supplier_pk), 'part': int(part_pk), 'supplier_part_number': sku},
            ]
            for payload in payloads:
                response = self._request_with_retries(
                    method='POST',
                    url=endpoint,
                    headers=headers,
                    json=payload,
                    timeout=20,
                )
                if response is None:
                    continue

                if response.status_code in [200, 201, 202]:
                    try:
                        created = response.json()
                        created_pk = int((created or {}).get('pk') or (created or {}).get('id') or 0)
                    except Exception:
                        created_pk = 0
                    if created_pk > 0:
                        cprint(
                            f"[BARCODE][PO]\tCreated missing supplier-part link supplier_pk={supplier_pk} part_pk={part_pk} supplier_part_pk={created_pk} sku={sku}",
                            silent=False,
                        )
                        return created_pk

        return self._resolve_supplier_part_pk(supplier_pk=supplier_pk, part_pk=part_pk, lookup_values=lookup_values)

    def _find_existing_part_for_row(self, lookup_values: List[str]) -> Optional[Dict]:
        """Resolve an existing part using direct part lookup first, then supplier-part fallback."""
        probe_values: List[str] = []
        for value in lookup_values:
            text = str(value or '').strip()
            if text and text not in probe_values:
                probe_values.append(text)

        for probe in probe_values:
            part_match = self._find_part_by_lookup(probe)
            if part_match:
                part_pk = int(part_match.get('pk') or part_match.get('id') or 0)
                if part_pk > 0 and self._validate_part_pk(part_pk):
                    part_detail = self._fetch_part_detail(part_pk)
                    if isinstance(part_detail, dict):
                        part_detail['supplier_part_pk'] = int(part_match.get('supplier_part_pk') or 0)
                        return part_detail
                    return {'pk': part_pk, 'part_pk': part_pk, 'supplier_part_pk': 0}
                cprint(
                    f"[BARCODE][PO]\tRejected direct part lookup result for '{probe}' because pk={part_pk} is not a valid /api/part/ record",
                    silent=False,
                )

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            return None

        try:
            headers = {
                'Authorization': f'Token {token}',
                'Accept': 'application/json',
            }
            endpoint = f"{base_url.rstrip('/')}/api/company/part/"

            for probe in probe_values:
                response = self._request_with_retries(
                    method='GET',
                    url=endpoint,
                    headers=headers,
                    params={'search': probe, 'limit': 5},
                    timeout=20,
                )
                if response is None:
                    continue

                payload = response.json()
                if isinstance(payload, dict):
                    rows = payload.get('results') or []
                elif isinstance(payload, list):
                    rows = payload
                else:
                    rows = []

                for candidate in rows:
                    part_ref = candidate.get('part')
                    if isinstance(part_ref, dict):
                        part_pk = part_ref.get('pk') or part_ref.get('id')
                    else:
                        part_pk = part_ref

                    try:
                        part_pk = int(part_pk)
                    except Exception:
                        continue

                    if not self._validate_part_pk(part_pk):
                        cprint(
                            f"[BARCODE][PO]\tRejected company-part fallback for '{probe}' because pk={part_pk} is not a valid /api/part/ record",
                            silent=False,
                        )
                        continue

                    supplier_part_pk = int(candidate.get('pk') or candidate.get('id') or 0)
                    part_detail = self._fetch_part_detail(part_pk)
                    if isinstance(part_detail, dict):
                        part_detail['supplier_part_pk'] = supplier_part_pk
                        return part_detail

                    return {'pk': part_pk, 'part_pk': part_pk, 'supplier_part_pk': supplier_part_pk}
        except Exception:
            return None

        return None

    def _resolve_location_string(self, part: Dict) -> str:
        return _BarcodeApiHelpers.resolve_location_string(part)

    def _validate_import_row_async(self, row: BarcodeScannedRow):
        """Assign-style live validation so Barcode list resolves existing parts before import."""
        try:
            row.status = 'Checking server...'
            self._update_results_table()

            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.0):
                row.status = 'Server offline'
                self._update_results_table()
                return

            row.status = 'Checking part...'
            self._update_results_table()

            part = self._find_existing_part_for_row([
                row.lookup_value,
                row.manufacturer_pn,
                row.supplier_pn,
                row.raw_barcode,
                row.barcode,
            ])

            if not part:
                row.status = 'New part'
                row.part_pk = None
                row.part_name = ''
                self._update_results_table()
                return

            row.part_pk = int(part.get('pk') or part.get('id') or 0)
            row.part_name = str(part.get('name') or part.get('IPN') or row.search_name)
            try:
                row.default_location_pk = int(part.get('default_location') or 0)
            except Exception:
                row.default_location_pk = 0

            if not row.location:
                row.location = self._resolve_location_string(part)

            # Fetch and set existing part category as a full path string so it
            # matches the format used by the dropdown options (e.g. "Electronics / Resistors / SMD").
            # Prefer pathstring over bare name at every level to avoid leaf-only mismatch.
            part_category = ''

            category_detail = part.get('category_detail')
            if isinstance(category_detail, dict):
                part_category = str(category_detail.get('pathstring') or category_detail.get('name') or '').strip()

            if not part_category:
                raw_category = part.get('category')
                if isinstance(raw_category, dict):
                    part_category = str(raw_category.get('pathstring') or raw_category.get('name') or '').strip()

            if not part_category:
                raw_category_name = part.get('category_name')
                if raw_category_name not in [None, '']:
                    part_category = str(raw_category_name).strip()

            # If we only got a leaf name (InvenTree returned category as an ID
            # rather than an expanded object), resolve it to the full path by
            # matching against the loaded category tree so it aligns with the
            # dropdown options (e.g. "--- Mechanical / Fasteners / Standoffs and Spacers").
            # Only substitute if there is exactly one match — ambiguous leaves are left as-is.
            if part_category and not part_category.isdigit():
                categories = list(getattr(self, 'categories', []))
                if categories:
                    leaf = part_category.split('/')[-1].strip()
                    matches = [
                        c for c in categories
                        # Strip leading dashes+space, then take the last path segment.
                        if re.sub(r'^-+\s*', '', c).split('/')[-1].strip() == leaf
                    ]
                    if len(matches) == 1:
                        part_category = matches[0]

            if part_category and not part_category.isdigit():
                row.category = part_category
                row._category_from_part = True
            else:
                row.category = ''
                row._category_from_part = False

            row.barcode_hash = str(part.get('barcode_hash') or '').strip()

            row.current_barcodes = self._fetch_part_barcodes(part_pk=row.part_pk)
            if not row.current_barcodes and row.barcode_hash:
                # Fallback for servers where barcode endpoint is unavailable.
                if row.part_name:
                    row.current_barcodes = [row.part_name]

            row.has_barcode = bool(row.current_barcodes) or bool(row.barcode_hash)

            has_location = bool(row.default_location_pk)
            if has_location and row.has_barcode:
                row.status = 'Existing (location+barcode)'
            elif has_location:
                row.status = 'Existing (missing barcode)'
            elif row.has_barcode:
                row.status = 'Existing (missing location)'
            else:
                row.status = 'Existing (missing location+barcode)'
        except Exception as exc:
            row.status = f'Error: {str(exc)[:40]}'
        finally:
            self._update_results_table()
    
    def _remove_row(self, idx: int):
        """Remove a scanned row."""
        if 0 <= idx < len(self.scanned_rows):
            self.scanned_rows.pop(idx)
            self._update_results_table()
        self.focus_barcode_input()

    def _clear_all_rows(self, _):
        """Clear all scanned rows."""
        if not self.scanned_rows:
            return
        self.scanned_rows.clear()
        self._recent_scan_codes.clear()
        self._update_results_table()
        self._show_status('Cleared all scanned items', color='blue')
        self.focus_barcode_input()

    def _on_clear_input(self, _):
        """Clear the barcode input field and return focus to it."""
        self.fields['barcode_input'].value = ''
        try:
            self.fields['barcode_input'].update()
        except AssertionError:
            pass
        self.focus_barcode_input()

    def _on_category_changed(self, *args, **kwargs):
        """Apply selected category to all items that don't already have one."""
        category = self.fields['category_select'].value
        if category:
            for row in self.scanned_rows:
                # Apply to new parts (no part_pk) and to existing parts that have no category set.
                if not row.category:
                    row.category = category
            self._update_results_table()
        self._refresh_import_button_state()
        # No focus_barcode_input() here — the dropdown search field fires on_change
        # on every keystroke; stealing focus mid-type would cut off the search query.

    def _on_location_changed(self, *args, **kwargs):
        """Apply selected location to all items that don't already have one."""
        location_value = self.fields['location_select'].value
        if location_value:
            location = str(location_value)
            try:
                cache = getattr(self, '_location_pk_to_path_cache', None)
                if isinstance(cache, dict):
                    location = cache.get(int(location_value), location)
            except (ValueError, TypeError):
                pass
            for row in self.scanned_rows:
                # Only set if the row has no location yet — never override an
                # existing location (whether from the part API or a prior pick).
                if not row.location:
                    row.location = location
            self._update_results_table()
        # No focus_barcode_input() here — same reason as _on_category_changed.

    def _on_create_stock_changed(self, _):
        """Apply create_stock flag to all items."""
        if bool(self.fields.get('po_flow_check').value):
            self.fields['create_stock_check'].value = False
            try:
                self.fields['create_stock_check'].update()
            except AssertionError:
                pass
            create_stock = False
        else:
            create_stock = self.fields['create_stock_check'].value
        for row in self.scanned_rows:
            row.create_stock = create_stock
        self._update_results_table()
        self.focus_barcode_input()

    def _on_po_flow_changed(self, _):
        """Toggle PO flow controls and keep stock controls consistent."""
        po_enabled = bool(self.fields.get('po_flow_check').value)
        self.fields['create_stock_check'].disabled = po_enabled
        self.fields['assign_all_stock_items_location_check'].disabled = po_enabled

        if po_enabled:
            self.fields['create_stock_check'].value = False
            self.fields['assign_all_stock_items_location_check'].value = False
            for row in self.scanned_rows:
                row.create_stock = False

        try:
            self.fields['create_stock_check'].update()
            self.fields['assign_all_stock_items_location_check'].update()
        except AssertionError:
            pass

        self._update_results_table()
        self.focus_barcode_input()

    def _clear_category(self, _):
        """Clear only the global category dropdown. Per-row categories are untouched."""
        self.fields['category_select'].value = None
        try:
            self.fields['category_select'].update()
        except AssertionError:
            pass
        self._refresh_import_button_state()
        self._show_status('Cleared category selection', color='blue')
        self.focus_barcode_input()

    def _clear_location(self, _):
        """Clear only the global location dropdown. Per-row locations are untouched."""
        self.fields['location_select'].value = None
        try:
            self.fields['location_select'].update()
        except AssertionError:
            pass
        self._show_status('Cleared location selection', color='blue')
        self.focus_barcode_input()

    def _toggle_create_stock(self, idx: int):
        """Toggle create_stock for a specific row."""
        if bool(self.fields.get('po_flow_check').value):
            return
        if 0 <= idx < len(self.scanned_rows):
            self.scanned_rows[idx].create_stock = not self.scanned_rows[idx].create_stock
            self._update_results_table()
        self.focus_barcode_input()

    def _set_row_category(self, idx: int, category: str):
        """Set category for a specific row from the inline per-row autocomplete."""
        if 0 <= idx < len(self.scanned_rows) and category:
            self.scanned_rows[idx].category = category
            self._update_results_table()
        self.focus_barcode_input()

    def _set_row_quantity(self, idx: int, value: str):
        """Store user-edited quantity for a row. Reverts to parsed value when field is cleared."""
        if 0 <= idx < len(self.scanned_rows):
            text = str(value or '').strip()
            if not text:
                self.scanned_rows[idx]._edited_quantity = None
                return
            try:
                qty = int(text)
                if qty > 0:
                    self.scanned_rows[idx]._edited_quantity = qty
            except (ValueError, TypeError):
                pass

    def _set_row_order_number(self, idx: int, value: str):
        """Store user-edited order number for a row."""
        if 0 <= idx < len(self.scanned_rows):
            self.scanned_rows[idx]._edited_order_number = str(value or '').strip()

    def _refresh_import_button_state(self):
        """Enable Import All only when every row has a category assigned."""
        btn = self.fields.get('barcode_submit')
        if btn is None:
            return
        if not self.scanned_rows:
            btn.disabled = True
        else:
            global_category = self.fields['category_select'].value if self.fields.get('category_select') else None
            all_have_category = all(
                bool(row.category) or bool(global_category)
                for row in self.scanned_rows
            )
            btn.disabled = not all_have_category
        try:
            btn.update()
        except AssertionError:
            pass

    def _show_status(self, message: str, color: str = 'black'):
        """Show status message."""
        self.fields['status_message'].value = message
        self.fields['status_message'].color = color
        try:
            self.fields['status_message'].update()
        except AssertionError:
            pass

    def _normalize_stock_location_value(self, location: str) -> str:
        return _BarcodeApiHelpers.normalize_stock_location_value(location)

    def _get_stock_location_pk(self, location: str) -> int:
        location_pk = inventree_interface.resolve_stock_location_pk(location, self.stock_location_id_map)
        if location_pk > 0:
            cprint(f'[BARCODE]\tResolved stock location: {location} -> pk={location_pk}', silent=False)
        else:
            cprint(f'[BARCODE]\tStock location cache miss: {location}', silent=False)
        return location_pk

    def _connect_server_with_retries(self, attempts: int = 3, delay_seconds: float = 1.5) -> bool:
        """Try connecting to InvenTree multiple times before failing."""
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if api_obj and getattr(api_obj, 'token', None) and getattr(api_obj, 'base_url', None):
            return True

        with self._connect_lock:
            api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
            if api_obj and getattr(api_obj, 'token', None) and getattr(api_obj, 'base_url', None):
                return True

            for attempt in range(1, attempts + 1):
                if inventree_interface.connect_to_server(force_reconnect=(attempt > 1)):
                    return True
                if attempt < attempts:
                    self._show_status(
                        f'InvenTree offline. Retrying ({attempt}/{attempts - 1}) in {delay_seconds:.1f}s...',
                        color='orange',
                    )
                    time.sleep(delay_seconds)
        return False

    def _request_with_retries(
            self,
            method: str,
            url: str,
            attempts: int = 3,
            delay_seconds: float = 1.0,
            **kwargs,
    ) -> Optional[requests.Response]:
        """Run HTTP request with retry on transient/network failures."""
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(method=method.upper(), url=url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f'HTTP {response.status_code}', response=response)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                status_code = None
                try:
                    status_code = int(getattr(getattr(exc, 'response', None), 'status_code', 0))
                except Exception:
                    status_code = None

                if status_code and 400 <= status_code < 500 and status_code != 429:
                    return None

                if attempt < attempts:
                    time.sleep(delay_seconds)
                else:
                    return None

        return None

    def _set_part_default_location(self, part_pk: int, location_pk: int) -> bool:
        return _BarcodeApiHelpers.set_part_default_location(self, part_pk, location_pk)

    def _link_part_barcode(self, part_pk: int, barcode_value: str) -> bool:
        return _BarcodeApiHelpers.link_part_barcode(self, part_pk, barcode_value)

    def _fetch_part_barcodes(self, part_pk: int) -> List[str]:
        return _BarcodeApiHelpers.fetch_part_barcodes(self, part_pk)

    def _collect_transfer_items_for_part(self, part_pk: int, location_pk: int) -> tuple[List[Dict], str]:
        return _BarcodeApiHelpers.collect_transfer_items_for_part(self, part_pk, location_pk)

    def _transfer_stock_items_bulk(self, transfer_items: List[Dict], location_pk: int) -> tuple[bool, str]:
        return _BarcodeApiHelpers.transfer_stock_items_bulk(self, transfer_items, location_pk)

    def _resolve_supplier_company_pk(self, supplier_key: str) -> int:
        return _BarcodeApiHelpers.resolve_supplier_company_pk(self, supplier_key)

    def _list_open_purchase_orders(self, supplier_pk: int, limit: int = 50) -> List[Dict]:
        return _BarcodeApiHelpers.list_open_purchase_orders(self, supplier_pk, limit)

    def _find_open_purchase_order(self, supplier_pk: int, reference: str) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_open_purchase_order(self, supplier_pk, reference)

    def _get_po_location_pk(self, po_data: Optional[Dict]) -> int:
        return _BarcodeApiHelpers.get_po_location_pk(po_data)

    def _create_purchase_order(self, supplier_pk: int, reference: str, location_pk: int = 0) -> tuple[Optional[Dict], str]:
        return _BarcodeApiHelpers.create_purchase_order(self, supplier_pk, reference, location_pk)

    def _find_po_line_for_part(self, po_pk: int, part_pk: int) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_po_line_for_part(self, po_pk, part_pk)

    def _find_po_line_for_internal_part(self, po_pk: int, internal_part_pk: int) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_po_line_for_internal_part(self, po_pk, internal_part_pk)

    def _create_po_line(self, po_pk: int, part_pk: int, quantity: int) -> tuple[Optional[Dict], str]:
        return _BarcodeApiHelpers.create_po_line(self, po_pk, part_pk, quantity)

    def _get_po_line_quantities(self, line_data: Optional[Dict]) -> tuple[int, int]:
        return _BarcodeApiHelpers.get_po_line_quantities(line_data)

    def _update_po_line_quantity(self, line_pk: int, quantity: int) -> tuple[bool, str]:
        return _BarcodeApiHelpers.update_po_line_quantity(self, line_pk, quantity)

    def _receive_po_line(self, line_pk: int, quantity: int, location_pk: int = 0) -> tuple[bool, str]:
        return _BarcodeApiHelpers.receive_po_items(self, line_pk, [{'line_item': line_pk, 'quantity': quantity}], location_pk)

    def _receive_po_items(self, po_pk: int, items: List[Dict], location_pk: int = 0) -> tuple[bool, str]:
        return _BarcodeApiHelpers.receive_po_items(self, po_pk, items, location_pk)

    def _trigger_po_action(self, po_pk: int, action: str) -> bool:
        return _BarcodeApiHelpers.trigger_po_action(self, po_pk, action)
    
    def _on_submit(self, _):
        """Submit scanned items for import."""
        if not self.scanned_rows:
            self.show_dialog(DialogType.ERROR, 'No items to import')
            return

        # Validate all items have required fields
        for row in self.scanned_rows:
            if not row.search_name:
                self.show_dialog(
                    DialogType.ERROR,
                    f'{row.supplier.upper()}: Could not extract part number'
                )
                return

        # Ensure every item has a category (either from the part itself, a global
        # selection, or a per-row pick).
        global_category = self.fields['category_select'].value if self.fields.get('category_select') else None
        missing_category = [
            row for row in self.scanned_rows
            if not row.category and not global_category
        ]
        if missing_category:
            self.show_dialog(
                DialogType.ERROR,
                f'{len(missing_category)} item(s) are missing a category. '
                'Set a global category or pick one per item.'
            )
            return

        # Reset transient caches before each run to avoid stale session state from
        # suppressing valid lookups in long UI sessions.
        with self._part_lookup_lock:
            self._part_lookup_cache.clear()
            self._part_lookup_inflight.clear()
        self._supplier_company_cache.clear()
        self._po_endpoint_cache.clear()

        self._show_status('Import running...', color='blue')
        self._execute_import()
    
    def _execute_import(self):
        """Execute the import process."""
        op_start_ts = time.perf_counter()
        total = len(self.scanned_rows)
        cprint(f'[BARCODE]\tStarting import of {total} item(s)', silent=False)
        self.fields['import_progress'].visible = True
        self.fields['import_progress'].value = 0
        self.fields['import_progress_message'].value = f'Importing 0/{total}'
        self.fields['import_progress_message'].color = 'blue'
        self.fields['import_progress'].update()
        self.fields['import_progress_message'].update()
        self._show_status('Starting import...', color='blue')

        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            cprint('[BARCODE]\tImport aborted: failed to connect to InvenTree', silent=False)
            self.fields['import_progress_message'].value = 'Import failed: server offline after retries'
            self.fields['import_progress_message'].color = 'red'
            self.fields['import_progress_message'].update()
            self.show_dialog(DialogType.ERROR, 'Failed to connect to InvenTree server after 3 retries')
            return

        success = 0
        failed = 0
        failures = []
        rows_snapshot = list(self.scanned_rows)
        use_manufacturer_barcode = bool(self.fields['use_manufacturer_barcode_check'].value)
        po_flow_enabled = bool(self.fields.get('po_flow_check').value)
        assign_all_stock_items_location = bool(self.fields.get('assign_all_stock_items_location_check').value) and not po_flow_enabled
        force_barcode_reassign = bool(self.fields.get('force_barcode_reassign_check').value)

        selected_location_value = str(self.fields.get('location_select').value or '').strip()
        assign_location_existing = bool(selected_location_value)
        selected_location_pk = 0
        if assign_location_existing:
            selected_location_pk = self._get_stock_location_pk(selected_location_value)
            if selected_location_pk <= 0:
                if po_flow_enabled:
                    cprint(f'[BARCODE]\tSelected location ignored for PO flow: {selected_location_value}', silent=False)
                    selected_location_pk = 0
                else:
                    self.fields['import_progress_message'].value = 'Import failed: selected location could not be resolved'
                    self.fields['import_progress_message'].color = 'red'
                    self.fields['import_progress_message'].update()
                    self.show_dialog(DialogType.ERROR, f'Selected stock location not found: {selected_location_value}')
                    return

        location_map: Dict[str, int] = {}
        location_map_lock = threading.Lock()
        location_map_loaded = False
        row_results: List[Dict] = []

        def _process_import_row(idx: int, row: BarcodeScannedRow) -> Dict:
            nonlocal location_map_loaded
            item_label = f'{row.supplier.upper()} - {row.search_name}'
            result = {
                'idx': idx,
                'row': row,
                'ok': False,
                'failure': '',
                'part_pk': 0,
                'supplier_part_pk': 0,
                'existing_part': False,
                'po_supplier_key': str(row.supplier or '').strip().lower(),
                'po_reference': row.effective_order_number,
                'po_quantity': max(1, row.effective_quantity),
            }

            try:
                existing_part = self._find_existing_part_for_row([
                    row.search_name,
                    row.manufacturer_pn,
                    row.supplier_pn,
                    row.raw_barcode,
                    row.barcode,
                ])

                if existing_part:
                    part_pk = int(existing_part.get('pk') or existing_part.get('id') or 0)
                    if not part_pk:
                        result['failure'] = f'{row.search_name}: Existing part has invalid PK'
                        return result

                    _ = str(existing_part.get('name') or existing_part.get('IPN') or row.search_name or '').strip()

                    # Existing-part path: Assign workflow semantics.
                    if assign_location_existing and not po_flow_enabled:
                        set_ok = self._set_part_default_location(part_pk=part_pk, location_pk=selected_location_pk)
                        if not set_ok:
                            result['failure'] = f'{row.search_name}: default location update failed'
                            return result

                    barcode_target = ''
                    if use_manufacturer_barcode:
                        barcode_target = str(row.manufacturer_pn or row.barcode or '').strip()

                    if barcode_target:
                        current_barcodes = self._fetch_part_barcodes(part_pk=part_pk)
                        current_barcodes_normalized = {
                            str(value or '').strip().lower()
                            for value in current_barcodes
                            if str(value or '').strip()
                        }
                        if barcode_target.lower() in current_barcodes_normalized:
                            pass
                        elif current_barcodes and not force_barcode_reassign:
                            pass
                        else:
                            barcode_ok = False
                            for attempt in range(1, 4):
                                try:
                                    barcode_ok = self._link_part_barcode(part_pk=part_pk, barcode_value=barcode_target)
                                    if barcode_ok:
                                        break
                                    elif attempt < 3:
                                        time.sleep(0.5)
                                except Exception as exc:
                                    cprint(f'[WARN]\tBarcode link retry {attempt} failed for "{row.search_name}": {str(exc)[:60]}', silent=False)
                                    if attempt < 3:
                                        time.sleep(0.5)
                            if not barcode_ok:
                                # Re-fetch to check if the barcode is already assigned
                                # (API may reject re-assignment of an identical value).
                                recheck = self._fetch_part_barcodes(part_pk=part_pk)
                                recheck_normalized = {
                                    str(b or '').strip().lower()
                                    for b in recheck
                                    if str(b or '').strip()
                                }
                                if barcode_target.lower() in recheck_normalized:
                                    cprint(
                                        f'[INFO]\tBarcode already assigned to part {row.search_name} (barcode={barcode_target})',
                                        silent=False,
                                    )
                                else:
                                    cprint(
                                        f'[WARN]\tBarcode reassignment failed for existing part {row.search_name} (barcode={barcode_target}) after retries',
                                        silent=False,
                                    )

                    result['ok'] = True
                    result['part_pk'] = int(part_pk)
                    result['existing_part'] = True
                    if po_flow_enabled:
                        if str(row.supplier or '').strip().lower() == 'unknown':
                            result['ok'] = False
                            result['failure'] = f'{row.search_name}: PO flow requires known supplier'
                            return result
                        if not result['po_reference']:
                            result['ok'] = False
                            result['failure'] = f'{row.search_name}: missing supplier order reference for PO flow'
                            return result
                    return result

                supplier_name = self._resolve_supplier_key(row.supplier)
                supplier_data = None
                if str(row.supplier or '').strip().lower() != 'unknown':
                    supplier_data = inventree_interface.supplier_search(
                        supplier=supplier_name,
                        part_number=row.search_name,
                    )
                else:
                    row.status = 'Skipped (unknown supplier)'
                    result['failure'] = f'{row.search_name}: skipped new part creation (unknown supplier)'
                    return result

                if not supplier_data:
                    part_form = {
                        'name': row.search_name,
                        'description': f'Imported from {row.supplier.upper()}',
                        'category_tree': [row.category] if row.category else [],
                    }
                else:
                    part_form = inventree_interface.translate_supplier_to_form(
                        supplier=supplier_name,
                        part_info=supplier_data,
                    )
                    part_form['name'] = row.search_name

                if row.category:
                    part_form['category_tree'] = [row.category]
                else:
                    part_form.pop('category_tree', None)

                stock_payload = None
                if row.create_stock and not po_flow_enabled:
                    if not row.location:
                        pass
                    else:
                        normalized_location = self._normalize_stock_location_value(row.location)
                        with location_map_lock:
                            if not location_map_loaded:
                                location_map.update(inventree_interface.get_stock_location_id_map() or {})
                                location_map_loaded = True

                        location_pk = inventree_interface.resolve_stock_location_pk(normalized_location, location_map)

                        if location_pk <= 0:
                            result['failure'] = f'{row.search_name}: Location not found'
                            return result

                        stock_payload = {
                            'location': location_pk,
                            'quantity': row.effective_quantity,
                            'make_default': False,
                        }

                with self._part_create_lock:
                    _create_result = inventree_interface.inventree_create(
                        part_info=part_form,
                        kicad=False,
                        show_progress=False,
                        is_custom=False,
                        stock=None,
                    )
                    part_pk = _create_result[1] if _create_result else None

                if not part_pk:
                    # If concurrent import created the same part first, try resolving it.
                    existing_after_create = self._find_existing_part_for_row([
                        row.search_name,
                        row.manufacturer_pn,
                        row.supplier_pn,
                        row.raw_barcode,
                        row.barcode,
                    ])
                    if existing_after_create:
                        part_pk = int(existing_after_create.get('pk') or existing_after_create.get('id') or 0)

                if not part_pk:
                    result['failure'] = f'{row.search_name}: Failed to create'
                    return result

                if stock_payload is not None:
                    stock_data = dict(stock_payload)
                    stock_data['part'] = part_pk
                    stock_result = inventree_interface.inventree_api.create_stock(stock_data)
                    if not stock_result:
                        result['failure'] = f'{row.search_name}: Failed to create stock'
                        return result

                barcode_value = row.barcode if use_manufacturer_barcode else ''
                if barcode_value:
                    try:
                        inventree_interface.inventree_api.link_barcode(barcode_value, part_pk=part_pk)
                    except Exception as exc:
                        cprint(f'[WARN]\tBarcode linking failed for {row.search_name}: {str(exc)}', silent=False)

                result['ok'] = True
                result['part_pk'] = int(part_pk)
                if po_flow_enabled and not result['po_reference']:
                    result['ok'] = False
                    result['failure'] = f'{row.search_name}: missing supplier order reference for PO flow'
                    return result

                if po_flow_enabled:
                    supplier_part = self._find_supplier_part_for_row([
                        row.search_name,
                        row.manufacturer_pn,
                        row.supplier_pn,
                        row.raw_barcode,
                        row.barcode,
                    ], row.supplier)
                    if supplier_part:
                        result['supplier_part_pk'] = int(supplier_part.get('supplier_part_pk') or 0)
                        cprint(
                            f"[BARCODE][PO]\tResolved supplier part: part_pk={result['part_pk']} supplier_part_pk={result['supplier_part_pk']} for {row.search_name}",
                            silent=False,
                        )
                return result
            except Exception as exc:
                result['failure'] = f'{row.search_name}: {str(exc)[:50]}'
                cprint(f'[WARN]\tImport exception for {item_label}: {str(exc)[:120]}', silent=False)
                return result

        max_workers = min(4, max(1, total))
        cprint(f'[BARCODE]\tParallel workers: {max_workers}', silent=False)

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_process_import_row, idx, row): (idx, row)
                for idx, row in enumerate(rows_snapshot, start=1)
            }

            for future in as_completed(future_map):
                result = future.result()
                row_results.append(result)

                completed += 1
                self.fields['import_progress'].value = completed / total if total else 1.0
                self.fields['import_progress'].update()
                self.fields['import_progress_message'].value = f'Processed {completed}/{total}'
                self.fields['import_progress_message'].update()

        if po_flow_enabled:
            po_candidates = [
                item for item in row_results
                if item.get('ok') and int(item.get('part_pk') or 0) > 0
            ]
            cprint(f'[BARCODE][PO]\tPO flow enabled: candidates={len(po_candidates)}', silent=False)
            po_groups: Dict[tuple[str, str], Dict] = {}

            for item in po_candidates:
                supplier_key = str(item.get('po_supplier_key') or '').strip().lower()
                po_ref = str(item.get('po_reference') or '').strip()
                if not supplier_key or not po_ref:
                    item['ok'] = False
                    item['failure'] = f"{item['row'].search_name}: missing supplier/order reference for PO flow"
                    cprint(f"[BARCODE][PO]\tMissing reference row={item['row'].search_name} supplier={supplier_key or '-'}", silent=False)
                    continue

                key = (supplier_key, po_ref.lower())
                if key not in po_groups:
                    po_groups[key] = {
                        'items': [],
                        'po_reference': po_ref,
                    }
                po_groups[key]['items'].append(item)

            cprint(f'[BARCODE][PO]\tGrouped into {len(po_groups)} supplier/order buckets', silent=False)

            for (supplier_key, _), group in po_groups.items():
                po_ref = str(group.get('po_reference') or '').strip()
                cprint(
                    f"[BARCODE][PO]\tProcessing supplier={supplier_key} ref={po_ref} rows={len(group['items'])}",
                    silent=False,
                )
                supplier_pk = self._resolve_supplier_company_pk(supplier_key)
                if supplier_pk <= 0:
                    detail = f'could not resolve supplier company for {supplier_key}'
                    cprint(f'[BARCODE][PO]\t{detail}', silent=False)
                    for item in group['items']:
                        item['ok'] = False
                        item['failure'] = f"{item['row'].search_name}: PO flow failed ({detail})"
                    continue

                open_pos = self._list_open_purchase_orders(supplier_pk=supplier_pk, limit=50)
                if open_pos:
                    po_summaries = [_BarcodeApiHelpers._po_summary(po) for po in open_pos[:8]]
                    cprint(
                        f"[BARCODE][PO]\tOpen editable POs for supplier_pk={supplier_pk}: count={len(open_pos)} -> {'; '.join(po_summaries)}",
                        silent=False,
                    )
                else:
                    cprint(f"[BARCODE][PO]\tOpen editable POs for supplier_pk={supplier_pk}: none", silent=False)

                group_parts: Dict[int, int] = {}
                unresolved_groups: Dict[int, Dict[str, Any]] = {}
                for item in group['items']:
                    supplier_part_pk = int(item.get('supplier_part_pk') or 0)
                    part_pk = int(item.get('part_pk') or 0)
                    if supplier_part_pk <= 0:
                        row_obj = item.get('row')
                        lookup_values = [
                            getattr(row_obj, 'search_name', ''),
                            getattr(row_obj, 'manufacturer_pn', ''),
                            getattr(row_obj, 'supplier_pn', ''),
                            getattr(row_obj, 'barcode', ''),
                            getattr(row_obj, 'raw_barcode', ''),
                        ]
                        supplier_part_pk = self._resolve_supplier_part_pk(
                            supplier_pk=supplier_pk,
                            part_pk=part_pk,
                            lookup_values=lookup_values,
                        )

                        if supplier_part_pk <= 0 and part_pk > 0:
                            supplier_part_pk = self._create_supplier_part_link(
                                supplier_pk=supplier_pk,
                                part_pk=part_pk,
                                lookup_values=lookup_values,
                            )

                        if supplier_part_pk > 0:
                            item['supplier_part_pk'] = supplier_part_pk
                            cprint(
                                f"[BARCODE][PO]\tResolved supplier_part_pk in-group: row={item['row'].search_name} part_pk={part_pk} supplier_part_pk={supplier_part_pk}",
                                silent=False,
                            )

                    if supplier_part_pk <= 0:
                        row_qty = max(1, int(item.get('po_quantity') or 1))
                        if part_pk > 0:
                            group_data = unresolved_groups.setdefault(part_pk, {'qty': 0, 'items': []})
                            group_data['qty'] = int(group_data.get('qty') or 0) + row_qty
                            group_data['items'].append(item)
                            cprint(
                                f"[BARCODE][PO]\tMissing supplier_part_pk for row={item['row'].search_name} part_pk={part_pk}; deferring to existing PO line lookup",
                                silent=False,
                            )
                        else:
                            cprint(f"[BARCODE][PO]\tMissing supplier_part_pk for row={item['row'].search_name} part_pk={part_pk}", silent=False)
                            item['ok'] = False
                            item['failure'] = f"{item['row'].search_name}: missing supplier part for PO flow"
                        continue

                    group_parts[supplier_part_pk] = group_parts.get(supplier_part_pk, 0) + int(item.get('po_quantity') or 1)

                cprint(
                    f"[BARCODE][PO]\tResolved parts for supplier={supplier_key} ref={po_ref}: supplier_parts={len(group_parts)} unresolved_internal_parts={len(unresolved_groups)}",
                    silent=False,
                )

                po_data = self._find_open_purchase_order(supplier_pk=supplier_pk, reference=po_ref)
                po_created = False
                if not po_data:
                    cprint(
                        f"[BARCODE][PO]\tFlow decision: CREATE new PO (no open PO matched supplier_pk={supplier_pk} ref={po_ref})",
                        silent=False,
                    )
                    po_data, po_error = self._create_purchase_order(
                        supplier_pk=supplier_pk,
                        reference=po_ref,
                        location_pk=selected_location_pk,
                    )
                    if not po_data:
                        detail = po_error or 'failed to create purchase order'
                        cprint(f"[BARCODE][PO]\tCreate PO failed supplier_pk={supplier_pk} ref={po_ref}: {detail}", silent=False)
                        for item in group['items']:
                            item['ok'] = False
                            item['failure'] = f"{item['row'].search_name}: PO flow failed ({detail})"
                        continue
                    po_created = True
                    cprint(f"[BARCODE][PO]\tCreated new PO supplier_pk={supplier_pk} ref={po_ref}", silent=False)
                else:
                    cprint(
                        f"[BARCODE][PO]\tFlow decision: EDIT existing open PO supplier_pk={supplier_pk} ref={po_ref} ({_BarcodeApiHelpers._po_summary(po_data)})",
                        silent=False,
                    )

                po_pk = int((po_data or {}).get('pk') or (po_data or {}).get('id') or 0)
                if po_pk <= 0:
                    cprint(f"[BARCODE][PO]\tInvalid PO id supplier_pk={supplier_pk} ref={po_ref}", silent=False)
                    for item in group['items']:
                        item['ok'] = False
                        item['failure'] = f"{item['row'].search_name}: PO flow failed (invalid PO id)"
                    continue

                receive_location_pk = self._get_po_location_pk(po_data) or int(selected_location_pk or 0)

                if po_created and unresolved_groups:
                    detail = 'missing supplier part link for one or more rows (cannot add new PO lines without supplier_part_pk)'
                    cprint(f"[BARCODE][PO]\t{detail}", silent=False)
                    for group_data in unresolved_groups.values():
                        for item in group_data.get('items') or []:
                            item['ok'] = False
                            item['failure'] = f"{item['row'].search_name}: PO flow failed ({detail})"
                    unresolved_groups = {}

                if not group_parts and not unresolved_groups:
                    cprint(f"[BARCODE][PO]\tSkipping PO operations (no resolvable lines) supplier={supplier_key} ref={po_ref}", silent=False)
                    continue

                po_issued = self._trigger_po_action(po_pk, 'issue')
                if po_issued:
                    cprint(f"[BARCODE][PO]\tIssued PO pk={po_pk}", silent=False)
                else:
                    cprint(f"[BARCODE][PO]\tIssue step failed or PO already issued pk={po_pk}", silent=False)

                part_errors: Dict[int, str] = {}
                unresolved_item_errors: Dict[int, str] = {}
                receive_items: List[Dict] = []
                for supplier_part_pk, qty in group_parts.items():
                    qty = max(1, int(qty or 1))
                    po_line = self._find_po_line_for_part(po_pk=po_pk, part_pk=supplier_part_pk)
                    if not po_line:
                        po_line, line_error = self._create_po_line(po_pk=po_pk, part_pk=supplier_part_pk, quantity=qty)
                        if not po_line:
                            part_errors[supplier_part_pk] = line_error or 'failed to create PO line'
                            cprint(f"[BARCODE][PO]\tCreate line failed po={po_pk} supplier_part={supplier_part_pk}: {part_errors[supplier_part_pk]}", silent=False)
                            continue
                        cprint(f"[BARCODE][PO]\tCreated line po={po_pk} supplier_part={supplier_part_pk} qty={qty}", silent=False)

                    ordered_qty, received_qty = self._get_po_line_quantities(po_line)
                    required_order_qty = max(int(ordered_qty or 0), int(received_qty or 0) + qty)

                    line_pk = int((po_line or {}).get('pk') or (po_line or {}).get('id') or 0)
                    if line_pk <= 0:
                        part_errors[supplier_part_pk] = 'invalid PO line id'
                        cprint(f"[BARCODE][PO]\tInvalid line id po={po_pk} supplier_part={supplier_part_pk}", silent=False)
                        continue

                    if required_order_qty > int(ordered_qty or 0):
                        updated, update_error = self._update_po_line_quantity(
                            line_pk=line_pk,
                            quantity=required_order_qty,
                        )
                        if not updated:
                            part_errors[supplier_part_pk] = update_error or 'failed to update PO line quantity'
                            cprint(f"[BARCODE][PO]\tUpdate line qty failed line={line_pk} required={required_order_qty}: {part_errors[supplier_part_pk]}", silent=False)
                            continue
                        cprint(f"[BARCODE][PO]\tUpdated line={line_pk} ordered={ordered_qty}->{required_order_qty}", silent=False)

                    receive_items.append({
                        'line_item': line_pk,
                        'quantity': qty,
                        'packaging': (po_line or {}).get('packaging') or '',
                        'batch_code': (po_line or {}).get('batch') or '',
                    })

                for internal_part_pk, group_data in unresolved_groups.items():
                    qty = max(1, int(group_data.get('qty') or 1))
                    po_line = self._find_po_line_for_internal_part(po_pk=po_pk, internal_part_pk=int(internal_part_pk))
                    if not po_line:
                        detail = f'no existing PO line found for part_pk={internal_part_pk}'
                        cprint(f"[BARCODE][PO]\t{detail}", silent=False)
                        for item in group_data.get('items') or []:
                            unresolved_item_errors[int(item.get('idx') or 0)] = detail
                        continue

                    ordered_qty, received_qty = self._get_po_line_quantities(po_line)
                    required_order_qty = max(int(ordered_qty or 0), int(received_qty or 0) + qty)
                    line_pk = int((po_line or {}).get('pk') or (po_line or {}).get('id') or 0)
                    if line_pk <= 0:
                        detail = f'invalid PO line id for part_pk={internal_part_pk}'
                        cprint(f"[BARCODE][PO]\t{detail}", silent=False)
                        for item in group_data.get('items') or []:
                            unresolved_item_errors[int(item.get('idx') or 0)] = detail
                        continue

                    if required_order_qty > int(ordered_qty or 0):
                        updated, update_error = self._update_po_line_quantity(
                            line_pk=line_pk,
                            quantity=required_order_qty,
                        )
                        if not updated:
                            detail = update_error or 'failed to update PO line quantity'
                            cprint(f"[BARCODE][PO]\tUpdate line qty failed line={line_pk} required={required_order_qty}: {detail}", silent=False)
                            for item in group_data.get('items') or []:
                                unresolved_item_errors[int(item.get('idx') or 0)] = detail
                            continue

                    receive_items.append({
                        'line_item': line_pk,
                        'quantity': qty,
                        'packaging': (po_line or {}).get('packaging') or '',
                        'batch_code': (po_line or {}).get('batch') or '',
                    })

                if not part_errors and receive_items:
                    received, recv_error = self._receive_po_items(
                        po_pk=po_pk,
                        items=receive_items,
                        location_pk=receive_location_pk,
                    )
                    if not received:
                        detail = recv_error or 'receive failed'
                        cprint(f"[BARCODE][PO]\tReceive failed po={po_pk}: {detail}", silent=False)
                        for item in group['items']:
                            item['ok'] = False
                            item['failure'] = f"{item['row'].search_name}: PO flow failed ({detail})"
                        continue

                    cprint(f"[BARCODE][PO]\tReceived PO pk={po_pk} items={len(receive_items)} location={receive_location_pk}", silent=False)

                if not part_errors:
                    self._trigger_po_action(po_pk, 'complete')
                    cprint(f"[BARCODE][PO]\tCompleted PO pk={po_pk}", silent=False)

                for item in group['items']:
                    supplier_part_pk = int(item.get('supplier_part_pk') or 0)
                    if supplier_part_pk in part_errors:
                        item['ok'] = False
                        item['failure'] = f"{item['row'].search_name}: PO flow failed ({part_errors[supplier_part_pk]})"
                    item_idx = int(item.get('idx') or 0)
                    if item_idx in unresolved_item_errors:
                        item['ok'] = False
                        item['failure'] = f"{item['row'].search_name}: PO flow failed ({unresolved_item_errors[item_idx]})"

        if assign_all_stock_items_location and selected_location_pk > 0:
            existing_success_rows = [
                item for item in row_results
                if item.get('ok') and item.get('existing_part') and int(item.get('part_pk') or 0) > 0
            ]
            part_to_rows: Dict[int, List[Dict]] = {}
            for item in existing_success_rows:
                part_to_rows.setdefault(int(item['part_pk']), []).append(item)

            all_transfer_items: List[Dict] = []
            collect_errors: List[str] = []

            for part_pk in sorted(part_to_rows.keys()):
                transfer_items, part_error = self._collect_transfer_items_for_part(
                    part_pk=part_pk,
                    location_pk=selected_location_pk,
                )
                if part_error:
                    collect_errors.append(f'part_pk={part_pk}: {part_error}')
                if transfer_items:
                    all_transfer_items.extend(transfer_items)

            bulk_ok, bulk_error = self._transfer_stock_items_bulk(
                transfer_items=all_transfer_items,
                location_pk=selected_location_pk,
            )
            if bulk_ok:
                cprint(f'[BARCODE]\tStock items updated (items={len(all_transfer_items)}, parts={len(part_to_rows)})', silent=False)
            else:
                cprint('[BARCODE]\tCombined stock transfer failed for existing parts', silent=False)
                detail = bulk_error or 'Bulk stock transfer request failed'
                for item in existing_success_rows:
                    item['ok'] = False
                    item['failure'] = f"{item['row'].search_name}: stock location assignment failed ({detail})"

            for warning in collect_errors[:5]:
                cprint(f'[BARCODE]\tStock collect warning: {warning}', silent=False)

        failed_rows: List[BarcodeScannedRow] = []
        for result in sorted(row_results, key=lambda item: item.get('idx', 0)):
            if result.get('ok'):
                success += 1
            else:
                failed += 1
                if result.get('failure'):
                    failures.append(result['failure'])
                if result.get('row'):
                    failed_rows.append(result['row'])

        # Remove only successful rows; keep failed ones in the list for inspection/retry.
        if failed == 0:
            self.scanned_rows.clear()
            self.fields['category_select'].value = None
            self.fields['location_select'].value = None
            try:
                self.fields['category_select'].update()
                self.fields['location_select'].update()
            except Exception:
                pass
        else:
            self.scanned_rows = failed_rows

        self._update_results_table()

        self.fields['import_progress'].value = 1.0
        self.fields['import_progress'].color = 'green' if failed == 0 else ('amber' if success > 0 else 'red')
        self.fields['import_progress_message'].value = f'Import finished: {success} success, {failed} failed'
        self.fields['import_progress_message'].color = 'green' if failed == 0 else 'orange'
        self.fields['import_progress'].update()
        self.fields['import_progress_message'].update()

        elapsed_total = (time.perf_counter() - op_start_ts) * 1000.0
        cprint(f'[BARCODE]\tTotal operation time: {elapsed_total:.1f} ms ({total} rows)', silent=False)
        if total:
            cprint(f'[BARCODE]\tAverage per row: {elapsed_total/total:.1f} ms', silent=False)
        cprint(f'[BARCODE]\tImport finished: success={success} failed={failed}', silent=False)

        self._show_status(f'Import complete: {success} success, {failed} failed', color='green' if failed == 0 else 'orange')

        if failed == 0:
            # All good — brief snackbar is enough.
            self.show_dialog(DialogType.VALID, f'All {success} item(s) imported successfully.')
        else:
            # Show a blocking modal so the user can read each failure before dismissing.
            failure_items = [
                ft.Row([
                    ft.Icon(ft.icons.ERROR_OUTLINE, color=ft.colors.RED_400, size=16),
                    ft.Text(f, size=12, selectable=True, expand=True),
                ], spacing=6)
                for f in failures
            ]
            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Row([
                    ft.Icon(ft.icons.WARNING_AMBER_ROUNDED, color=ft.colors.AMBER_700),
                    ft.Text(
                        f'Import finished with {failed} failure{"s" if failed != 1 else ""}',
                        weight=ft.FontWeight.BOLD,
                    ),
                ], spacing=8),
                content=ft.Container(
                    width=520,
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                f'{success} succeeded, {failed} failed. '
                                'Failed items remain in the list.',
                                size=13,
                                color=ft.colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Divider(height=8),
                            ft.Column(
                                controls=failure_items,
                                scroll=ft.ScrollMode.AUTO,
                                spacing=4,
                                height=min(320, len(failure_items) * 36 + 16),
                            ),
                        ],
                        spacing=4,
                        tight=True,
                    ),
                ),
                actions=[
                    ft.TextButton(
                        'OK',
                        on_click=lambda e: (
                            setattr(dlg, 'open', False),
                            self._page.update(),
                            self.focus_barcode_input(),
                        ),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            self._page.dialog = dlg
            dlg.open = True
            try:
                self._page.update()
            except AssertionError:
                pass
    
    @staticmethod
    def _resolve_supplier_key(supplier: str) -> str:
        """Map barcode supplier name to InvenTree supplier name."""
        mapping = {
            'tme': 'TME',
            'lcsc': 'LCSC',
            'mouser': 'Mouser',
            'digikey': 'Digi-Key',
        }
        return mapping.get(supplier.lower(), supplier)


class BarcodeAssignmentView(MainView):
    """Assign location / barcode to existing parts from scanned codes."""

    title = 'Assign'
    fields = {}

    def __init__(self, page: ft.Page):
        self.parser = BarcodeParser()
        self.scanned_rows: List[ExistingPartScanRow] = []
        self._row_counter = 0
        self._rows_lock = threading.Lock()
        self._last_scan_code = ''
        self._last_scan_ts = 0.0
        self._recent_scan_codes: Dict[str, float] = {}
        # Reuse one HTTP session and cache repeated API lookups for faster scans.
        self._http = requests.Session()
        self._part_lookup_cache: Dict[str, Optional[Dict]] = {}
        self._part_lookup_lock = threading.Lock()
        self._part_lookup_inflight: Dict[str, threading.Event] = {}
        self._location_name_cache: Dict[int, str] = {}
        self._location_path_to_pk_cache: Dict[str, int] = {}
        self._location_pk_to_path_cache: Dict[int, str] = {}
        self._location_path_map_loaded = False
        self._part_barcodes_cache: Dict[int, List[str]] = {}
        self._barcode_endpoint_available: Optional[bool] = None
        self._results_table_last_update_ts = 0.0
        self._results_table_min_update_interval_s = 0.15
        self._connect_lock = threading.Lock()
        super().__init__(page=page)
        self.build_page()

    def build_page(self) -> None:
        self.fields['barcode_input'] = ft.TextField(
            label='Scan barcode / part number',
            multiline=True,
            min_lines=3,
            max_lines=6,
            on_submit=self._on_code_submit,
            on_change=self._on_code_changed,
            autofocus=True,
            hint_text='Scan one code per line. Unknown supplier formats are accepted as raw lookup values.',
        )

        self.fields['parse_codes'] = ft.ElevatedButton(
            text='Parse Codes',
            on_click=self._parse_batch_codes,
        )
        self.fields['clear_input'] = ft.ElevatedButton(
            text='Clear Input',
            on_click=lambda _: setattr(self.fields['barcode_input'], 'value', '') or self.fields['barcode_input'].update(),
        )

        self.fields['clear_all_rows'] = ft.IconButton(
            icon=ft.icons.DELETE_SWEEP,
            tooltip='Clear all queued items',
            on_click=self._clear_all_rows,
        )

        self.fields['results_table'] = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('Input Code')),
                ft.DataColumn(ft.Text('Supplier')),
                ft.DataColumn(ft.Text('Lookup')),
                ft.DataColumn(ft.Text('Status')),
                ft.DataColumn(ft.Text('Part')),
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Barcode')),
                ft.DataColumn(ft.Text('Remove')),
            ],
            rows=[],
            horizontal_lines=ft.border.BorderSide(1, ft.colors.OUTLINE),
        )

        self.fields['location_select'] = DropdownWithSearch(
            label='Stock Location (All Valid Items)',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
        )
        self.fields['reload_locations'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload stock locations from InvenTree',
            on_click=self._reload_locations,
        )

        self.fields['assign_location_check'] = ft.Checkbox(
            label='Assign selected location to all valid items',
            value=True,
        )
        self.fields['assign_all_stock_items_location_check'] = ft.Checkbox(
            label='Also assign selected location to all stock items of each part',
            value=False,
        )
        self.fields['reassign_name_barcode_check'] = ft.Checkbox(
            label='Reassign external barcode using part name (not IPN)',
            value=False,
        )
        self.fields['force_barcode_reassign_check'] = ft.Checkbox(
            label='Force barcode reassignment if barcode already exists (skips if is already part name)',
            value=False,
        )

        self.fields['apply_assignments'] = ft.ElevatedButton(
            text='Apply To Valid Items',
            on_click=self._apply_assignments,
            color='white',
            bgcolor='green',
            width=220,
        )

        self.fields['status_message'] = ft.Text(value='Ready to scan', size=12, color='blue')
        self.fields['progress'] = ft.ProgressBar(value=0, visible=False, height=8)
        self.fields['progress_message'] = ft.Text(value='', size=11, color='blue')

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row([ft.Text('Assign Existing Parts', style=ft.TextThemeStyle.HEADLINE_MEDIUM)]),
                            ft.Divider(),
                            ft.Text('1. Scan or Paste Codes:', style=ft.TextThemeStyle.BODY_LARGE),
                            self.fields['barcode_input'],
                            ft.Row([self.fields['parse_codes'], self.fields['clear_input'], self.fields['clear_all_rows']]),
                            ft.Divider(),
                            ft.Text('2. Review Existing Part Matches:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Container(content=self.fields['results_table'], expand=True),
                            ft.Divider(),
                            ft.Text('3. Configure Assignments:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Row([self.fields['location_select'], self.fields['reload_locations']]),
                            self.fields['assign_location_check'],
                            self.fields['assign_all_stock_items_location_check'],
                            self.fields['reassign_name_barcode_check'],
                            self.fields['force_barcode_reassign_check'],
                            ft.Divider(),
                            ft.Row([
                                self.fields['apply_assignments'],
                                ft.ElevatedButton(text='Back', on_click=lambda _: self._page.go('/main/create')),
                            ]),
                            self.fields['progress'],
                            self.fields['progress_message'],
                            self.fields['status_message'],
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=10,
                    ),
                    padding=20,
                    expand=True,
                ),
            ],
            expand=True,
        )

        self.focus_input()

    def did_mount(self):
        self._load_locations()
        self.focus_input()
        return super().did_mount()

    def focus_input(self):
        try:
            self.fields['barcode_input'].focus()
            self.fields['barcode_input'].update()
        except AssertionError:
            pass

    def _set_status(self, message: str, color: str = 'black'):
        self.fields['status_message'].value = message
        self.fields['status_message'].color = color
        try:
            self.fields['status_message'].update()
        except AssertionError:
            pass

    def _connect_server_with_retries(
            self,
            attempts: int = 3,
            delay_seconds: float = 1.5,
            row: Optional[ExistingPartScanRow] = None,
    ) -> bool:
        """Try connecting to InvenTree multiple times before failing."""
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if api_obj and getattr(api_obj, 'token', None) and getattr(api_obj, 'base_url', None):
            return True

        # Single-flight connect: avoid duplicate expensive reconnects from parallel row threads.
        with self._connect_lock:
            api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
            if api_obj and getattr(api_obj, 'token', None) and getattr(api_obj, 'base_url', None):
                return True

            for attempt in range(1, attempts + 1):
                if row is not None:
                    row.status = f'Checking server ({attempt}/{attempts})...'
                    self._update_results_table_throttled()

                if inventree_interface.connect_to_server(force_reconnect=(attempt > 1)):
                    return True

                if attempt < attempts:
                    if row is not None:
                        row.status = f'Server offline, retrying ({attempt}/{attempts - 1})...'
                        self._update_results_table_throttled()
                    else:
                        self._set_status(
                            f'InvenTree offline. Retrying ({attempt}/{attempts - 1}) in {delay_seconds:.1f}s...',
                            color='orange',
                        )
                    time.sleep(delay_seconds)

        return False

    def _load_locations(self, reload: bool = False):
        try:
            if reload:
                self._location_path_map_loaded = False
                self._location_name_cache.clear()
                self._location_path_to_pk_cache.clear()
                self._location_pk_to_path_cache.clear()

            location_list = inventree_interface.build_stock_location_tree(reload=reload)
            self.fields['location_select'].options = [ft.dropdown.Option(location) for location in location_list]

            # Keep the control explicitly interactive on initial route render.
            self.fields['location_select'].disabled = False
            self.fields['location_select'].done_search()
            try:
                self.fields['location_select'].update()
            except AssertionError:
                pass

            self._page.update()
        except Exception as exc:
            cprint(f'[ERROR] Failed to load stock locations: {exc}', silent=False)

    def _reload_locations(self, _):
        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            self.show_dialog(DialogType.ERROR, 'ERROR: Failed to connect to InvenTree server')
            return
        self._load_locations(reload=True)
        self._set_status('Stock locations reloaded', color='green')

    def _request_with_retries(
            self,
            method: str,
            url: str,
            attempts: int = 3,
            delay_seconds: float = 1.0,
            row: Optional[ExistingPartScanRow] = None,
            **kwargs,
    ) -> Optional[requests.Response]:
        """Run HTTP request with retry on transient/network failures."""
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(method=method.upper(), url=url, **kwargs)
                # Retry transient upstream failures and rate limits.
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f'HTTP {response.status_code}', response=response)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                status_code = None
                try:
                    status_code = int(getattr(getattr(exc, 'response', None), 'status_code', 0))
                except Exception:
                    status_code = None

                # Do not retry most client-side request errors (except 429).
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    return None

                if attempt < attempts:
                    if row is not None:
                        row.status = f'Network retry ({attempt}/{attempts - 1})...'
                        self._update_results_table_throttled()
                    else:
                        self._set_status(
                            f'Network issue. Retrying ({attempt}/{attempts - 1}) in {delay_seconds:.1f}s...',
                            color='orange',
                        )
                    time.sleep(delay_seconds)
                else:
                    return None

        return None

    def _on_code_submit(self, _):
        text = (self.fields['barcode_input'].value or '').strip()
        if not text:
            return

        if self._enqueue_code(text):
            self.fields['barcode_input'].value = ''
            self.fields['barcode_input'].update()
            self.focus_input()

    def _on_code_changed(self, _):
        text = self.fields['barcode_input'].value or ''
        if '\n' not in text and '\r' not in text:
            return

        lines = [line.strip() for line in text.replace('\r', '\n').split('\n') if line.strip()]
        success = 0
        for line in lines:
            if self._enqueue_code(line, update_table=False):
                success += 1

        self._update_results_table()
        self.fields['barcode_input'].value = ''
        self.fields['barcode_input'].update()
        self.focus_input()
        self._set_status(f'Queued {success} item(s) for validation', color='blue')

    def _parse_batch_codes(self, _):
        text = (self.fields['barcode_input'].value or '').strip()
        if not text:
            self._set_status('No input provided', color='red')
            return

        lines = [line.strip() for line in text.split('\n') if line.strip()]
        success = 0
        for line in lines:
            if self._enqueue_code(line, update_table=False):
                success += 1

        self._update_results_table()
        self.fields['barcode_input'].value = ''
        self.fields['barcode_input'].update()
        self.focus_input()
        self._set_status(f'Queued {success} item(s) for validation', color='blue')

    def _enqueue_code(self, raw_code: str, update_table: bool = True) -> bool:
        code = str(raw_code or '').strip()
        if not code:
            return False

        now = time.monotonic()
        recent_ts = self._recent_scan_codes.get(code)
        if recent_ts is not None and (now - recent_ts) < 0.5:
            cprint(f'[ASSIGN]\tIgnored duplicate scan: {code}', silent=False)
            return False

        self._recent_scan_codes[code] = now
        self._last_scan_code = code
        self._last_scan_ts = now

        parsed = self.parser.parse(code)
        supplier = parsed.get('supplier', 'unknown')
        lookup_value = parsed.get('barcode', '') or parsed.get('manufacturer_pn', '') or parsed.get('supplier_pn', '') or code

        with self._rows_lock:
            self._row_counter += 1
            row = ExistingPartScanRow(
                row_id=self._row_counter,
                raw_code=code,
                supplier=supplier,
                lookup_value=lookup_value,
            )
            self.scanned_rows.append(row)

        if update_table:
            self._update_results_table()

        thread = threading.Thread(target=self._validate_row_async, args=(row.row_id,), daemon=True)
        thread.start()
        return True

    def _validate_row_async(self, row_id: int):
        import time
        row_start_ts = time.perf_counter()
        
        with self._rows_lock:
            row = next((r for r in self.scanned_rows if r.row_id == row_id), None)
        if not row:
            return

        try:
            row.status = 'Checking server...'
            self._update_results_table_throttled()

            ts_server = time.perf_counter()
            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.0, row=row):
                row.status = 'Server offline (after retries)'
                self._update_results_table_throttled(force=True)
                return
            elapsed_server = (time.perf_counter() - ts_server) * 1000.0
            cprint(f'[ASSIGN]\tValidation row {row_id}: server check {elapsed_server:.1f} ms', silent=False)

            row.status = 'Checking part...'
            self._update_results_table_throttled()

            ts_part_lookup = time.perf_counter()
            part = self._find_part_by_lookup(row.lookup_value)
            elapsed_part_lookup = (time.perf_counter() - ts_part_lookup) * 1000.0
            
            if not part:
                row.status = 'Part not found'
                cprint(f'[ASSIGN]\tValidation row {row_id}: part lookup failed {elapsed_part_lookup:.1f} ms', silent=False)
            else:
                cprint(f'[ASSIGN]\tValidation row {row_id}: part lookup {elapsed_part_lookup:.1f} ms', silent=False)
                
                row.part_pk = int(part.get('pk') or part.get('id'))
                row.part_name = str(part.get('name') or part.get('IPN') or row.lookup_value)
                try:
                    row.default_location_pk = int(part.get('default_location') or 0)
                except Exception:
                    row.default_location_pk = 0
                
                ts_location = time.perf_counter()
                full_location = self._resolve_location_string(part)
                row.location = self._location_leaf(full_location)
                elapsed_location = (time.perf_counter() - ts_location) * 1000.0
                cprint(f'[ASSIGN]\tValidation row {row_id}: location resolve {elapsed_location:.1f} ms', silent=False)

                ts_barcodes = time.perf_counter()
                row.current_barcodes = self._fetch_part_barcodes(part_pk=row.part_pk)
                row.barcode_hash = str(part.get('barcode_hash') or '').strip()
                if not row.current_barcodes and row.barcode_hash:
                    # In some InvenTree versions GET /api/barcode/ is not available.
                    # Fall back to part name (user-facing barcode label in this workflow).
                    if row.part_name:
                        row.current_barcodes = [row.part_name]
                    else:
                        generated = self._generate_part_barcode(part_pk=row.part_pk)
                        if generated:
                            row.current_barcodes = [generated]
                row.has_barcode = bool(row.current_barcodes) or bool(row.barcode_hash)
                elapsed_barcodes = (time.perf_counter() - ts_barcodes) * 1000.0
                cprint(f'[ASSIGN]\tValidation row {row_id}: barcode ops {elapsed_barcodes:.1f} ms', silent=False)

                has_location = bool(row.location and row.location != '-' and row.location.lower() != 'none')
                if has_location and row.has_barcode:
                    row.status = 'Valid (location+barcode)'
                elif has_location:
                    row.status = 'Missing barcode'
                elif row.has_barcode:
                    row.status = 'Missing location'
                else:
                    row.status = 'Missing location+barcode'
        except Exception as exc:
            row.status = f'Error: {str(exc)[:40]}'

        elapsed_total = (time.perf_counter() - row_start_ts) * 1000.0
        cprint(f'[ASSIGN]\tValidation row {row_id} total: {elapsed_total:.1f} ms', silent=False)
        
        self._update_results_table_throttled(force=True)

    def _find_part_by_lookup(self, lookup_value: str) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_part_by_lookup(self, lookup_value)

    def _ensure_location_path_cache(self):
        """Load full stock-location id->path map once to avoid per-row tree API calls."""
        if self._location_path_map_loaded:
            return
        self._location_path_map_loaded = True

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return

        endpoint = f"{base_url.rstrip('/')}/api/stock/location/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }

        items: List[Dict] = []
        next_url = endpoint
        next_params = {'limit': 250}

        while next_url:
            response = self._request_with_retries(
                method='GET',
                url=next_url,
                headers=headers,
                params=next_params,
                timeout=20,
            )
            if response is None:
                break

            payload = response.json()
            if isinstance(payload, dict):
                rows = payload.get('results') or []
                next_url = payload.get('next')
                next_params = {}
            elif isinstance(payload, list):
                rows = payload
                next_url = None
            else:
                rows = []
                next_url = None

            for row in rows:
                if isinstance(row, dict):
                    items.append(row)

        if not items:
            return

        node_name: Dict[int, str] = {}
        node_parent: Dict[int, Optional[int]] = {}
        for item in items:
            try:
                item_pk = int(item.get('pk') or item.get('id'))
            except Exception:
                continue

            name = str(item.get('name') or '').strip()
            parent_val = item.get('parent')
            parent_pk = None
            try:
                if parent_val not in [None, '', 'None']:
                    parent_pk = int(parent_val)
            except Exception:
                parent_pk = None

            node_name[item_pk] = name
            node_parent[item_pk] = parent_pk

        visited_cache: Dict[int, str] = {}

        def build_path(pk: int) -> str:
            if pk in visited_cache:
                return visited_cache[pk]

            names: List[str] = []
            seen: set = set()
            cur = pk
            while cur and cur not in seen:
                seen.add(cur)
                cur_name = node_name.get(cur, '')
                if cur_name:
                    names.append(cur_name)
                cur = node_parent.get(cur)

            path = '/'.join(reversed(names)) if names else str(pk)
            visited_cache[pk] = path
            return path

        for loc_pk in node_name.keys():
            path = build_path(loc_pk)
            self._location_name_cache[loc_pk] = path
            self._location_path_to_pk_cache[path] = loc_pk

    def _normalize_stock_location_value(self, location: str) -> str:
        return _BarcodeApiHelpers.normalize_stock_location_value(location)

    def _get_stock_location_pk(self, location: str) -> int:
        location_pk = inventree_interface.resolve_stock_location_pk(location, self._location_path_to_pk_cache)
        if location_pk > 0:
            cprint(f'[ASSIGN]\tResolved stock location from cache: {location} -> pk={location_pk}', silent=False)
        else:
            cprint(f'[ASSIGN]\tStock location cache miss: {location}', silent=False)
        return location_pk

    def _resolve_location_string(self, part: Dict) -> str:
        return _BarcodeApiHelpers.resolve_location_string(part)

    def _fetch_part_barcodes(self, part_pk: int) -> List[str]:
        return _BarcodeApiHelpers.fetch_part_barcodes(self, part_pk)

    def _generate_part_barcode(self, part_pk: int) -> str:
        """Generate or fetch internal barcode string for a part via barcode API."""
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return ''

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return ''

        endpoint = f"{base_url.rstrip('/')}/api/barcode/generate/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        try:
            response = self._request_with_retries(
                method='POST',
                url=endpoint,
                headers=headers,
                json={'model': 'part', 'pk': int(part_pk)},
                timeout=20,
            )
            if response is None:
                return ''
            payload = response.json()
            return str(payload.get('barcode') or '').strip()
        except Exception:
            return ''

    def _update_all_stock_items_location(self, part_pk: int, location_pk: int) -> tuple[int, int, str]:
        """Update location for all stock items that belong to a part.

        Returns: (updated_count, failed_count, error_message)
        """
        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            return 0, 0, 'Missing InvenTree API object'

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            return 0, 0, 'Missing InvenTree auth context'

        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        updated = 0
        failed = 0

        try:
            url = f"{base_url.rstrip('/')}/api/stock/"
            params = {'part': part_pk, 'limit': 250}
            stock_items = []

            while url:
                response = self._request_with_retries(
                    method='GET',
                    url=url,
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                if response is None:
                    return updated, failed, 'Failed to list stock items after retries'
                payload = response.json()

                if isinstance(payload, dict):
                    rows = payload.get('results') or []
                    next_url = payload.get('next')
                elif isinstance(payload, list):
                    rows = payload
                    next_url = None
                else:
                    rows = []
                    next_url = None

                for item in rows:
                    item_pk = item.get('pk') or item.get('id')
                    if not item_pk:
                        continue

                    current_loc = item.get('location')
                    try:
                        if current_loc is not None and int(current_loc) == int(location_pk):
                            # No-op: already assigned to requested location.
                            continue
                    except Exception:
                        pass

                    stock_items.append(item)

                url = next_url
                params = {}

            if not stock_items:
                return 0, 0, ''

            # Fast path: try server-side bulk transfer once for all affected stock items.
            transfer_items = []
            for item in stock_items:
                try:
                    transfer_items.append(
                        {
                            'pk': int(item.get('pk') or item.get('id')),
                            'quantity': str(item.get('quantity') or '0'),
                            'batch': str(item.get('batch') or ''),
                            'packaging': str(item.get('packaging') or ''),
                            'status': int(item.get('status') or 0),
                        }
                    )
                except Exception:
                    continue

            transfer_endpoint = f"{base_url.rstrip('/')}/api/stock/transfer/"
            transfer_payload = {
                'items': transfer_items,
                'location': int(location_pk),
                'notes': 'Ki-nTree bulk location update',
            }
            transfer_response = self._request_with_retries(
                method='POST',
                url=transfer_endpoint,
                headers=headers,
                json=transfer_payload,
                timeout=30,
            )

            if transfer_response is not None and transfer_response.status_code in [200, 201, 202]:
                return len(transfer_items), 0, ''

            # Compatibility fallback: patch each stock item one-by-one.
            cprint('[ASSIGN]\tBulk stock transfer unavailable, falling back to per-item PATCH', silent=False)
            for item in stock_items:
                item_pk = item.get('pk') or item.get('id')
                if not item_pk:
                    continue

                patch_url = f"{base_url.rstrip('/')}/api/stock/{item_pk}/"
                patch_resp = self._request_with_retries(
                    method='PATCH',
                    url=patch_url,
                    headers=headers,
                    json={'location': int(location_pk)},
                    timeout=20,
                )

                if patch_resp is not None and patch_resp.status_code in [200, 202]:
                    updated += 1
                else:
                    failed += 1

            return updated, failed, ''
        except Exception as exc:
            return updated, failed, str(exc)

    def _collect_transfer_items_for_part(self, part_pk: int, location_pk: int) -> tuple[List[Dict], str]:
        return _BarcodeApiHelpers.collect_transfer_items_for_part(self, part_pk, location_pk)

    def _transfer_stock_items_bulk(self, transfer_items: List[Dict], location_pk: int) -> tuple[bool, str]:
        return _BarcodeApiHelpers.transfer_stock_items_bulk(self, transfer_items, location_pk)

    def _set_part_default_location(self, part_pk: int, location_pk: int) -> bool:
        return _BarcodeApiHelpers.set_part_default_location(self, part_pk, location_pk)

    def _link_part_barcode(self, part_pk: int, barcode_value: str) -> bool:
        return _BarcodeApiHelpers.link_part_barcode(self, part_pk, barcode_value)

    @staticmethod
    def _location_leaf(location: str) -> str:
        """Return only the last path segment for location display."""
        text = str(location or '').strip()
        if not text:
            return '-'
        if text.lower() == 'none':
            return '-'
        parts = [part.strip() for part in text.split('/') if part.strip()]
        return parts[-1] if parts else text

    def _remove_row(self, row_id: int):
        with self._rows_lock:
            self.scanned_rows = [row for row in self.scanned_rows if row.row_id != row_id]
        self._update_results_table()

    def _clear_all_rows(self, _):
        with self._rows_lock:
            if not self.scanned_rows:
                return
            self.scanned_rows.clear()
            self._recent_scan_codes.clear()
        self._update_results_table()
        self._set_status('Cleared all queued items', color='blue')

    def _update_results_table(self):
        with self._rows_lock:
            rows_snapshot = list(self.scanned_rows)

        table_rows = []
        for row in rows_snapshot:
            status_color = 'green' if row.status.startswith('Valid') else ('red' if 'not found' in row.status.lower() or 'error' in row.status.lower() else 'blue')
            if row.current_barcodes:
                barcode_text = ', '.join(row.current_barcodes[:2])
            else:
                barcode_text = 'No'
            if len(row.current_barcodes) > 2:
                barcode_text += f' (+{len(row.current_barcodes) - 2})'
            part_text = f"{row.part_name} ({row.part_pk})" if row.part_pk else '-'

            display_code = self._truncate_text(row.display_code)
            supplier_text = self._truncate_text(row.supplier.upper())
            lookup_text = self._truncate_text(row.lookup_value)
            status_text = self._truncate_text(row.status)
            part_text = self._truncate_text(part_text)
            location_text = self._truncate_text(row.location or '-')
            barcode_text = self._truncate_text(barcode_text)

            table_rows.append(ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(display_code, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(supplier_text, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(lookup_text, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(status_text, color=status_color, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(part_text, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(location_text, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(barcode_text, size=12, no_wrap=True)),
                    ft.DataCell(ft.IconButton(icon=ft.icons.DELETE, on_click=lambda _, rid=row.row_id: self._remove_row(rid))),
                ]
            ))

        self.fields['results_table'].rows = table_rows
        try:
            self._page.update()
        except AssertionError:
            pass

    def _update_results_table_throttled(self, force: bool = False):
        now = time.monotonic()
        if not force and (now - self._results_table_last_update_ts) < self._results_table_min_update_interval_s:
            return
        self._results_table_last_update_ts = now
        self._update_results_table()

    def _apply_assignments(self, _):
        import time
        op_start_ts = time.perf_counter()
        
        with self._rows_lock:
            valid_rows = [row for row in self.scanned_rows if row.part_pk]

        if not valid_rows:
            self.show_dialog(DialogType.ERROR, 'No valid items to update')
            return

        ts_server_check = time.perf_counter()
        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            self.show_dialog(DialogType.ERROR, 'Failed to connect to InvenTree server after 3 retries')
            return
        elapsed_server_check = (time.perf_counter() - ts_server_check) * 1000.0
        cprint(f'[ASSIGN]\tServer check: {elapsed_server_check:.1f} ms', silent=False)

        apply_location = bool(self.fields['assign_location_check'].value)
        apply_stock_items_location = bool(self.fields['assign_all_stock_items_location_check'].value)
        reassign_barcode = bool(self.fields['reassign_name_barcode_check'].value)
        force_reassign = bool(self.fields['force_barcode_reassign_check'].value)

        if apply_stock_items_location:
            # Stock item location assignment requires a selected location.
            apply_location = True

        location_value = None
        location_pk = 0
        if apply_location:
            location_value = str(self.fields['location_select'].value or '').strip()
            if not location_value:
                self.show_dialog(DialogType.ERROR, 'Select a stock location before applying')
                return
            cprint(f'[ASSIGN]\tUsing selected stock location: {location_value}', silent=False)
            ts_loc_resolve = time.perf_counter()
            location_pk = self._get_stock_location_pk(location_value)
            elapsed_loc_resolve = (time.perf_counter() - ts_loc_resolve) * 1000.0
            cprint(f'[ASSIGN]\tInitial stock location resolve: pk={location_pk} ({elapsed_loc_resolve:.1f} ms)', silent=False)

        success = 0
        failed = 0
        failures = []
        successful_row_ids = []
        total = len(valid_rows)

        self.fields['progress'].visible = True
        self.fields['progress'].value = 0
        self.fields['progress_message'].value = f'Processing 0/{total}'
        self.fields['progress_message'].color = 'blue'
        self.fields['progress'].update()
        self.fields['progress_message'].update()

        def _process_row(idx: int, row: ExistingPartScanRow) -> Dict:
            ts_row_start = time.perf_counter()
            row_failures: List[str] = []
            row_ok = True
            location_stage_ok = True

            try:
                if apply_location:
                    ts_loc_update = time.perf_counter()
                    if location_pk > 0:
                        if int(row.default_location_pk or 0) == int(location_pk):
                            set_ok = True
                        else:
                            set_ok = self._set_part_default_location(row.part_pk, location_pk)
                    else:
                        set_ok = inventree_interface.inventree_set_part_default_location(row.part_pk, location_value)
                    elapsed_loc_update = (time.perf_counter() - ts_loc_update) * 1000.0
                    cprint(f'[ASSIGN]\t  Row {idx}: part_default_location update ({elapsed_loc_update:.1f} ms)', silent=False)

                    if not set_ok:
                        row_ok = False
                        location_stage_ok = False
                        row_failures.append(f'{row.part_name}: default location update failed')
                    elif location_pk > 0:
                        row.default_location_pk = int(location_pk)

                    if apply_stock_items_location and location_pk <= 0:
                        row_ok = False
                        location_stage_ok = False
                        row_failures.append(f'{row.part_name}: stock location assignment failed (invalid location)')

                if reassign_barcode and row.part_name:
                    if row.has_barcode and not force_reassign:
                        cprint(f'[ASSIGN]\t  Row {idx}: barcode exists, skipped', silent=False)
                    else:
                        current_barcodes_normalized = {
                            str(value or '').strip().lower()
                            for value in (row.current_barcodes or [])
                            if str(value or '').strip()
                        }
                        if row.part_name.strip().lower() in current_barcodes_normalized:
                            cprint(f'[ASSIGN]\t  Row {idx}: barcode already matches part name, skipped', silent=False)
                        else:
                            ts_barcode = time.perf_counter()
                            barcode_ok = self._link_part_barcode(row.part_pk, row.part_name)
                            elapsed_barcode = (time.perf_counter() - ts_barcode) * 1000.0
                            cprint(f'[ASSIGN]\t  Row {idx}: barcode link ({elapsed_barcode:.1f} ms)', silent=False)
                            if not barcode_ok:
                                row_ok = False
                                row_failures.append(f'{row.part_name}: barcode reassignment failed')
            except Exception as exc:
                row_ok = False
                row_failures.append(f'{row.part_name or row.lookup_value}: {str(exc)[:60]}')

            elapsed_row = (time.perf_counter() - ts_row_start) * 1000.0
            cprint(f'[ASSIGN]\tRow {idx} total: {elapsed_row:.1f} ms', silent=False)

            return {
                'idx': idx,
                'row': row,
                'row_ok': row_ok,
                'location_stage_ok': location_stage_ok,
                'failures': row_failures,
            }

        max_workers = min(4, max(1, len(valid_rows)))
        cprint(f'[ASSIGN]\tParallel row workers: {max_workers}', silent=False)

        row_results: List[Dict] = []
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_process_row, idx, row): (idx, row)
                for idx, row in enumerate(valid_rows, start=1)
            }
            for future in as_completed(future_map):
                result = future.result()
                row_results.append(result)
                completed += 1
                self.fields['progress'].value = completed / total if total else 1.0
                self.fields['progress_message'].value = f'Processing {completed}/{total}'
                self.fields['progress'].update()
                self.fields['progress_message'].update()

        if apply_stock_items_location and location_pk > 0:
            ts_stock_phase = time.perf_counter()
            part_to_rows: Dict[int, List[ExistingPartScanRow]] = {}
            all_transfer_items: List[Dict] = []
            collect_errors: List[str] = []
            part_transfer_counts: Dict[int, int] = {}

            for result in row_results:
                row = result['row']
                if not result['location_stage_ok']:
                    continue
                part_to_rows.setdefault(int(row.part_pk), []).append(row)

            for part_pk in sorted(part_to_rows.keys()):
                part_items, part_error = self._collect_transfer_items_for_part(part_pk=part_pk, location_pk=location_pk)
                if part_error:
                    collect_errors.append(f'part_pk={part_pk}: {part_error}')
                if part_items:
                    part_transfer_counts[part_pk] = len(part_items)
                    all_transfer_items.extend(part_items)

            bulk_ok, bulk_error = self._transfer_stock_items_bulk(all_transfer_items, location_pk)
            if bulk_ok:
                elapsed_stock_phase = (time.perf_counter() - ts_stock_phase) * 1000.0
                cprint(
                    f'[ASSIGN]\tCombined stock transfer ({elapsed_stock_phase:.1f} ms, items={len(all_transfer_items)}, parts={len(part_to_rows)})',
                    silent=False,
                )
            else:
                cprint('[ASSIGN]\tCombined stock transfer failed, falling back to per-part updates', silent=False)
                for part_pk in sorted(part_to_rows.keys()):
                    updated, stock_failed, stock_error = self._update_all_stock_items_location(
                        part_pk=part_pk,
                        location_pk=location_pk,
                    )
                    cprint(
                        f'[ASSIGN]\t  Fallback part_pk={part_pk}: updated={updated} failed={stock_failed}',
                        silent=False,
                    )
                    if stock_failed > 0 or stock_error:
                        details = stock_error if stock_error else f'{stock_failed} stock item updates failed'
                        for row in part_to_rows[part_pk]:
                            for result in row_results:
                                if result['row'].row_id == row.row_id:
                                    result['row_ok'] = False
                                    result['failures'].append(f'{row.part_name}: stock location assignment failed ({details})')

            if collect_errors:
                for item in collect_errors[:5]:
                    cprint(f'[ASSIGN]\tStock collect warning: {item}', silent=False)
            if bulk_error:
                cprint(f'[ASSIGN]\tStock transfer warning: {bulk_error}', silent=False)

            if bulk_ok:
                for part_pk, rows in part_to_rows.items():
                    updated_count = int(part_transfer_counts.get(part_pk, 0))
                    for row in rows:
                        cprint(
                            f'[ASSIGN]\t  Row part_pk={part_pk}: stock_items location update (combined, updated={updated_count})',
                            silent=False,
                        )

        for result in sorted(row_results, key=lambda item: item['idx']):
            if result['failures']:
                failures.extend(result['failures'])
            if result['row_ok']:
                success += 1
                successful_row_ids.append(result['row'].row_id)
            else:
                failed += 1

        self.fields['progress'].value = 1.0
        self.fields['progress'].color = 'green' if failed == 0 else ('amber' if success > 0 else 'red')
        self.fields['progress_message'].value = f'Update finished: {success} success, {failed} failed'
        self.fields['progress_message'].color = 'green' if failed == 0 else 'orange'
        self.fields['progress'].update()
        self.fields['progress_message'].update()

        elapsed_total = (time.perf_counter() - op_start_ts) * 1000.0
        cprint(f'[ASSIGN]\tTotal operation time: {elapsed_total:.1f} ms ({total} rows)', silent=False)
        cprint(f'[ASSIGN]\tPer-row average: {elapsed_total/total:.1f} ms', silent=False)

        if failed:
            detail = '\n'.join([f'- {item}' for item in failures[:5]])
            if len(failures) > 5:
                detail += f'\n- ... and {len(failures) - 5} more'
            self.show_dialog(DialogType.WARNING, f'Finished with failures:\n{detail}')
        else:
            self.show_dialog(DialogType.VALID, f'Updated {success} item(s) successfully')

        if successful_row_ids:
            with self._rows_lock:
                self.scanned_rows = [row for row in self.scanned_rows if row.row_id not in successful_row_ids]
            self._update_results_table()

        self._set_status(f'Update finished: {success} success, {failed} failed', color='green' if failed == 0 else 'orange')

    @staticmethod
    def _truncate_text(value: str, max_len: int = 30) -> str:
        text = str(value or '')
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + '...'
