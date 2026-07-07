"""Location verification view for Ki-nTree.

Scan a location to load all its items, then scan item barcodes to verify
presence. Items found turn green; unexpected scans are added in red.
Manual override icon lets the user mark any item as present.
"""

import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

import flet as ft
import requests

from ...common.tools import cprint
from ...database import inventree_interface
from ...search.barcode_parser import BarcodeParser
from .barcode import BarcodeApiMixin
from .barcode import _BarcodeApiHelpers
from .common import DialogType
from .main import MainView


# ---------------------------------------------------------------------------
# Scan result states (for the feedback indicator)
# ---------------------------------------------------------------------------

_SCAN_IDLE = "idle"
_SCAN_CHECKING = "checking"
_SCAN_VERIFIED = "verified"
_SCAN_UNEXPECTED = "unexpected"
_SCAN_DUPLICATE = "duplicate"  # scanned an already-fully-verified part


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class VerifyItem:
    """One expected stock item row in the selected location."""

    STATUS_EXPECTED = "expected"
    STATUS_VERIFIED = "verified"
    STATUS_UNEXPECTED = "unexpected"

    def __init__(
        self,
        part_pk: int,
        part_name: str,
        stock_pk: int,
        quantity: str,
        location: str,
    ):
        self.part_pk = part_pk
        self.part_name = part_name
        self.stock_pk = stock_pk
        self.quantity = quantity
        self.location = location
        self.status = self.STATUS_EXPECTED
        self.manual = False
        # Populated for unexpected items: where InvenTree says this part lives
        self.expected_locations: List[str] = []


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class VerificationView(BarcodeApiMixin, MainView):
    """Scan a location then verify its contents by scanning item barcodes."""

    title = "Verification"
    fields = {}

    # Phase constants
    _PHASE_LOCATION = "location"
    _PHASE_ITEMS = "items"

    def __init__(self, page: ft.Page):
        self._phase = self._PHASE_LOCATION
        self._current_location_pk: Optional[int] = None
        self._current_location_path: str = ""

        self._items: List[VerifyItem] = []
        self._items_lock = threading.Lock()

        self._recent_scan_codes: Dict[str, float] = {}
        self._part_lookup_cache: Dict[str, Optional[Dict]] = {}
        self._part_lookup_lock = threading.Lock()
        self._part_lookup_inflight: Dict[str, threading.Event] = {}

        self._connect_lock = threading.Lock()
        self._http = requests.Session()
        self.parser = BarcodeParser()

        self.stock_locations: List[str] = []
        self.stock_location_id_map: Dict[str, int] = {}

        self._table_last_update_ts = 0.0
        self._table_min_update_interval_s = 0.15

        # Scan feedback indicator state
        self._scan_state = _SCAN_IDLE
        self._scan_state_lock = threading.Lock()
        self._scan_seq = 0  # incremented on every new scan; guards stale CHECKING updates

        super().__init__(page=page)
        self.build_page()

    # ------------------------------------------------------------------ #
    #  Build UI                                                            #
    # ------------------------------------------------------------------ #

    def build_page(self) -> None:
        self.fields["scan_input"] = ft.TextField(
            label="Scan location barcode / name",
            multiline=True,
            min_lines=3,
            max_lines=6,
            on_submit=self._on_input_submit,
            on_change=self._on_input_changed,
            hint_text="Start by scanning or typing a location. Then scan item barcodes.",
            expand=True,
        )

        # Scan feedback indicator: ProgressRing (spinner) while checking,
        # Icon when done. Wrapped in a fixed-size container so layout is stable.
        self._scan_ring = ft.ProgressRing(
            width=36,
            height=36,
            stroke_width=3,
            color="blue",
            visible=False,
        )
        self._scan_icon = ft.Icon(
            name=ft.icons.RADIO_BUTTON_UNCHECKED,
            size=36,
            color="grey",
            visible=True,
        )
        self._scan_indicator = ft.Stack(
            controls=[self._scan_ring, self._scan_icon],
            width=40,
            height=40,
        )

        self.fields["parse_btn"] = ft.ElevatedButton(
            text="Parse",
            on_click=self._on_parse,
        )
        self.fields["clear_input_btn"] = ft.ElevatedButton(
            text="Clear Input",
            on_click=self._on_clear_input,
        )
        self.fields["clear_all_btn"] = ft.IconButton(
            icon=ft.icons.DELETE_SWEEP,
            tooltip="Clear all items and reset",
            on_click=self._on_clear_all,
        )
        self.fields["reload_locations"] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip="Reload stock locations from InvenTree",
            on_click=self._reload_locations,
        )

        # Phase indicator
        self._phase_text = ft.Text(
            "Phase: Select Location",
            size=13,
            weight=ft.FontWeight.BOLD,
            color="blue",
        )
        self._location_text = ft.Text(
            "",
            size=13,
            italic=True,
            color="grey",
        )

        # Summary counters
        self._counter_text = ft.Text("", size=13, color="grey")

        self._items_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Part Name")),
                ft.DataColumn(ft.Text("Qty")),
                ft.DataColumn(ft.Text("Location")),
                ft.DataColumn(ft.Text("Status")),
                ft.DataColumn(ft.Text("Override")),
            ],
            rows=[],
            horizontal_lines=ft.border.BorderSide(1, ft.colors.OUTLINE),
        )

        self.fields["status"] = ft.Text(value="", size=12, color="blue")

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "Location Verification",
                                style=ft.TextThemeStyle.HEADLINE_SMALL,
                            ),
                            ft.Row(
                                controls=[
                                    self._phase_text,
                                    ft.Container(expand=True),
                                    self._location_text,
                                    self._counter_text,
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            # Input row: text field + persistent scan indicator
                            ft.Row(
                                controls=[
                                    self.fields["scan_input"],
                                    ft.Container(
                                        content=self._scan_indicator,
                                        alignment=ft.alignment.center,
                                        width=56,
                                        height=56,
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=8,
                            ),
                            ft.Row(
                                controls=[
                                    self.fields["parse_btn"],
                                    self.fields["clear_input_btn"],
                                    self.fields["clear_all_btn"],
                                    self.fields["reload_locations"],
                                ]
                            ),
                            self.fields["status"],
                            ft.Container(content=self._items_table),
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

    def focus_input(self):
        try:
            self.fields["scan_input"].focus()
            self.fields["scan_input"].update()
        except Exception:
            pass

    def did_mount(self):
        threading.Thread(target=self._load_locations, args=(False,), daemon=True).start()
        return super().did_mount()

    def nav_rail_redirect(self, e):
        super().nav_rail_redirect(e)

    # ------------------------------------------------------------------ #
    #  Scan feedback indicator                                             #
    # ------------------------------------------------------------------ #

    def _set_scan_state(self, state: str, seq: int = -1):
        """Update the scan indicator directly from the calling (worker) thread.

        For _SCAN_CHECKING: skip if a newer scan has already started (seq !=
        self._scan_seq means this CHECKING is stale).
        For final states: always apply — they are always the last thing a
        scan thread does, so they can never be stale relative to themselves.
        """
        with self._scan_state_lock:
            if state == _SCAN_CHECKING and seq != -1 and seq != self._scan_seq:
                return
            self._scan_state = state

        page = self.page
        if not page:
            return

        if state == _SCAN_CHECKING:
            self._scan_ring.visible = True
            self._scan_icon.visible = False
        elif state == _SCAN_VERIFIED:
            self._scan_ring.visible = False
            self._scan_icon.name = ft.icons.CHECK_CIRCLE
            self._scan_icon.color = "green"
            self._scan_icon.visible = True
        elif state == _SCAN_UNEXPECTED:
            self._scan_ring.visible = False
            self._scan_icon.name = ft.icons.CANCEL
            self._scan_icon.color = "red"
            self._scan_icon.visible = True
        elif state == _SCAN_DUPLICATE:
            self._scan_ring.visible = False
            self._scan_icon.name = ft.icons.WARNING_AMBER_ROUNDED
            self._scan_icon.color = "amber"
            self._scan_icon.visible = True
        else:  # idle
            self._scan_ring.visible = False
            self._scan_icon.name = ft.icons.RADIO_BUTTON_UNCHECKED
            self._scan_icon.color = "grey"
            self._scan_icon.visible = True

        try:
            self._scan_ring.update()
            self._scan_icon.update()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Location loading                                                    #
    # ------------------------------------------------------------------ #

    def _load_locations(self, reload: bool = False):
        try:
            if reload:
                inventree_interface.reload_location_cache()
            self.stock_locations = list(inventree_interface.get_cached_location_tree())
            self.stock_location_id_map = inventree_interface.get_cached_location_id_map() or {}
        except Exception as exc:
            cprint(f"[VERIFICATION] Failed to load locations: {exc}", silent=False)

    def _reload_locations(self, _):
        if not self._connect_server_with_retries():
            self.show_dialog(DialogType.ERROR, "Failed to connect to InvenTree server")
            return
        threading.Thread(target=self._load_locations, args=(True,), daemon=True).start()
        self._show_status("Reloading locations...", color="blue")

    def _connect_server_with_retries(self, attempts: int = 3, delay_seconds: float = 1.5) -> bool:
        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        if api_obj and getattr(api_obj, "token", None) and getattr(api_obj, "base_url", None):
            return True
        with self._connect_lock:
            api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
            if api_obj and getattr(api_obj, "token", None) and getattr(api_obj, "base_url", None):
                return True
            for attempt in range(1, attempts + 1):
                if inventree_interface.connect_to_server(force_reconnect=(attempt > 1)):
                    return True
                if attempt < attempts:
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
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.request(method=method.upper(), url=url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                status_code = None
                try:
                    status_code = int(getattr(getattr(exc, "response", None), "status_code", 0))
                except Exception:
                    pass
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    return None
                if attempt < attempts:
                    time.sleep(delay_seconds)
                else:
                    return None
        return None

    # ------------------------------------------------------------------ #
    #  Input handling                                                      #
    # ------------------------------------------------------------------ #

    def _on_clear_input(self, _):
        self.fields["scan_input"].value = ""
        self.fields["scan_input"].update()

    def _on_clear_all(self, _):
        self._phase = self._PHASE_LOCATION
        self._current_location_pk = None
        self._current_location_path = ""
        with self._items_lock:
            self._items.clear()
        self._recent_scan_codes.clear()
        self.fields["scan_input"].value = ""
        self.fields["scan_input"].label = "Scan location barcode / name"
        self.fields[
            "scan_input"
        ].hint_text = "Start by scanning or typing a location. Then scan item barcodes."
        self._phase_text.value = "Phase: Select Location"
        self._phase_text.color = "blue"
        self._location_text.value = ""
        self._counter_text.value = ""
        self._set_scan_state(_SCAN_IDLE)
        self._update_table()
        self._show_status("Reset.", color="grey")
        try:
            self.fields["scan_input"].update()
            self._phase_text.update()
            self._location_text.update()
            self._counter_text.update()
        except Exception:
            pass

    def _on_input_submit(self, _):
        text = (self.fields["scan_input"].value or "").strip()
        if not text:
            return
        self._dispatch(text)
        self.fields["scan_input"].value = ""
        self.fields["scan_input"].update()

    def _on_input_changed(self, _):
        text = self.fields["scan_input"].value or ""
        if "\n" not in text and "\r" not in text:
            return
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        self.fields["scan_input"].value = ""
        self.fields["scan_input"].update()
        for line in lines:
            self._dispatch(line)

    def _on_parse(self, _):
        text = (self.fields["scan_input"].value or "").strip()
        if not text:
            self._show_status("No input to parse.", color="red")
            return
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        self.fields["scan_input"].value = ""
        self.fields["scan_input"].update()
        for line in lines:
            self._dispatch(line)

    def _dispatch(self, raw: str):
        """Route scan depending on current phase."""
        code = raw.strip()
        if not code:
            return

        # Debounce
        now = time.monotonic()
        if self._recent_scan_codes.get(code) and (now - self._recent_scan_codes[code]) < 0.5:
            return
        self._recent_scan_codes[code] = now

        if self._phase == self._PHASE_LOCATION:
            threading.Thread(target=self._resolve_location, args=(code,), daemon=True).start()
        else:
            with self._scan_state_lock:
                self._scan_seq += 1
                seq = self._scan_seq
            self._set_scan_state(_SCAN_CHECKING, seq=seq)
            threading.Thread(target=self._resolve_item_scan, args=(code, seq), daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Phase 1: resolve location                                           #
    # ------------------------------------------------------------------ #

    def _resolve_location(self, raw: str):
        self._show_status("Resolving location...", color="blue")

        if not self._connect_server_with_retries():
            self._show_status("Server offline.", color="red")
            return

        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        token = getattr(api_obj, "token", None) if api_obj else None
        base_url = getattr(api_obj, "base_url", "") if api_obj else ""
        if not token or not base_url:
            self._show_status("No InvenTree auth.", color="red")
            return

        headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

        # Try exact name match first
        loc = self._find_location_by_name(raw, headers, base_url)
        if not loc:
            # Try InvenTree barcode endpoint
            try:
                resp = self._request_with_retries(
                    method="POST",
                    url=f"{base_url.rstrip('/')}/api/barcode/",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"barcode": raw},
                    timeout=20,
                )
                if resp:
                    result = resp.json()
                    stock_loc = result.get("stocklocation") if isinstance(result, dict) else None
                    if stock_loc:
                        loc_pk = stock_loc.get("pk") or stock_loc.get("id")
                        if loc_pk:
                            r2 = self._request_with_retries(
                                method="GET",
                                url=f"{base_url.rstrip('/')}/api/stock/location/{loc_pk}/",
                                headers=headers,
                                timeout=20,
                            )
                            if r2:
                                loc = r2.json()
            except Exception:
                pass

        if not loc:
            # Try path lookup from local cache
            loc_pk = inventree_interface.resolve_stock_location_pk(raw, self.stock_location_id_map)
            if loc_pk and loc_pk > 0:
                r3 = self._request_with_retries(
                    method="GET",
                    url=f"{base_url.rstrip('/')}/api/stock/location/{loc_pk}/",
                    headers=headers,
                    timeout=20,
                )
                if r3:
                    loc = r3.json()

        if not loc:
            self._show_status(f"Location not found: {raw}", color="red")
            return

        loc_pk = int(loc.get("pk") or loc.get("id") or 0)
        loc_path = loc.get("pathstring") or loc.get("name") or raw
        sublocations = int(loc.get("sublocations") or 0)

        if not loc_pk:
            self._show_status("Invalid location response.", color="red")
            return

        self._show_status(f"Loading items for: {loc_path}...", color="blue")
        items = self._fetch_location_stock(loc_pk, headers, base_url, sublocations > 0)

        if not items:
            self._show_status(f"No stock items found at: {loc_path}", color="orange")
        else:
            self._show_status(f"Loaded {len(items)} item(s). Scan to verify.", color="green")

        self._current_location_pk = loc_pk
        self._current_location_path = loc_path

        with self._items_lock:
            self._items = items

        self._phase = self._PHASE_ITEMS
        self._phase_text.value = "Phase: Verify Items"
        self._phase_text.color = "green"
        self._location_text.value = f"Location: {loc_path}"
        self.fields["scan_input"].label = "Scan item barcode"
        self.fields[
            "scan_input"
        ].hint_text = "Scan item barcodes to verify. Unknown items will be added in red."

        self._update_table()
        self._update_counter()

        try:
            self._phase_text.update()
            self._location_text.update()
            self.fields["scan_input"].update()
            self._counter_text.update()
        except Exception:
            pass

    def _find_location_by_name(self, name: str, headers: Dict, base_url: str) -> Optional[Dict]:
        resp = self._request_with_retries(
            method="GET",
            url=f"{base_url.rstrip('/')}/api/stock/location/",
            headers=headers,
            params={"search": name, "limit": 10},
            timeout=20,
        )
        if not resp:
            return None
        payload = resp.json()
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return None
        needle = name.strip().lower()
        for loc in rows:
            if str(loc.get("name") or "").strip().lower() == needle:
                return loc
            if str(loc.get("pathstring") or "").strip().lower() == needle:
                return loc
        return None

    def _fetch_location_stock(
        self,
        location_pk: int,
        headers: Dict,
        base_url: str,
        include_sublocations: bool,
    ) -> List[VerifyItem]:
        items: List[VerifyItem] = []
        url = f"{base_url.rstrip('/')}/api/stock/"
        params: Dict[str, Any] = {
            "location": location_pk,
            "limit": 500,
            "part_detail": True,
            "location_detail": True,
        }
        if include_sublocations:
            params["cascade"] = True

        while url:
            resp = self._request_with_retries(
                method="GET", url=url, headers=headers, params=params, timeout=30
            )
            if not resp:
                break
            payload = resp.json()
            rows = payload.get("results", payload) if isinstance(payload, dict) else payload
            for si in rows if isinstance(rows, list) else []:
                si_pk = int(si.get("pk") or si.get("id") or 0)
                if not si_pk:
                    continue
                part_detail = si.get("part_detail") or {}
                part_pk = int(part_detail.get("pk") or part_detail.get("id") or si.get("part") or 0)
                part_name = (
                    str(part_detail.get("name") or part_detail.get("IPN") or "").strip()
                    or f"Part #{part_pk}"
                )
                qty = str(si.get("quantity") or "0")
                loc_detail = si.get("location_detail") or {}
                loc_name = (
                    loc_detail.get("pathstring")
                    or loc_detail.get("name")
                    or str(si.get("location") or "-")
                )
                items.append(
                    VerifyItem(
                        part_pk=part_pk,
                        part_name=part_name,
                        stock_pk=si_pk,
                        quantity=qty,
                        location=loc_name,
                    )
                )
            url = payload.get("next") if isinstance(payload, dict) else None
            params = {}
        return items

    # ------------------------------------------------------------------ #
    #  Phase 2: verify item scans                                          #
    # ------------------------------------------------------------------ #

    def _resolve_item_scan(self, raw: str, seq: int = -1):
        # Step 0 — parse barcode locally first (no API call)
        parsed = self.parser.parse(raw)
        supplier = parsed.get("supplier", "unknown")
        mpn = str(parsed.get("manufacturer_pn") or "").strip()
        spn = str(parsed.get("supplier_pn") or "").strip()
        barcode_val = str(parsed.get("barcode") or "").strip()
        lookup_value = barcode_val or mpn or spn or raw

        # Fast local match: if the parsed value matches an expected or already-verified
        # item's name/IPN directly we can skip the API entirely.
        fast_result = self._fast_match_mutate(lookup_value, mpn, spn, raw)
        if fast_result is not None:
            fast_status, fast_name = fast_result
            if fast_status.endswith("_dup"):
                self._show_status(f"Already verified: {fast_name}", color="amber")
                self._set_scan_state(_SCAN_DUPLICATE, seq=seq)
            else:
                self._show_status(f"✓ Verified: {fast_name}", color="green")
                self._set_scan_state(_SCAN_VERIFIED, seq=seq)
                self._update_table_throttled(force=True)
                self._update_counter()
            return

        # No fast match — go to server
        if not self._connect_server_with_retries():
            self._show_status("Server offline.", color="red")
            self._set_scan_state(_SCAN_UNEXPECTED, seq=seq)
            return

        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        token = getattr(api_obj, "token", None) if api_obj else None
        base_url = getattr(api_obj, "base_url", "") if api_obj else ""
        if not token or not base_url:
            self._show_status("No InvenTree auth.", color="red")
            self._set_scan_state(_SCAN_UNEXPECTED, seq=seq)
            return

        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        part_pk = 0
        part_name = ""

        # Step 1 — InvenTree barcode endpoint (handles assigned barcodes)
        self._show_status("Checking barcode...", color="blue")
        try:
            resp = self._request_with_retries(
                method="POST",
                url=f"{base_url.rstrip('/')}/api/barcode/",
                headers=headers,
                json={"barcode": raw},
                timeout=20,
            )
            if resp:
                result = resp.json()
                if isinstance(result, dict):
                    part_ref = result.get("part")
                    if isinstance(part_ref, dict):
                        part_pk = int(part_ref.get("pk") or part_ref.get("id") or 0)
                        part_name = str(part_ref.get("name") or part_ref.get("IPN") or "")
                    elif part_ref:
                        part_pk = int(part_ref)

                    if not part_pk:
                        si_ref = result.get("stockitem")
                        if isinstance(si_ref, dict):
                            si_part = si_ref.get("part")
                            if isinstance(si_part, dict):
                                part_pk = int(si_part.get("pk") or si_part.get("id") or 0)
                                part_name = str(si_part.get("name") or si_part.get("IPN") or "")
                            elif si_part:
                                part_pk = int(si_part)
        except Exception:
            pass

        if part_pk and not part_name:
            part_name = self._fetch_part_name(part_pk, headers, base_url)

        # Step 2 — Supplier-part lookup (handles supplier barcodes with known supplier)
        if not part_pk and supplier != "unknown":
            self._show_status("Checking supplier part...", color="blue")
            try:
                supplier_pk = _BarcodeApiHelpers.resolve_supplier_company_pk(self, supplier)
                if supplier_pk > 0:
                    for probe in [v for v in [spn, mpn, lookup_value] if v]:
                        resp2 = self._request_with_retries(
                            method="GET",
                            url=f"{base_url.rstrip('/')}/api/company/part/",
                            headers=headers,
                            params={"supplier": supplier_pk, "search": probe, "limit": 10},
                            timeout=20,
                        )
                        if not resp2:
                            continue
                        payload = resp2.json()
                        rows = (
                            payload.get("results", payload)
                            if isinstance(payload, dict)
                            else payload
                        )
                        for candidate in rows if isinstance(rows, list) else []:
                            part_ref = candidate.get("part") or candidate.get("part_detail")
                            if isinstance(part_ref, dict):
                                candidate_pk = int(part_ref.get("pk") or part_ref.get("id") or 0)
                                candidate_name = str(
                                    part_ref.get("name") or part_ref.get("IPN") or ""
                                )
                            elif part_ref:
                                candidate_pk = int(part_ref)
                                candidate_name = ""
                            else:
                                continue
                            if candidate_pk:
                                part_pk = candidate_pk
                                part_name = candidate_name
                                break
                        if part_pk:
                            break

                    if part_pk and not part_name:
                        part_name = self._fetch_part_name(part_pk, headers, base_url)
            except Exception:
                pass

        # Step 3 — Exact IPN / name search (fallback for plain-text codes)
        if not part_pk:
            self._show_status("Checking part name...", color="blue")
            for probe in [v for v in [lookup_value, mpn, spn, raw] if v]:
                result = _BarcodeApiHelpers.find_part_by_lookup(self, probe)
                if result:
                    part_pk = int(result.get("pk") or result.get("id") or 0)
                    part_name = str(result.get("name") or result.get("IPN") or probe)
                    break

        if part_pk:
            # Fetch where this part actually lives in InvenTree for display
            expected_locs = self._fetch_part_locations(part_pk, headers, base_url)
            self._mark_item_verified(
                part_pk, part_name or f"Part #{part_pk}", raw, seq, expected_locs
            )
        else:
            self._add_unexpected_item(raw, supplier, lookup_value, seq)

    def _fast_match_mutate(self, lookup_value: str, mpn: str, spn: str, raw: str):
        """Try to verify an item purely from the local items list (no API call).

        Returns:
          (STATUS_VERIFIED, part_name)  — matched an expected copy, now marked verified
          (STATUS_DUPLICATE, part_name) — all copies already verified
          None                          — no local match at all; caller must hit API
        Never calls any UI methods — caller handles UI after lock is released.
        """
        probes = list(dict.fromkeys(v.lower() for v in [lookup_value, mpn, spn, raw] if v))
        with self._items_lock:
            # Find any expected copy first
            target = next(
                (
                    it
                    for it in self._items
                    if it.status == VerifyItem.STATUS_EXPECTED
                    and any(it.part_name.lower() == p for p in probes)
                ),
                None,
            )
            if target:
                target.status = VerifyItem.STATUS_VERIFIED
                return (VerifyItem.STATUS_VERIFIED, target.part_name)

            # No expected copy — check if there are already-verified copies
            verified_copy = next(
                (
                    it
                    for it in self._items
                    if it.status == VerifyItem.STATUS_VERIFIED
                    and any(it.part_name.lower() == p for p in probes)
                ),
                None,
            )
            if verified_copy:
                return (VerifyItem.STATUS_VERIFIED + "_dup", verified_copy.part_name)

        return None

    def _fetch_part_name(self, part_pk: int, headers: Dict, base_url: str) -> str:
        try:
            r = self._request_with_retries(
                method="GET",
                url=f"{base_url.rstrip('/')}/api/part/{part_pk}/",
                headers=headers,
                timeout=20,
            )
            if r:
                d = r.json()
                return str(d.get("name") or d.get("IPN") or f"Part #{part_pk}")
        except Exception:
            pass
        return f"Part #{part_pk}"

    def _fetch_part_locations(self, part_pk: int, headers: Dict, base_url: str) -> List[str]:
        """Return a deduplicated list of location pathstrings where part_pk has stock."""
        locs: List[str] = []
        try:
            resp = self._request_with_retries(
                method="GET",
                url=f"{base_url.rstrip('/')}/api/stock/",
                headers=headers,
                params={"part": part_pk, "limit": 100, "location_detail": True},
                timeout=20,
            )
            if resp:
                payload = resp.json()
                rows = payload.get("results", payload) if isinstance(payload, dict) else payload
                seen: set = set()
                for si in rows if isinstance(rows, list) else []:
                    loc_detail = si.get("location_detail") or {}
                    loc_name = (
                        loc_detail.get("pathstring")
                        or loc_detail.get("name")
                        or str(si.get("location") or "")
                    ).strip()
                    if loc_name and loc_name not in seen:
                        seen.add(loc_name)
                        locs.append(loc_name)
        except Exception:
            pass
        return locs

    def _mark_item_verified(
        self,
        part_pk: int,
        part_name: str,
        raw: str,
        seq: int = -1,
        expected_locs: Optional[List[str]] = None,
    ):
        """Find matching expected item and mark it verified, or add as unexpected.

        All list mutations happen under the lock; UI calls happen after release.
        """
        outcome = None  # "verified" | "duplicate" | "unexpected"
        with self._items_lock:
            target = next(
                (
                    it
                    for it in self._items
                    if it.part_pk == part_pk and it.status == VerifyItem.STATUS_EXPECTED
                ),
                None,
            )
            if target:
                target.status = VerifyItem.STATUS_VERIFIED
                outcome = "verified"
            elif part_pk > 0 and any(it.part_pk == part_pk for it in self._items):
                outcome = "duplicate"
            else:
                new_item = VerifyItem(
                    part_pk=part_pk,
                    part_name=part_name,
                    stock_pk=0,
                    quantity="-",
                    location=self._current_location_path,
                )
                new_item.status = VerifyItem.STATUS_UNEXPECTED
                new_item.expected_locations = expected_locs or []
                self._items.append(new_item)
                outcome = "unexpected"

        # UI updates outside the lock
        if outcome == "verified":
            self._show_status(f"✓ Verified: {part_name}", color="green")
            self._set_scan_state(_SCAN_VERIFIED, seq=seq)
            self._update_table_throttled(force=True)
            self._update_counter()
        elif outcome == "duplicate":
            self._show_status(f"Already verified: {part_name}", color="amber")
            self._set_scan_state(_SCAN_DUPLICATE, seq=seq)
        else:
            self._show_status(f"Unexpected item: {part_name}", color="red")
            self._set_scan_state(_SCAN_UNEXPECTED, seq=seq)
            self._update_table_throttled(force=True)
            self._update_counter()

    def _add_unexpected_item(self, raw: str, supplier: str, lookup_value: str, seq: int = -1):
        """Add an unresolved scan as an unexpected (red) item."""
        display_name = lookup_value if lookup_value != raw else raw
        with self._items_lock:
            new_item = VerifyItem(
                part_pk=0,
                part_name=display_name,
                stock_pk=0,
                quantity="-",
                location=self._current_location_path,
            )
            new_item.status = VerifyItem.STATUS_UNEXPECTED
            new_item.expected_locations = []
            self._items.append(new_item)

        self._show_status(f"Unrecognised scan: {display_name}", color="red")
        self._set_scan_state(_SCAN_UNEXPECTED, seq=seq)
        self._update_table_throttled(force=True)
        self._update_counter()

    def _toggle_manual_override(self, item: VerifyItem):
        """Toggle manual verification for an item."""
        with self._items_lock:
            if item.status == VerifyItem.STATUS_VERIFIED and item.manual:
                item.status = VerifyItem.STATUS_EXPECTED
                item.manual = False
            else:
                item.status = VerifyItem.STATUS_VERIFIED
                item.manual = True
        self._update_table()
        self._update_counter()

    # ------------------------------------------------------------------ #
    #  Table rendering                                                     #
    # ------------------------------------------------------------------ #

    _STATUS_COLOR = {
        VerifyItem.STATUS_EXPECTED: None,
        VerifyItem.STATUS_VERIFIED: "green",
        VerifyItem.STATUS_UNEXPECTED: "red",
    }
    _STATUS_LABEL = {
        VerifyItem.STATUS_EXPECTED: "Expected",
        VerifyItem.STATUS_VERIFIED: "Verified ✓",
        VerifyItem.STATUS_UNEXPECTED: "Unexpected",
    }
    _ROW_COLOR = {
        VerifyItem.STATUS_EXPECTED: None,
        VerifyItem.STATUS_VERIFIED: ft.colors.GREEN_50,
        VerifyItem.STATUS_UNEXPECTED: ft.colors.RED_50,
    }

    def _build_table_rows(self) -> list:
        with self._items_lock:
            snapshot = list(self._items)

        # Sort: unexpected first, then expected, then verified
        _order = {
            VerifyItem.STATUS_UNEXPECTED: 0,
            VerifyItem.STATUS_EXPECTED: 1,
            VerifyItem.STATUS_VERIFIED: 2,
        }
        snapshot.sort(key=lambda it: (_order.get(it.status, 9), it.part_name.lower()))

        # Pre-compute per-part_pk copy counts for the hint icon
        pk_total: Counter = Counter(it.part_pk for it in snapshot if it.part_pk > 0)
        pk_verified: Counter = Counter(
            it.part_pk
            for it in snapshot
            if it.part_pk > 0 and it.status == VerifyItem.STATUS_VERIFIED
        )

        rows = []
        for item in snapshot:
            color = self._STATUS_COLOR.get(item.status)
            label = self._STATUS_LABEL.get(item.status, item.status)
            row_color = self._ROW_COLOR.get(item.status)

            # Location cell: for unexpected items show where InvenTree says the part lives
            if item.status == VerifyItem.STATUS_UNEXPECTED:
                if item.expected_locations:
                    loc_str = " / ".join(item.expected_locations)
                    loc_cell = ft.DataCell(
                        ft.Text(
                            self._truncate(loc_str, 35),
                            size=12,
                            color="red",
                            weight=ft.FontWeight.BOLD,
                            italic=True,
                            tooltip=loc_str if len(loc_str) > 35 else None,
                        )
                    )
                else:
                    loc_cell = ft.DataCell(ft.Text("—", size=12, color="grey"))
            else:
                loc_cell = ft.DataCell(
                    ft.Text(self._truncate(item.location, 30), size=12, color="grey")
                )

            override_icon = ft.icons.CHECK_CIRCLE_OUTLINE
            override_tooltip = "Mark as present"
            override_color = "green"
            if item.status == VerifyItem.STATUS_VERIFIED and item.manual:
                override_icon = ft.icons.CANCEL_OUTLINED
                override_tooltip = "Unmark manual override"
                override_color = "orange"

            # Copy hint: show a small green copy icon when this part has multiple
            # stock rows and at least one sibling is already verified.
            show_copy_hint = (
                item.part_pk > 0
                and pk_total[item.part_pk] > 1
                and pk_verified[item.part_pk] > 0
                and item.status != VerifyItem.STATUS_VERIFIED
            )
            copy_count = pk_total[item.part_pk]
            copy_verified = pk_verified[item.part_pk]

            name_controls = [
                ft.Text(
                    self._truncate(item.part_name),
                    size=12,
                    color=color,
                    weight=ft.FontWeight.BOLD
                    if item.status != VerifyItem.STATUS_EXPECTED
                    else ft.FontWeight.NORMAL,
                )
            ]
            if show_copy_hint:
                name_controls.append(
                    ft.Icon(
                        name=ft.icons.COPY_ALL_OUTLINED,
                        size=14,
                        color="green",
                        tooltip=f"{copy_verified}/{copy_count} copies verified",
                    )
                )

            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Row(controls=name_controls, spacing=4, tight=True)),
                        ft.DataCell(ft.Text(item.quantity, size=12)),
                        loc_cell,
                        ft.DataCell(
                            ft.Text(
                                label,
                                size=12,
                                color=color,
                                weight=ft.FontWeight.BOLD,
                            )
                        ),
                        ft.DataCell(
                            ft.IconButton(
                                icon=override_icon,
                                icon_color=override_color,
                                tooltip=override_tooltip,
                                on_click=lambda _, it=item: self._toggle_manual_override(it),
                            )
                        ),
                    ],
                    color=row_color,
                )
            )
        return rows

    def _update_table(self):
        rows = self._build_table_rows()
        page = self.page
        if not page:
            return
        self._items_table.rows = rows
        try:
            page.update()
        except Exception:
            pass

    def _update_table_throttled(self, force: bool = False):
        now = time.monotonic()
        if not force and (now - self._table_last_update_ts) < self._table_min_update_interval_s:
            return
        self._table_last_update_ts = now
        self._update_table()

    def _update_counter(self):
        with self._items_lock:
            total = len(self._items)
            verified = sum(1 for it in self._items if it.status == VerifyItem.STATUS_VERIFIED)
            unexpected = sum(1 for it in self._items if it.status == VerifyItem.STATUS_UNEXPECTED)

        if total == 0:
            self._counter_text.value = ""
            self._counter_text.color = "grey"
        else:
            self._counter_text.value = f"{verified}/{total} verified" + (
                f"  •  {unexpected} unexpected" if unexpected else ""
            )
            if unexpected:
                self._counter_text.color = "red"
            elif verified == total:
                self._counter_text.color = "green"
            else:
                self._counter_text.color = "orange"

        try:
            self._counter_text.update()
        except Exception:
            pass

    @staticmethod
    def _truncate(value: str, max_len: int = 40) -> str:
        text = str(value or "")
        return text if len(text) <= max_len else text[: max_len - 3] + "..."

    # ------------------------------------------------------------------ #
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    def _show_status(self, message: str, color: str = "black"):
        self.fields["status"].value = message
        self.fields["status"].color = color
        try:
            self.fields["status"].update()
        except AssertionError:
            pass
