"""Location management GUI view for InvenTree."""

from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any, Dict, List, Optional

import flet as ft
import requests

from ...common.tools import cprint
from ...database import inventree_interface
from ...search.barcode_parser import BarcodeParser
from .barcode import BarcodeApiMixin
from .barcode import _BarcodeApiHelpers
from .common import DialogType
from .common import DropdownWithSearch
from .common import GUI_PARAMS
from .main import MainView


class LocationScanRow:
    """A scanned location to be re-parented."""

    def __init__(self, row_id: int, raw_code: str, location_path: str, location_pk: int):
        self.row_id = row_id
        self.raw_code = raw_code
        self.location_path = location_path
        self.location_pk = location_pk
        self.status = "Ready"


class StockItemRow:
    """A single stock item to be moved."""

    def __init__(
        self,
        row_id: int,
        raw_code: str,
        stock_pk: int,
        part_name: str,
        supplier: str,
        quantity: str,
        current_location: str,
    ):
        self.row_id = row_id
        self.raw_code = raw_code
        self.stock_pk = stock_pk
        self.part_name = part_name
        self.supplier = supplier
        self.quantity = quantity
        self.current_location = current_location
        self.status = "Ready"


class PendingRow:
    """A scanned code still being resolved."""

    def __init__(self, row_id: int, raw_code: str, supplier: str, lookup_value: str):
        self.row_id = row_id
        self.raw_code = raw_code
        self.supplier = supplier
        self.lookup_value = lookup_value
        self.status = "Resolving..."


class LocationsView(BarcodeApiMixin, MainView):
    """Move stock items and sub-locations to a destination location."""

    title = "Locations"
    fields = {}

    def __init__(self, page: ft.Page):
        self.stock_locations: List[str] = []
        self.stock_location_id_map: Dict[str, int] = {}
        self._connect_lock = threading.Lock()
        self._http = requests.Session()

        self.parser = BarcodeParser()
        self.pending_rows: List[PendingRow] = []
        self.stock_rows: List[StockItemRow] = []
        self.location_rows: List[LocationScanRow] = []
        self._row_counter = 0
        self._rows_lock = threading.Lock()
        self._recent_scan_codes: Dict[str, float] = {}
        self._part_lookup_cache: Dict[str, Optional[Dict]] = {}
        self._part_lookup_lock = threading.Lock()
        self._part_lookup_inflight: Dict[str, threading.Event] = {}
        self._location_name_cache: Dict[int, str] = {}
        self._location_path_to_pk_cache: Dict[str, int] = {}
        self._location_pk_to_path_cache: Dict[int, str] = {}
        self._location_path_map_loaded = False
        self._barcode_endpoint_available: Optional[bool] = None
        self._results_table_last_update_ts = 0.0
        self._results_table_min_update_interval_s = 0.15

        super().__init__(page=page)
        self.build_page()

    # ------------------------------------------------------------------ #
    #  Build UI                                                            #
    # ------------------------------------------------------------------ #

    def build_page(self) -> None:
        self.fields["barcode_input"] = ft.TextField(
            label="Scan barcode / part number / location name",
            multiline=True,
            min_lines=3,
            max_lines=6,
            on_submit=self._on_code_submit,
            on_change=self._on_code_changed,
            hint_text="Scan one code per line. Parts, stock items, or location names accepted.",
        )
        self.fields["parse_codes"] = ft.ElevatedButton(
            text="Parse Codes",
            on_click=self._parse_batch_codes,
        )
        self.fields["clear_input"] = ft.ElevatedButton(
            text="Clear Input",
            on_click=self._on_clear_input,
        )
        self.fields["clear_all_rows"] = ft.IconButton(
            icon=ft.icons.DELETE_SWEEP,
            tooltip="Clear all queued items",
            on_click=self._clear_all_rows,
        )
        self.fields["results_table"] = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Type")),
                ft.DataColumn(ft.Text("Part / Location")),
                ft.DataColumn(ft.Text("Supplier")),
                ft.DataColumn(ft.Text("Qty")),
                ft.DataColumn(ft.Text("Current Location")),
                ft.DataColumn(ft.Text("Status")),
                ft.DataColumn(ft.Text("Remove")),
            ],
            rows=[],
            horizontal_lines=ft.border.BorderSide(1, ft.colors.OUTLINE),
        )
        self.fields["location_select"] = DropdownWithSearch(
            label="Destination Location",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        )
        self.fields["reload_locations"] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip="Reload stock locations from InvenTree",
            on_click=self._reload_locations,
        )
        self.fields["apply_button"] = ft.ElevatedButton(
            text="Move",
            icon=ft.icons.DRIVE_FILE_MOVE,
            on_click=self._on_apply,
            color="white",
            bgcolor="green",
        )
        self.fields["progress"] = ft.ProgressBar(value=0, visible=False, height=8)
        self.fields["progress_message"] = ft.Text(value="", size=11, color="blue")
        self.fields["status"] = ft.Text(value="", size=12, color="blue")

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "Move Items to Location",
                                style=ft.TextThemeStyle.HEADLINE_SMALL,
                            ),
                            self.fields["barcode_input"],
                            ft.Row(
                                [
                                    self.fields["parse_codes"],
                                    self.fields["clear_input"],
                                    self.fields["clear_all_rows"],
                                ]
                            ),
                            ft.Container(content=self.fields["results_table"]),
                            ft.Row(
                                [
                                    self.fields["location_select"],
                                    self.fields["reload_locations"],
                                ]
                            ),
                            ft.Row([self.fields["apply_button"]]),
                            self.fields["progress"],
                            self.fields["progress_message"],
                            self.fields["status"],
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
            self.fields["barcode_input"].focus()
            self.fields["barcode_input"].update()
        except Exception:
            pass

    def did_mount(self):
        threading.Thread(target=self._load_locations, args=(False,), daemon=True).start()
        return super().did_mount()

    def nav_rail_redirect(self, e):
        field = self.fields.get("location_select")
        if field:
            field.reset_search_state()
        super().nav_rail_redirect(e)

    def will_unmount(self):
        field = self.fields.get("location_select")
        if field:
            field.reset_search_state()
        return super().will_unmount()

    # ------------------------------------------------------------------ #
    #  Location loading                                                    #
    # ------------------------------------------------------------------ #

    def _load_locations(self, reload: bool = False):
        try:
            if reload:
                inventree_interface.reload_location_cache()
            location_list = inventree_interface.get_cached_location_tree()
            self.stock_locations = list(location_list)
            self.stock_location_id_map = inventree_interface.get_cached_location_id_map() or {}

            loc_opts = [ft.dropdown.Option(loc) for loc in self.stock_locations]

            self._location_path_map_loaded = False
            self._location_name_cache.clear()
            self._location_path_to_pk_cache.clear()
            self._location_pk_to_path_cache.clear()
            self._location_path_to_pk_cache = dict(self.stock_location_id_map)

            def _apply():
                if not self.page:
                    return
                self.fields["location_select"].options = loc_opts
                self.fields["location_select"].set_enabled(True)
                self.fields["location_select"].reset_search_state()
                self.page.update()

            page = self.page
            if page:
                page.run_thread(_apply)

        except Exception as exc:
            cprint(f"[LOCATIONS] Failed to load stock locations: {exc}", silent=False)

    def _reload_locations(self, _):
        if not self._connect_server_with_retries():
            self.show_dialog(DialogType.ERROR, "Failed to connect to InvenTree server")
            return
        threading.Thread(target=self._load_locations, args=(True,), daemon=True).start()
        self._show_status("Reloading...", color="blue")

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
    #  Status                                                              #
    # ------------------------------------------------------------------ #

    def _show_status(self, message: str, color: str = "black"):
        self.fields["status"].value = message
        self.fields["status"].color = color
        try:
            self.fields["status"].update()
        except AssertionError:
            pass

    # ------------------------------------------------------------------ #
    #  Input handling                                                      #
    # ------------------------------------------------------------------ #

    def _on_clear_input(self, _):
        self.fields["barcode_input"].value = ""
        self.fields["barcode_input"].update()

    def _on_code_submit(self, _):
        text = (self.fields["barcode_input"].value or "").strip()
        if not text:
            return
        if self._enqueue_code(text):
            self.fields["barcode_input"].value = ""
            self.fields["barcode_input"].update()

    def _on_code_changed(self, _):
        text = self.fields["barcode_input"].value or ""
        if "\n" not in text and "\r" not in text:
            return
        lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
        success = 0
        for line in lines:
            if self._enqueue_code(line, update_table=False):
                success += 1
        self._update_results_table()
        self.fields["barcode_input"].value = ""
        self.fields["barcode_input"].update()
        self._show_status(f"Queued {success} code(s) for resolution", color="blue")

    def _parse_batch_codes(self, _):
        text = (self.fields["barcode_input"].value or "").strip()
        if not text:
            self._show_status("No input provided", color="red")
            return
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        success = 0
        for line in lines:
            if self._enqueue_code(line, update_table=False):
                success += 1
        self._update_results_table()
        self.fields["barcode_input"].value = ""
        self.fields["barcode_input"].update()
        self._show_status(f"Queued {success} code(s) for resolution", color="blue")

    def _enqueue_code(self, raw_code: str, update_table: bool = True) -> bool:
        code = str(raw_code or "").strip()
        if not code:
            return False
        now = time.monotonic()
        if self._recent_scan_codes.get(code) and (now - self._recent_scan_codes[code]) < 0.5:
            return False
        self._recent_scan_codes[code] = now

        parsed = self.parser.parse(code)
        supplier = parsed.get("supplier", "unknown")
        lookup_value = (
            parsed.get("barcode", "")
            or parsed.get("manufacturer_pn", "")
            or parsed.get("supplier_pn", "")
            or code
        )

        with self._rows_lock:
            self._row_counter += 1
            row = PendingRow(
                row_id=self._row_counter,
                raw_code=code,
                supplier=supplier,
                lookup_value=lookup_value,
            )
            self.pending_rows.append(row)

        if update_table:
            self._update_results_table()

        threading.Thread(target=self._resolve_code_async, args=(row.row_id,), daemon=True).start()
        return True

    # ------------------------------------------------------------------ #
    #  Async resolution                                                    #
    # ------------------------------------------------------------------ #

    def _resolve_code_async(self, row_id: int):
        with self._rows_lock:
            row = next((r for r in self.pending_rows if r.row_id == row_id), None)
        if not row:
            return

        try:
            row.status = "Connecting..."
            self._update_results_table_throttled()

            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.0):
                row.status = "Server offline"
                self._update_results_table_throttled(force=True)
                return

            api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
            token = getattr(api_obj, "token", None) if api_obj else None
            base_url = getattr(api_obj, "base_url", "") if api_obj else ""
            if not token or not base_url:
                row.status = "No auth"
                self._update_results_table_throttled(force=True)
                return

            headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

            # 1. Try location by name
            row.status = "Checking location..."
            self._update_results_table_throttled()
            loc = self._find_location_by_name(row.lookup_value, headers, base_url)
            if loc:
                loc_pk = int(loc.get("pk") or loc.get("id"))
                loc_path = loc.get("pathstring") or loc.get("name") or row.lookup_value
                with self._rows_lock:
                    self.pending_rows = [r for r in self.pending_rows if r.row_id != row_id]
                    self._row_counter += 1
                    loc_row = LocationScanRow(
                        row_id=row_id,
                        raw_code=row.raw_code,
                        location_path=loc_path,
                        location_pk=loc_pk,
                    )
                    self.location_rows.append(loc_row)
                self._update_results_table_throttled(force=True)
                return

            # 2. Try part — use supplier info if available
            row.status = "Checking part..."
            self._update_results_table_throttled()

            part = None
            if row.supplier != "unknown":
                sp = (
                    _BarcodeApiHelpers.find_supplier_part_for_row_static(
                        self, [row.lookup_value], row.supplier
                    )
                    if hasattr(_BarcodeApiHelpers, "find_supplier_part_for_row_static")
                    else None
                )
                if sp is None:
                    # fall back via instance method
                    try:
                        sp = self._find_supplier_part_for_row([row.lookup_value], row.supplier)
                    except Exception:
                        sp = None
                if sp:
                    part_ref = sp.get("part") or sp.get("part_detail")
                    part_pk = int(
                        part_ref.get("pk") or part_ref.get("id")
                        if isinstance(part_ref, dict)
                        else part_ref or 0
                    )
                    if part_pk:
                        resp = self._request_with_retries(
                            method="GET",
                            url=f"{base_url.rstrip('/')}/api/part/{part_pk}/",
                            headers=headers,
                            timeout=20,
                        )
                        if resp:
                            part = resp.json()

            if part is None:
                part = _BarcodeApiHelpers.find_part_by_lookup(self, row.lookup_value)

            if not part:
                row.status = "Not found"
                self._update_results_table_throttled(force=True)
                return

            part_pk = int(part.get("pk") or part.get("id"))
            part_name = str(part.get("name") or part.get("IPN") or row.lookup_value)

            # 3. Fetch all stock items for this part
            row.status = "Fetching stock..."
            self._update_results_table_throttled()

            stock_items = self._fetch_stock_items(part_pk, headers, base_url)

            if not stock_items:
                row.status = "No stock"
                self._update_results_table_throttled(force=True)
                return

            # Build reverse map pk→path from local cache (may be empty if not yet loaded)
            pk_to_path = {v: k for k, v in self.stock_location_id_map.items()}

            # Build one StockItemRow per stock item
            new_stock_rows = []
            for si in stock_items:
                si_pk = int(si.get("pk") or si.get("id") or 0)
                if not si_pk:
                    continue
                qty = str(si.get("quantity") or "0")
                loc_detail = si.get("location_detail") or {}
                raw_loc = si.get("location")
                try:
                    raw_loc_int = int(raw_loc) if raw_loc is not None else -1
                except Exception:
                    raw_loc_int = -1
                loc_name = (
                    loc_detail.get("pathstring")
                    or loc_detail.get("name")
                    or pk_to_path.get(raw_loc_int)
                    or str(raw_loc or "-")
                )
                with self._rows_lock:
                    self._row_counter += 1
                    new_stock_rows.append(
                        StockItemRow(
                            row_id=self._row_counter,
                            raw_code=row.raw_code,
                            stock_pk=si_pk,
                            part_name=part_name,
                            supplier=row.supplier if row.supplier != "unknown" else "",
                            quantity=qty,
                            current_location=loc_name,
                        )
                    )

            with self._rows_lock:
                self.pending_rows = [r for r in self.pending_rows if r.row_id != row_id]
                self.stock_rows.extend(new_stock_rows)

            self._update_results_table_throttled(force=True)

        except Exception as exc:
            import traceback

            cprint(
                f"[LOCATIONS] Error resolving row {row_id}: {traceback.format_exc()}", silent=False
            )
            row.status = f"Error: {str(exc)[:40]}"
            self._update_results_table_throttled(force=True)

    def _find_supplier_part_for_row(
        self, lookup_values: List[str], supplier_key: str
    ) -> Optional[Dict]:
        """Delegate to BarcodeApiMixin via instance (needs self._request_with_retries etc.)"""
        # _BarcodeApiHelpers methods are unbound — call via the mixin on self
        supplier_norm = str(supplier_key or "").strip().lower()
        # supplier_aliases = _BarcodeApiHelpers.PO_SUPPLIER_NAME_MAP.get(supplier_norm, [supplier_key])
        supplier_pk = _BarcodeApiHelpers.resolve_supplier_company_pk(self, supplier_norm)
        if supplier_pk <= 0:
            return None

        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        token = getattr(api_obj, "token", None) if api_obj else None
        base_url = getattr(api_obj, "base_url", "") if api_obj else ""
        if not token or not base_url:
            return None

        headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
        for probe in lookup_values:
            resp = self._request_with_retries(
                method="GET",
                url=f"{base_url.rstrip('/')}/api/company/part/",
                headers=headers,
                params={"supplier": int(supplier_pk), "search": probe, "limit": 10},
                timeout=20,
            )
            if not resp:
                continue
            payload = resp.json()
            rows = payload.get("results", payload) if isinstance(payload, dict) else payload
            for candidate in rows if isinstance(rows, list) else []:
                if int(candidate.get("pk") or candidate.get("id") or 0) > 0:
                    return candidate
        return None

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
        return None

    def _fetch_stock_items(self, part_pk: int, headers: Dict, base_url: str) -> List[Dict]:
        items = []
        url = f"{base_url.rstrip('/')}/api/stock/"
        params: Dict[str, Any] = {"part": part_pk, "limit": 250, "location_detail": True}
        while url:
            resp = self._request_with_retries(
                method="GET", url=url, headers=headers, params=params, timeout=20
            )
            if not resp:
                break
            payload = resp.json()
            rows = payload.get("results", payload) if isinstance(payload, dict) else payload
            items.extend(rows if isinstance(rows, list) else [])
            url = payload.get("next") if isinstance(payload, dict) else None
            params = {}
        return items

    def _find_part_by_lookup(self, lookup_value: str) -> Optional[Dict]:
        return _BarcodeApiHelpers.find_part_by_lookup(self, lookup_value)

    def _resolve_location_string(self, part: Dict) -> str:
        return _BarcodeApiHelpers.resolve_location_string(part)

    def _remove_row(self, row_id: int):
        with self._rows_lock:
            self.pending_rows = [r for r in self.pending_rows if r.row_id != row_id]
            self.stock_rows = [r for r in self.stock_rows if r.row_id != row_id]
            self.location_rows = [r for r in self.location_rows if r.row_id != row_id]
        self._update_results_table()

    def _clear_all_rows(self, _):
        with self._rows_lock:
            if not self.pending_rows and not self.stock_rows and not self.location_rows:
                return
            self.pending_rows.clear()
            self.stock_rows.clear()
            self.location_rows.clear()
            self._recent_scan_codes.clear()
        self._update_results_table()
        self._show_status("Cleared all queued items", color="blue")

    # ------------------------------------------------------------------ #
    #  Table rendering                                                     #
    # ------------------------------------------------------------------ #

    def _build_table_rows(self) -> list:
        with self._rows_lock:
            pending_snap = list(self.pending_rows)
            stock_snap = list(self.stock_rows)
            loc_snap = list(self.location_rows)

        table_rows = []

        for row in pending_snap:
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text("...", size=12, color="grey")),
                        ft.DataCell(ft.Text(self._truncate(row.lookup_value), size=12)),
                        ft.DataCell(
                            ft.Text(
                                row.supplier.upper() if row.supplier != "unknown" else "-", size=12
                            )
                        ),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text(row.status, size=12, color="blue")),
                        ft.DataCell(
                            ft.IconButton(
                                icon=ft.icons.DELETE,
                                on_click=lambda _, rid=row.row_id: self._remove_row(rid),
                            )
                        ),
                    ]
                )
            )

        for row in loc_snap:
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text("Location", size=12, color="purple", weight=ft.FontWeight.BOLD)
                        ),
                        ft.DataCell(ft.Text(self._truncate(row.location_path), size=12)),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text("-", size=12)),
                        ft.DataCell(ft.Text("Ready", size=12, color="green")),
                        ft.DataCell(
                            ft.IconButton(
                                icon=ft.icons.DELETE,
                                on_click=lambda _, rid=row.row_id: self._remove_row(rid),
                            )
                        ),
                    ]
                )
            )

        for row in stock_snap:
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text("Stock", size=12, color="blue")),
                        ft.DataCell(ft.Text(self._truncate(row.part_name), size=12)),
                        ft.DataCell(
                            ft.Text(row.supplier.upper() if row.supplier else "-", size=12)
                        ),
                        ft.DataCell(ft.Text(row.quantity, size=12)),
                        ft.DataCell(ft.Text(self._truncate(row.current_location), size=12)),
                        ft.DataCell(ft.Text(row.status, size=12, color="green")),
                        ft.DataCell(
                            ft.IconButton(
                                icon=ft.icons.DELETE,
                                on_click=lambda _, rid=row.row_id: self._remove_row(rid),
                            )
                        ),
                    ]
                )
            )

        return table_rows

    def _update_results_table(self):
        table_rows = self._build_table_rows()

        def _apply():
            if not self.page:
                return
            self.fields["results_table"].rows = table_rows
            self.page.update()

        page = self.page
        if page:
            page.run_thread(_apply)

    def _update_results_table_throttled(self, force: bool = False):
        now = time.monotonic()
        if (
            not force
            and (now - self._results_table_last_update_ts)
            < self._results_table_min_update_interval_s
        ):
            return
        self._results_table_last_update_ts = now
        self._update_results_table()

    @staticmethod
    def _truncate(value: str, max_len: int = 35) -> str:
        text = str(value or "")
        return text if len(text) <= max_len else text[: max_len - 3] + "..."

    def _get_location_pk(self, location_value: str) -> int:
        return inventree_interface.resolve_stock_location_pk(
            location_value, self.stock_location_id_map
        )

    # ------------------------------------------------------------------ #
    #  Apply / Move                                                        #
    # ------------------------------------------------------------------ #

    def _on_apply(self, _):
        with self._rows_lock:
            valid_stock = list(self.stock_rows)
            valid_locs = list(self.location_rows)

        if not valid_stock and not valid_locs:
            self.show_dialog(DialogType.ERROR, "No resolved items to move")
            return

        location_value = str(self.fields["location_select"].value or "").strip()
        if not location_value:
            self.show_dialog(DialogType.ERROR, "Select a destination location")
            return

        if not self._connect_server_with_retries():
            self.show_dialog(DialogType.ERROR, "Failed to connect to InvenTree server")
            return

        location_pk = self._get_location_pk(location_value)
        if location_pk <= 0:
            self.show_dialog(DialogType.ERROR, f"Could not resolve location: {location_value}")
            return

        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        token = getattr(api_obj, "token", None) if api_obj else None
        base_url = getattr(api_obj, "base_url", "") if api_obj else ""
        if not token or not base_url:
            self.show_dialog(DialogType.ERROR, "InvenTree auth context unavailable")
            return

        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        total = len(valid_stock) + len(valid_locs)
        self.fields["progress"].visible = True
        self.fields["progress"].value = 0
        self.fields["progress_message"].value = f"Processing 0/{total}"
        self.fields["progress_message"].color = "blue"
        self._page.update()

        success = 0
        failed = 0
        failures: List[str] = []
        successful_stock_ids: List[int] = []
        successful_loc_ids: List[int] = []

        def _move_stock(row: StockItemRow) -> Dict[str, Any]:
            try:
                resp = self._request_with_retries(
                    method="POST",
                    url=f"{base_url.rstrip('/')}/api/stock/transfer/",
                    headers=headers,
                    json={
                        "items": [{"pk": row.stock_pk, "quantity": row.quantity}],
                        "location": location_pk,
                        "notes": "Ki-nTree location move",
                    },
                    timeout=30,
                )
                if resp and resp.status_code in [200, 201, 202]:
                    return {"row": row, "ok": True, "errors": [], "type": "stock"}
                # fallback PATCH
                resp2 = self._request_with_retries(
                    method="PATCH",
                    url=f"{base_url.rstrip('/')}/api/stock/{row.stock_pk}/",
                    headers=headers,
                    json={"location": location_pk},
                    timeout=20,
                )
                ok = resp2 is not None and resp2.status_code in [200, 202]
                return {
                    "row": row,
                    "ok": ok,
                    "errors": []
                    if ok
                    else [f"{row.part_name} (stock {row.stock_pk}): move failed"],
                    "type": "stock",
                }
            except Exception as exc:
                return {
                    "row": row,
                    "ok": False,
                    "errors": [f"{row.part_name}: {str(exc)[:60]}"],
                    "type": "stock",
                }

        def _move_location(row: LocationScanRow) -> Dict[str, Any]:
            try:
                resp = self._request_with_retries(
                    method="PATCH",
                    url=f"{base_url.rstrip('/')}/api/stock/location/{row.location_pk}/",
                    headers=headers,
                    json={"parent": location_pk},
                    timeout=20,
                )
                ok = resp is not None and resp.status_code in [200, 202]
                return {
                    "row": row,
                    "ok": ok,
                    "errors": [] if ok else [f"{row.location_path}: move failed"],
                    "type": "location",
                }
            except Exception as exc:
                return {
                    "row": row,
                    "ok": False,
                    "errors": [f"{row.location_path}: {str(exc)[:60]}"],
                    "type": "location",
                }

        completed = 0
        with ThreadPoolExecutor(max_workers=min(4, max(1, total))) as executor:
            futures = {}
            for row in valid_stock:
                futures[executor.submit(_move_stock, row)] = row
            for row in valid_locs:
                futures[executor.submit(_move_location, row)] = row

            for future in as_completed(futures):
                result = future.result()
                completed += 1
                if result["ok"]:
                    success += 1
                    if result["type"] == "stock":
                        successful_stock_ids.append(result["row"].row_id)
                    else:
                        successful_loc_ids.append(result["row"].row_id)
                else:
                    failed += 1
                    failures.extend(result["errors"])
                self.fields["progress"].value = completed / total if total else 1.0
                self.fields["progress_message"].value = f"Processing {completed}/{total}"
                self._page.update()

        self.fields["progress"].value = 1.0
        self.fields["progress"].color = (
            "green" if failed == 0 else ("amber" if success > 0 else "red")
        )
        self.fields["progress_message"].value = f"Done: {success} success, {failed} failed"
        self.fields["progress_message"].color = "green" if failed == 0 else "orange"
        self._page.update()

        if failures:
            detail = "\n".join(f"- {e}" for e in failures[:5])
            if len(failures) > 5:
                detail += f"\n- ... and {len(failures) - 5} more"
            self.show_dialog(DialogType.WARNING, f"Finished with issues:\n{detail}")
        else:
            self.show_dialog(DialogType.VALID, f"Moved {success} item(s) successfully")

        if successful_stock_ids or successful_loc_ids:
            with self._rows_lock:
                self.stock_rows = [
                    r for r in self.stock_rows if r.row_id not in successful_stock_ids
                ]
                self.location_rows = [
                    r for r in self.location_rows if r.row_id not in successful_loc_ids
                ]
            self._update_results_table()
            if successful_loc_ids:
                threading.Thread(target=self._load_locations, args=(True,), daemon=True).start()

        self._show_status(
            f"Done: {success} success, {failed} failed",
            color="green" if failed == 0 else "orange",
        )
