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
from .barcode import ExistingPartScanRow
from .common import DialogType
from .common import DropdownWithSearch
from .common import GUI_PARAMS
from .main import MainView


class LocationsView(BarcodeApiMixin, MainView):
    """Manage stock location hierarchy and move items to locations."""

    title = "Locations"
    fields = {}

    _ROOT_LABEL = "(root)"

    def __init__(self, page: ft.Page):
        self.stock_locations: List[str] = []
        self.stock_location_id_map: Dict[str, int] = {}
        self._connect_lock = threading.Lock()
        self._http = requests.Session()

        # Scan/resolve state (section 2)
        self.parser = BarcodeParser()
        self.scanned_rows: List[ExistingPartScanRow] = []
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
        # --- Section 1: Reassign locations ---
        self.fields["source_location"] = DropdownWithSearch(
            label="Location To Move",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        )
        self.fields["target_parent"] = DropdownWithSearch(
            label="New Parent Location",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        )
        self.fields["reload_locations_s1"] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip="Reload stock locations from InvenTree",
            on_click=self._reload_locations_s1,
        )
        self.fields["clear_selection"] = ft.IconButton(
            icon=ft.icons.CLEAR,
            tooltip="Clear selected source/parent",
            on_click=self._clear_selection,
        )
        self.fields["move_button"] = ft.ElevatedButton(
            text="Move",
            on_click=self._on_move,
            color="white",
            bgcolor="green",
            width=120,
        )
        self.fields["status_s1"] = ft.Text(value="", size=12, color="blue")

        # --- Section 2: Move items to a location ---
        self.fields["barcode_input"] = ft.TextField(
            label="Scan barcode / part number",
            multiline=True,
            min_lines=3,
            max_lines=6,
            on_submit=self._on_code_submit,
            on_change=self._on_code_changed,
            hint_text="Scan one code per line. Part, supplier part, or stock item barcodes accepted.",
        )
        self.fields["parse_codes"] = ft.ElevatedButton(
            text="Parse Codes",
            on_click=self._parse_batch_codes,
        )
        self.fields["clear_input"] = ft.ElevatedButton(
            text="Clear Input",
            on_click=lambda _: (
                setattr(self.fields["barcode_input"], "value", "")
                or self.fields["barcode_input"].update()
            ),
        )
        self.fields["clear_all_rows"] = ft.IconButton(
            icon=ft.icons.DELETE_SWEEP,
            tooltip="Clear all queued items",
            on_click=self._clear_all_rows,
        )
        self.fields["results_table"] = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Input Code")),
                ft.DataColumn(ft.Text("Supplier")),
                ft.DataColumn(ft.Text("Lookup")),
                ft.DataColumn(ft.Text("Status")),
                ft.DataColumn(ft.Text("Part")),
                ft.DataColumn(ft.Text("Current Location")),
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
        self.fields["reload_locations_s2"] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip="Reload stock locations from InvenTree",
            on_click=self._reload_locations_s2,
        )
        self.fields["apply_button"] = ft.ElevatedButton(
            text="Apply",
            on_click=self._on_apply,
            color="white",
            bgcolor="green",
            width=120,
        )
        self.fields["progress"] = ft.ProgressBar(value=0, visible=False, height=8)
        self.fields["progress_message"] = ft.Text(value="", size=11, color="blue")
        self.fields["status_s2"] = ft.Text(value="", size=12, color="blue")

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            # Section 1
                            ft.Text(
                                "Reassign Locations",
                                style=ft.TextThemeStyle.HEADLINE_SMALL,
                            ),
                            ft.Row(
                                [
                                    self.fields["source_location"],
                                    self.fields["reload_locations_s1"],
                                ]
                            ),
                            ft.Row(
                                [
                                    self.fields["target_parent"],
                                    self.fields["clear_selection"],
                                ]
                            ),
                            ft.Row([self.fields["move_button"]]),
                            self.fields["status_s1"],
                            ft.Divider(height=24),
                            # Section 2
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
                            ft.Container(content=self.fields["results_table"], expand=True),
                            ft.Row(
                                [
                                    self.fields["location_select"],
                                    self.fields["reload_locations_s2"],
                                ]
                            ),
                            ft.Row([self.fields["apply_button"]]),
                            self.fields["progress"],
                            self.fields["progress_message"],
                            self.fields["status_s2"],
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
            import logging

            logging.exception("Exception focusing/updating barcode_input field:")

    def did_mount(self):
        self._load_locations(reload=False)
        self.focus_input()
        return super().did_mount()

    # ------------------------------------------------------------------ #
    #  Shared: location loading                                            #
    # ------------------------------------------------------------------ #

    def _load_locations(self, reload: bool = False):
        try:
            location_list = inventree_interface.build_stock_location_tree(reload=reload)
            self.stock_locations = list(location_list)
            self.stock_location_id_map = inventree_interface.get_stock_location_id_map() or {}

            source_options = [ft.dropdown.Option(loc) for loc in self.stock_locations]
            target_options = [ft.dropdown.Option(self._ROOT_LABEL)] + source_options

            self.fields["source_location"].options = source_options
            self.fields["target_parent"].options = target_options
            self.fields["source_location"].disabled = False
            self.fields["target_parent"].disabled = False
            self.fields["source_location"].done_search()
            self.fields["target_parent"].done_search()

            loc_options = [ft.dropdown.Option(loc) for loc in self.stock_locations]
            self.fields["location_select"].options = loc_options
            self.fields["location_select"].disabled = False
            self.fields["location_select"].done_search()

            # Rebuild path caches for section 2 item resolution
            self._location_path_map_loaded = False
            self._location_name_cache.clear()
            self._location_path_to_pk_cache.clear()
            self._location_pk_to_path_cache.clear()
            self._location_path_to_pk_cache = dict(self.stock_location_id_map)

            self._page.update()
        except Exception as exc:
            cprint(f"[LOCATIONS] Failed to load stock locations: {exc}", silent=False)
            self._show_status_s1("Failed to load stock locations", color="red")

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
                    import logging

                    logging.exception("Exception extracting status_code from response:")
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    return None
                if attempt < attempts:
                    time.sleep(delay_seconds)
                else:
                    return None
        return None

    # ------------------------------------------------------------------ #
    #  Section 1: Reassign locations                                       #
    # ------------------------------------------------------------------ #

    def _show_status_s1(self, message: str, color: str = "black"):
        self.fields["status_s1"].value = message
        self.fields["status_s1"].color = color
        try:
            self.fields["status_s1"].update()
        except AssertionError:
            pass

    def _reload_locations_s1(self, _):
        if not self._connect_server_with_retries():
            self.show_dialog(DialogType.ERROR, "Failed to connect to InvenTree server")
            return
        self._load_locations(reload=True)
        self._show_status_s1("Stock locations reloaded", color="green")

    def _clear_selection(self, _):
        self.fields["source_location"].value = None
        self.fields["target_parent"].value = None
        try:
            self.fields["source_location"].update()
            self.fields["target_parent"].update()
        except AssertionError:
            pass
        self._show_status_s1("Selection cleared", color="blue")

    def _on_move(self, _):
        source = str(self.fields["source_location"].value or "").strip()
        target = str(self.fields["target_parent"].value or "").strip()

        if not source:
            self.show_dialog(DialogType.ERROR, "Select source location")
            return
        if not target:
            self.show_dialog(DialogType.ERROR, "Select target parent location")
            return
        if target != self._ROOT_LABEL and source == target:
            self.show_dialog(DialogType.ERROR, "Source and target parent cannot be the same")
            return
        if target != self._ROOT_LABEL and target.startswith(f"{source}/"):
            self.show_dialog(DialogType.ERROR, "Cannot move a location into its own child")
            return

        if not self._connect_server_with_retries():
            self.show_dialog(
                DialogType.ERROR, "Failed to connect to InvenTree server after retries"
            )
            return

        source_pk = inventree_interface.resolve_stock_location_pk(
            source, self.stock_location_id_map
        )
        if source_pk <= 0:
            self.show_dialog(DialogType.ERROR, f"Source location not found: {source}")
            return

        if target == self._ROOT_LABEL:
            parent_pk = None
        else:
            parent_pk = inventree_interface.resolve_stock_location_pk(
                target, self.stock_location_id_map
            )
            if parent_pk <= 0:
                self.show_dialog(DialogType.ERROR, f"Target parent not found: {target}")
                return

        api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
        token = getattr(api_obj, "token", None) if api_obj else None
        base_url = getattr(api_obj, "base_url", "") if api_obj else ""
        if not token or not base_url:
            self.show_dialog(DialogType.ERROR, "InvenTree auth context unavailable")
            return

        endpoint = f"{base_url.rstrip('/')}/api/stock/location/{int(source_pk)}/"
        headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = self._http.patch(
                endpoint, headers=headers, json={"parent": parent_pk}, timeout=20
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            body = ""
            try:
                body = exc.response.text[:300] if exc.response is not None else ""
            except Exception:
                import logging

                logging.exception("Exception extracting error body from response:")
            self.show_dialog(DialogType.ERROR, f"Failed to move location: {exc}\n{body}")
            return

        self._show_status_s1("Location moved successfully", color="green")
        self._load_locations(reload=True)

    # ------------------------------------------------------------------ #
    #  Section 2: Move items to location                                   #
    # ------------------------------------------------------------------ #

    def _show_status_s2(self, message: str, color: str = "black"):
        self.fields["status_s2"].value = message
        self.fields["status_s2"].color = color
        try:
            self.fields["status_s2"].update()
        except AssertionError:
            pass

    def _reload_locations_s2(self, _):
        if not self._connect_server_with_retries():
            self.show_dialog(DialogType.ERROR, "Failed to connect to InvenTree server")
            return
        self._load_locations(reload=True)
        self._show_status_s2("Stock locations reloaded", color="green")

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
        self._show_status_s2(f"Queued {success} item(s) for validation", color="blue")

    def _parse_batch_codes(self, _):
        text = (self.fields["barcode_input"].value or "").strip()
        if not text:
            self._show_status_s2("No input provided", color="red")
            return
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        success = 0
        for line in lines:
            if self._enqueue_code(line, update_table=False):
                success += 1
        self._update_results_table()
        self.fields["barcode_input"].value = ""
        self.fields["barcode_input"].update()
        self._show_status_s2(f"Queued {success} item(s) for validation", color="blue")

    def _enqueue_code(self, raw_code: str, update_table: bool = True) -> bool:
        code = str(raw_code or "").strip()
        if not code:
            return False
        now = time.monotonic()
        recent_ts = self._recent_scan_codes.get(code)
        if recent_ts is not None and (now - recent_ts) < 0.5:
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
        with self._rows_lock:
            row = next((r for r in self.scanned_rows if r.row_id == row_id), None)
        if not row:
            return

        try:
            row.status = "Checking server..."
            self._update_results_table_throttled()

            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.0):
                row.status = "Server offline"
                self._update_results_table_throttled(force=True)
                return

            row.status = "Checking part..."
            self._update_results_table_throttled()

            part = self._find_part_by_lookup(row.lookup_value)
            if not part:
                row.status = "Part not found"
                self._update_results_table_throttled(force=True)
                return

            row.part_pk = int(part.get("pk") or part.get("id"))
            row.part_name = str(part.get("name") or part.get("IPN") or row.lookup_value)
            try:
                row.default_location_pk = int(part.get("default_location") or 0)
            except Exception:
                row.default_location_pk = 0

            row.location = self._resolve_location_string(part)

            has_location = bool(row.location and row.location not in ("-", "None", "none"))
            row.status = "Ready" if has_location else "Missing location"
        except Exception as exc:
            row.status = f"Error: {str(exc)[:40]}"

        self._update_results_table_throttled(force=True)

    def _remove_row(self, row_id: int):
        with self._rows_lock:
            self.scanned_rows = [r for r in self.scanned_rows if r.row_id != row_id]
        self._update_results_table()

    def _clear_all_rows(self, _):
        with self._rows_lock:
            if not self.scanned_rows:
                return
            self.scanned_rows.clear()
            self._recent_scan_codes.clear()
        self._update_results_table()
        self._show_status_s2("Cleared all queued items", color="blue")

    def _update_results_table(self):
        with self._rows_lock:
            rows_snapshot = list(self.scanned_rows)

        table_rows = []
        for row in rows_snapshot:
            if row.status.startswith("Ready") or row.status.startswith("Missing location"):
                status_color = "green" if row.status == "Ready" else "orange"
            elif "not found" in row.status.lower() or "error" in row.status.lower():
                status_color = "red"
            else:
                status_color = "blue"

            part_text = f"{row.part_name} ({row.part_pk})" if row.part_pk else "-"
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                self._truncate(row.display_code),
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                self._truncate(row.supplier.upper()),
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                self._truncate(row.lookup_value),
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                self._truncate(row.status),
                                color=status_color,
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                self._truncate(part_text),
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                self._truncate(row.location or "-"),
                                size=12,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            )
                        ),
                        ft.DataCell(
                            ft.IconButton(
                                icon=ft.icons.DELETE,
                                on_click=lambda _, rid=row.row_id: self._remove_row(rid),
                            )
                        ),
                    ]
                )
            )

        self.fields["results_table"].rows = table_rows
        try:
            self._page.update()
        except AssertionError:
            pass

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
    def _truncate(value: str, max_len: int = 30) -> str:
        text = str(value or "")
        return text if len(text) <= max_len else text[: max_len - 3] + "..."

    def _get_location_pk(self, location_value: str) -> int:
        return inventree_interface.resolve_stock_location_pk(
            location_value, self.stock_location_id_map
        )

    def _on_apply(self, _):
        with self._rows_lock:
            valid_rows = [r for r in self.scanned_rows if r.part_pk]

        if not valid_rows:
            self.show_dialog(DialogType.ERROR, "No resolved items to update")
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

        total = len(valid_rows)
        self.fields["progress"].visible = True
        self.fields["progress"].value = 0
        self.fields["progress_message"].value = f"Processing 0/{total}"
        self.fields["progress_message"].color = "blue"
        try:
            self.fields["progress"].update()
            self.fields["progress_message"].update()
        except AssertionError:
            pass

        success = 0
        failed = 0
        failures: List[str] = []
        successful_row_ids: List[int] = []

        def _process_row(idx: int, row: ExistingPartScanRow) -> Dict[str, Any]:
            errors: List[str] = []
            ok = True

            try:
                part_pk = int(row.part_pk)

                # Always set part default location
                part_endpoint = f"{base_url.rstrip('/')}/api/part/{part_pk}/"
                resp = self._request_with_retries(
                    method="PATCH",
                    url=part_endpoint,
                    headers=headers,
                    json={"default_location": location_pk},
                    timeout=20,
                )
                if resp is None or resp.status_code not in [200, 202]:
                    ok = False
                    errors.append(f"{row.part_name}: failed to set default location on part")

                # Set location on all supplier parts of this part
                sp_list_resp = self._request_with_retries(
                    method="GET",
                    url=f"{base_url.rstrip('/')}/api/company/part/",
                    headers=headers,
                    params={"part": part_pk, "limit": 250},
                    timeout=20,
                )
                if sp_list_resp is not None:
                    sp_payload = sp_list_resp.json()
                    sp_rows = (
                        sp_payload.get("results", sp_payload)
                        if isinstance(sp_payload, dict)
                        else sp_payload
                    )
                    for sp in sp_rows if isinstance(sp_rows, list) else []:
                        sp_pk = sp.get("pk") or sp.get("id")
                        if not sp_pk:
                            continue
                        sp_resp = self._request_with_retries(
                            method="PATCH",
                            url=f"{base_url.rstrip('/')}/api/company/part/{sp_pk}/",
                            headers=headers,
                            json={"default_location": location_pk},
                            timeout=20,
                        )
                        if sp_resp is None or sp_resp.status_code not in [200, 202]:
                            errors.append(
                                f"{row.part_name}: supplier part pk={sp_pk} location update failed"
                            )

                # Transfer all stock items to new location
                stock_items: List[Dict] = []
                stock_url = f"{base_url.rstrip('/')}/api/stock/"
                stock_params: Dict[str, Any] = {"part": part_pk, "limit": 250}
                while stock_url:
                    sr = self._request_with_retries(
                        method="GET",
                        url=stock_url,
                        headers=headers,
                        params=stock_params,
                        timeout=20,
                    )
                    if sr is None:
                        break
                    sp2 = sr.json()
                    s_rows = sp2.get("results", sp2) if isinstance(sp2, dict) else sp2
                    for si in s_rows if isinstance(s_rows, list) else []:
                        try:
                            if (
                                si.get("location") is not None
                                and int(si["location"]) == location_pk
                            ):
                                continue
                        except Exception:
                            import logging

                            logging.exception("Exception comparing si['location'] and location_pk:")
                        stock_items.append(si)
                    stock_url = sp2.get("next") if isinstance(sp2, dict) else None
                    stock_params = {}

                if stock_items:
                    transfer_items = []
                    for si in stock_items:
                        try:
                            transfer_items.append(
                                {
                                    "pk": int(si.get("pk") or si.get("id")),
                                    "quantity": str(si.get("quantity") or "0"),
                                    "batch": str(si.get("batch") or ""),
                                    "packaging": str(si.get("packaging") or ""),
                                    "status": int(si.get("status") or 0),
                                }
                            )
                        except Exception:
                            continue

                    tr = self._request_with_retries(
                        method="POST",
                        url=f"{base_url.rstrip('/')}/api/stock/transfer/",
                        headers=headers,
                        json={
                            "items": transfer_items,
                            "location": location_pk,
                            "notes": "Ki-nTree location update",
                        },
                        timeout=30,
                    )
                    if tr is None or tr.status_code not in [200, 201, 202]:
                        # Per-item fallback
                        for si in stock_items:
                            si_pk = si.get("pk") or si.get("id")
                            if not si_pk:
                                continue
                            fr = self._request_with_retries(
                                method="PATCH",
                                url=f"{base_url.rstrip('/')}/api/stock/{si_pk}/",
                                headers=headers,
                                json={"location": location_pk},
                                timeout=20,
                            )
                            if fr is None or fr.status_code not in [200, 202]:
                                errors.append(
                                    f"{row.part_name}: stock item pk={si_pk} transfer failed"
                                )

            except Exception as exc:
                ok = False
                errors.append(f"{row.part_name or row.lookup_value}: {str(exc)[:60]}")

            return {"row": row, "ok": ok and not errors, "errors": errors}

        completed = 0
        with ThreadPoolExecutor(max_workers=min(4, max(1, total))) as executor:
            future_map = {
                executor.submit(_process_row, idx, row): row
                for idx, row in enumerate(valid_rows, start=1)
            }
            for future in as_completed(future_map):
                result = future.result()
                completed += 1
                if result["ok"]:
                    success += 1
                    successful_row_ids.append(result["row"].row_id)
                else:
                    failed += 1
                    failures.extend(result["errors"])
                self.fields["progress"].value = completed / total if total else 1.0
                self.fields["progress_message"].value = f"Processing {completed}/{total}"
                try:
                    self.fields["progress"].update()
                    self.fields["progress_message"].update()
                except AssertionError:
                    pass

        self.fields["progress"].value = 1.0
        self.fields["progress"].color = (
            "green" if failed == 0 else ("amber" if success > 0 else "red")
        )
        self.fields["progress_message"].value = f"Done: {success} success, {failed} failed"
        self.fields["progress_message"].color = "green" if failed == 0 else "orange"
        try:
            self.fields["progress"].update()
            self.fields["progress_message"].update()
        except AssertionError:
            pass

        if failures:
            detail = "\n".join(f"- {e}" for e in failures[:5])
            if len(failures) > 5:
                detail += f"\n- ... and {len(failures) - 5} more"
            self.show_dialog(DialogType.WARNING, f"Finished with issues:\n{detail}")
        else:
            self.show_dialog(DialogType.VALID, f"Updated {success} item(s) successfully")

        if successful_row_ids:
            with self._rows_lock:
                self.scanned_rows = [
                    r for r in self.scanned_rows if r.row_id not in successful_row_ids
                ]
            self._update_results_table()

        self._show_status_s2(
            f"Done: {success} success, {failed} failed",
            color="green" if failed == 0 else "orange",
        )
