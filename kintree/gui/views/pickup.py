"""Inventory pickup / put-down view for Ki-nTree."""

from collections import defaultdict
import threading
from typing import Dict, List, Optional

import flet as ft
import requests

from ...database import pickup_api
from ...database import pickup_history
from ...database.inventree_api import get_inventree_api
from ...search.barcode_parser import BarcodeParser
from .barcode import _BarcodeApiHelpers
from .common import GUI_PARAMS
from .main import MainView

_parser = BarcodeParser()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class PickupItem:
    """A single BOM line. A part with N distinct locations produces N items."""

    def __init__(
        self,
        part_pk: int,
        part_name: str,
        location: str,
        quantity: float = 1,
        reference: str = "",
    ):
        self.part_pk = part_pk
        self.part_name = part_name
        self.location = location  # single location string for this row
        self.quantity = quantity
        self.reference = reference
        self.checked = False  # manually ticked
        self.scanned = False  # confirmed via barcode scan


# ---------------------------------------------------------------------------
# Guided-mode modal
# ---------------------------------------------------------------------------


class GuidedPickupModal:
    """
    Two-page modal for guided OUT (pickup) and IN (put-down) flow.

    Page 0 — overview table grouped by location:
        OUT: input resolves location barcodes only.
        IN:  input resolves both part *and* location barcodes; a part scan
             jumps directly to the location that part belongs to.

    Page 1 — per-location detail:
        OUT: input resolves part barcodes only.
        IN:  input resolves part barcodes; a location barcode also navigates.
    """

    MODE_OUT = "out"
    MODE_IN = "in"

    def __init__(
        self,
        page: ft.Page,
        items: List[PickupItem],
        find_part_fn,
        on_close_fn,
        mode: str = "out",
        query: str = "",
        label: str = "",
        record_id: Optional[str] = None,
    ):
        """
        Parameters
        ----------
        page            Flet page reference.
        items           Full list of PickupItems from the BOM resolve.
        find_part_fn    Callable(lookup_value) -> Optional[dict]
        on_close_fn     Called when the modal closes; receives (items, record_id).
        mode            'out' (pickup) or 'in' (put-down).
        query           Original search query — used for history persistence.
        label           Human-readable op label.
        record_id       Existing history record to update, or None for new.
        """
        self._page = page
        self._items = items
        self._find_part = find_part_fn
        self._on_close = on_close_fn
        self._mode = mode
        self._query = query
        self._label = label
        self._record_id = record_id
        self._current_location: Optional[str] = None
        self._current_part_pk: Optional[int] = None  # IN mode: part scanned first
        self._parent_filter: Optional[str] = None  # set when a parent location was scanned
        self._scan_lock = threading.Lock()

        # Build controls
        _input_hint = (
            "Scan part or location barcode…" if mode == self.MODE_IN else "Scan location barcode…"
        )
        self._scanner_input = ft.TextField(
            hint_text=_input_hint,
            prefix_icon=ft.icons.QR_CODE_SCANNER,
            dense=True,
            autofocus=True,
            on_submit=self._on_scan_submit,
            on_change=self._on_scan_input_changed,
        )
        self._status_text = ft.Text("", size=12, italic=True, color="grey")

        # Page 0 — location overview table
        self._location_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Location")),
                ft.DataColumn(ft.Text("Part Name")),
                ft.DataColumn(ft.Text("Qty")),
                ft.DataColumn(ft.Text("Done")),
            ],
            rows=[],
            column_spacing=14,
            horizontal_margin=8,
            show_bottom_border=True,
        )
        # Header row for page 0 — swaps between hint text and parent-filter banner
        self._page0_back_btn = ft.IconButton(
            icon=ft.icons.ARROW_BACK,
            tooltip="Show all locations",
            on_click=lambda e: self._go_page0(),
            visible=False,
        )
        _p0_hint = (
            "Scan a part or location barcode to begin"
            if mode == self.MODE_IN
            else "Scan a location barcode to begin"
        )
        self._page0_header = ft.Text(
            _p0_hint,
            style=ft.TextThemeStyle.TITLE_MEDIUM,
        )
        self._page0_content = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        self._page0_back_btn,
                        ft.Container(
                            content=self._page0_header,
                            expand=True,
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(width=48),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=ft.ListView(
                        controls=[self._location_table],
                        expand=True,
                    ),
                    expand=True,
                    height=420,
                ),
            ],
            spacing=8,
            expand=True,
        )

        # Page 1 — per-location part scanning
        self._location_header = ft.Text(
            "",
            style=ft.TextThemeStyle.HEADLINE_SMALL,
            text_align=ft.TextAlign.CENTER,
            weight=ft.FontWeight.BOLD,
        )
        self._part_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Part Name")),
                ft.DataColumn(ft.Text("Qty")),
                ft.DataColumn(ft.Text("✓")),
            ],
            rows=[],
            column_spacing=14,
            horizontal_margin=8,
            show_bottom_border=True,
        )
        self._page1_content = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.icons.ARROW_BACK,
                            tooltip="Back to locations",
                            on_click=lambda e: self._go_page0(),
                        ),
                        ft.Container(
                            content=self._location_header,
                            expand=True,
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(width=48),  # balance the back button width
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=ft.ListView(
                        controls=[self._part_table],
                        expand=True,
                    ),
                    expand=True,
                    height=420,
                ),
            ],
            spacing=8,
            expand=True,
            visible=False,
        )

        # Page 1b — IN mode: part scanned first, confirm by scanning destination location
        self._part_header = ft.Text(
            "",
            style=ft.TextThemeStyle.HEADLINE_SMALL,
            text_align=ft.TextAlign.CENTER,
            weight=ft.FontWeight.BOLD,
        )
        self._location_list_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Put here")),
                ft.DataColumn(ft.Text("Qty")),
                ft.DataColumn(ft.Text("✓")),
            ],
            rows=[],
            column_spacing=14,
            horizontal_margin=8,
            show_bottom_border=True,
        )
        self._page1b_content = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.icons.ARROW_BACK,
                            tooltip="Back to overview",
                            on_click=lambda e: self._go_page0(),
                        ),
                        ft.Container(
                            content=self._part_header,
                            expand=True,
                            alignment=ft.alignment.center,
                        ),
                        ft.Container(width=48),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=ft.ListView(
                        controls=[self._location_list_table],
                        expand=True,
                    ),
                    expand=True,
                    height=420,
                ),
            ],
            spacing=8,
            expand=True,
            visible=False,
        )

        # Close button
        self._close_btn = ft.TextButton(
            "Close",
            icon=ft.icons.CLOSE,
            on_click=self._on_close_click,
        )

        total = len(self._items)
        self._counter_text = ft.Text(
            f"0 / {total}",
            size=13,
            weight=ft.FontWeight.BOLD,
            color="grey",
        )

        _title = "Guided Put-Down — IN" if mode == self.MODE_IN else "Guided Pickup — OUT"
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                controls=[
                    ft.Text(_title, expand=True),
                    self._counter_text,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        self._scanner_input,
                        self._status_text,
                        ft.Divider(height=6),
                        self._page0_content,
                        self._page1_content,
                        self._page1b_content,
                    ],
                    spacing=6,
                    expand=True,
                ),
                width=820,
                height=600,
                padding=ft.padding.only(top=8),
            ),
            actions=[self._close_btn],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        self._rebuild_location_table()

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def open(self):
        self._refresh_counter()
        self._page.open(self._dialog)

        # Defer focus slightly so the dialog is fully rendered before focusing
        def _deferred_focus():
            import time

            time.sleep(0.15)
            self._focus_input()

        threading.Thread(target=_deferred_focus, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Navigation                                                          #
    # ------------------------------------------------------------------ #

    def _go_page0(self):
        self._current_location = None
        self._current_part_pk = None
        self._parent_filter = None
        self._page0_content.visible = True
        self._page1_content.visible = False
        self._page1b_content.visible = False
        self._scanner_input.hint_text = (
            "Scan part or location barcode…"
            if self._mode == self.MODE_IN
            else "Scan location barcode…"
        )
        self._page0_header.value = (
            "Scan a part or location barcode to begin"
            if self._mode == self.MODE_IN
            else "Scan a location barcode to begin"
        )
        self._page0_header.style = ft.TextThemeStyle.TITLE_MEDIUM
        self._page0_header.weight = ft.FontWeight.NORMAL
        self._page0_header.text_align = ft.TextAlign.LEFT
        self._page0_back_btn.visible = False
        self._rebuild_location_table()
        self._set_status("", color="grey")
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._page1b_content.update()
            self._scanner_input.update()
        except Exception:
            import logging

            logging.exception("Exception updating page1b_content or scanner_input:")

    def _go_page_parent(self, parent_name: str):
        """Filter page 0 to show only child locations of *parent_name*."""
        self._parent_filter = parent_name
        self._current_location = None
        self._current_part_pk = None
        self._page0_content.visible = True
        self._page1_content.visible = False
        self._page1b_content.visible = False
        self._scanner_input.hint_text = "Scan a child location barcode…"
        self._page0_header.value = parent_name
        self._page0_header.style = ft.TextThemeStyle.HEADLINE_SMALL
        self._page0_header.weight = ft.FontWeight.BOLD
        self._page0_header.text_align = ft.TextAlign.CENTER
        self._page0_back_btn.visible = True
        self._rebuild_location_table()
        self._set_status("Scan or tap a child location.", color="grey")
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._scanner_input.update()
        except Exception:
            import logging

            logging.exception("Exception updating page0_content, page1_content, or scanner_input:")

    def _go_page1(self, location: str):
        self._current_location = location
        self._current_part_pk = None
        # Show only the leaf segment (last part after the final '/')
        self._location_header.value = location.rsplit("/", 1)[-1]
        self._page0_content.visible = False
        self._page1_content.visible = True
        self._page1b_content.visible = False
        self._scanner_input.hint_text = (
            "Scan a part barcode…"
            if self._mode == self.MODE_OUT
            else "Scan part or location barcode…"
        )
        verb = "pick up" if self._mode == self.MODE_OUT else "put down"
        self._rebuild_part_table()
        self._set_status(f"Scan parts to {verb} from this location.", color="grey")
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._page1b_content.update()
            self._location_header.update()
            self._scanner_input.update()
        except Exception:
            import logging

            logging.exception(
                "Exception updating page0_content, page1_content, page1b_content, location_header, or scanner_input:"
            )

    def _go_page1b(self, part_pk: int, part_name: str):
        """IN mode: part scanned first — show destination locations, confirm by scanning one."""
        self._current_part_pk = part_pk
        self._current_location = None
        self._part_header.value = part_name
        self._page0_content.visible = False
        self._page1_content.visible = False
        self._page1b_content.visible = True
        self._scanner_input.hint_text = "Scan destination location barcode…"
        self._rebuild_location_list_table()
        self._set_status("Scan the location where you are putting this part.", color="grey")
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._page1b_content.update()
            self._part_header.update()
            self._scanner_input.update()
        except Exception:
            import logging

            logging.exception(
                "Exception updating part_header, page0_content, page1_content, page1b_content, or scanner_input:"
            )

    # ------------------------------------------------------------------ #
    #  Scanner input                                                       #
    # ------------------------------------------------------------------ #

    def _on_scan_input_changed(self, e):
        """Handle scanners that embed newlines inside the barcode payload."""
        text = e.control.value or ""
        normalized = text.replace("\r", "\n")
        if "\n" not in normalized:
            return
        # Split into completed lines; keep any trailing fragment in the field.
        has_trailing = normalized.endswith("\n")
        raw_lines = normalized.split("\n")
        completed = raw_lines if has_trailing else raw_lines[:-1]
        pending = "" if has_trailing else (raw_lines[-1] if raw_lines else "")
        lines = [line.strip() for line in completed if line.strip()]
        e.control.value = pending
        try:
            e.control.update()
        except Exception:
            import logging

            logging.exception("Exception updating e.control in _on_scan_input_changed:")
        for line in lines:
            self._dispatch_scan(line)

    def _on_scan_submit(self, e):
        raw = (e.control.value or "").strip()
        e.control.value = ""
        try:
            e.control.update()
        except Exception:
            import logging

            logging.exception("Exception updating e.control in _on_scan_submit:")
        if not raw:
            return
        self._dispatch_scan(raw)

    def _dispatch_scan(self, raw: str):
        """Route a completed scan string to the appropriate resolver.

        OUT mode:
          • Page 0 → location only.
          • Page 1 → part only.

        IN mode:
          • Page 0 → try part first (jumps to that part's location),
                      fall back to location if part lookup fails.
          • Page 1 → try part first; if it resolves to a *different*
                      location, navigate there instead.
        """
        if self._mode == self.MODE_IN:
            threading.Thread(
                target=self._resolve_scan_in,
                args=(raw,),
                daemon=True,
            ).start()
        elif self._current_location is None:
            threading.Thread(
                target=self._resolve_location_scan,
                args=(raw,),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=self._resolve_part_scan,
                args=(raw,),
                daemon=True,
            ).start()

    def _resolve_location_scan(self, raw: str):
        """Resolve *raw* as an InvenTree location barcode.

        If it resolves to a parent location (has child locations that contain
        BOM items), switches to the parent-filter view.  Otherwise navigates
        directly to page 1 for that location.
        """
        api = get_inventree_api()
        if not api:
            self._set_status("Not connected to InvenTree.", color="red")
            return

        location_name = None
        sublocations = 0

        # POST /api/barcode/ — InvenTree's universal barcode resolver
        try:
            result = api.post("barcode/", data={"barcode": raw})
            if isinstance(result, dict):
                loc = result.get("stocklocation")
                if loc:
                    loc_pk = loc.get("pk") or loc.get("id")
                    if loc_pk:
                        loc_obj = api.get(f"stock/location/{loc_pk}/")
                        if isinstance(loc_obj, dict):
                            location_name = (
                                loc_obj.get("pathstring") or loc_obj.get("name") or str(loc_pk)
                            )
                            sublocations = int(loc_obj.get("sublocations") or 0)
        except Exception:
            import logging

            logging.exception("Exception in location lookup:")

        # Fallback: match raw text against known location strings (exact or suffix)
        if not location_name:
            known = {
                item.location
                for item in self._items
                if item.location and item.location != "(no location)"
            }
            # Also collect all unique path prefixes as potential parent names
            all_prefixes: set = set()
            for loc in known:
                parts = loc.split("/")
                for i in range(1, len(parts)):
                    all_prefixes.add("/".join(parts[:i]))
            raw_lower = raw.strip().lower()
            for candidate in known | all_prefixes:
                if candidate.lower() == raw_lower or candidate.lower().endswith("/" + raw_lower):
                    location_name = candidate
                    break

        if not location_name:
            self._set_status(f"Location not recognised: {raw}", color="orange")
            self._focus_input()
            return

        # Check if this is a parent location: has children that contain BOM items
        child_items = [
            it
            for it in self._items
            if it.location.startswith(location_name + "/") and it.location != location_name
        ]
        direct_items = [it for it in self._items if it.location == location_name]

        if (sublocations > 0 or child_items) and not direct_items:
            # Pure parent — filter to its children
            if not child_items:
                self._set_status(f"No BOM items under: {location_name}", color="orange")
                self._focus_input()
                return
            self._go_page_parent(location_name)
        elif direct_items:
            # Leaf or mixed — go straight to part scanning
            self._go_page1(location_name)
        else:
            self._set_status(f"No BOM items for location: {location_name}", color="orange")
            self._focus_input()

    def _resolve_part_scan(self, raw: str):
        """Try to match *raw* against BOM items for the current location.

        The raw scan is first decoded by BarcodeParser so that manufacturer
        2D barcodes (DigiKey, Mouser, LCSC, TME) are understood; the
        manufacturer PN or supplier PN extracted from the parsed result is
        used as the lookup value fed to InvenTree.
        """
        loc_items = [it for it in self._items if it.location == self._current_location]

        matched: Optional[PickupItem] = None

        # Decode the raw scan with BarcodeParser — same logic as barcode page
        parsed = _parser.parse(raw)
        supplier = parsed.get("supplier", "unknown")
        if supplier == "unknown":
            # Unknown format: treat raw value as-is (InvenTree internal barcode,
            # plain IPN, part name, etc.)
            lookup_candidates = [raw.strip()]
        else:
            mpn = str(parsed.get("manufacturer_pn") or "").strip()
            spn = str(parsed.get("supplier_pn") or "").strip()
            # Prefer MPN then SPN; fall back to raw if both are empty
            lookup_candidates = [v for v in [mpn, spn] if v] or [raw.strip()]

        # Try InvenTree lookup for each candidate
        for candidate in lookup_candidates:
            result = self._find_part(candidate)
            if result:
                found_pk = int(result.get("pk") or result.get("id") or 0)
                for item in loc_items:
                    if item.part_pk == found_pk:
                        matched = item
                        break
            if matched:
                break

        # Fallback: exact name/IPN match against loc_items (case-insensitive)
        if not matched:
            for candidate in lookup_candidates:
                cand_lower = candidate.lower()
                for item in loc_items:
                    if item.part_name.lower() == cand_lower:
                        matched = item
                        break
                if matched:
                    break

        if not matched:
            self._set_status(f"Part not found in this location: {raw}", color="orange")
            self._focus_input()
            return

        with self._scan_lock:
            matched.scanned = True

        self._set_status(f"✓ {matched.part_name}", color="green")
        self._refresh_counter()
        self._rebuild_part_table()

        # All done for this location? Auto-return to page 0.
        remaining = [it for it in loc_items if not it.scanned and not it.checked]
        if not remaining:
            verb = "put down at" if self._mode == self.MODE_IN else "picked for"
            self._set_status(f"All parts {verb} {self._current_location}!", color="green")
            try:
                self._status_text.update()
            except Exception:
                import logging

                logging.exception("Exception updating status_text:")
            import time

            time.sleep(1.2)
            self._go_page0()
        else:
            self._focus_input()

    def _resolve_scan_in(self, raw: str):
        """IN-mode unified resolver: tries part lookup first, then location.

        Page 0 / page 1b:
          • Part scan → go to page 1b (part name big, list destination locations,
            confirm by scanning a location).
        Page 1b:
          • Location scan → confirm that location for the current part.
        Page 1 (location-first path, reached via location scan on page 0):
          • Part scan → mark part done in that location.
          • Location scan → navigate to that location.
        """
        # ---- On page 1b: expect a location confirmation scan ----
        if self._current_part_pk is not None:
            self._resolve_location_confirm_in(raw)
            return

        # ---- Try part lookup (works on page 0 and page 1) ----
        parsed = _parser.parse(raw)
        supplier = parsed.get("supplier", "unknown")
        if supplier == "unknown":
            lookup_candidates = [raw.strip()]
        else:
            mpn = str(parsed.get("manufacturer_pn") or "").strip()
            spn = str(parsed.get("supplier_pn") or "").strip()
            lookup_candidates = [v for v in [mpn, spn] if v] or [raw.strip()]

        found_pk = 0
        found_name = ""
        for candidate in lookup_candidates:
            result = self._find_part(candidate)
            if result:
                found_pk = int(result.get("pk") or result.get("id") or 0)
                found_name = str(result.get("name") or result.get("IPN") or candidate)
                if found_pk:
                    break

        # Also try exact name/IPN match against all BOM items
        if not found_pk:
            for candidate in lookup_candidates:
                cand_lower = candidate.lower()
                for item in self._items:
                    if item.part_name.lower() == cand_lower:
                        found_pk = item.part_pk
                        found_name = item.part_name
                        break
                if found_pk:
                    break

        if found_pk:
            matched_items = [it for it in self._items if it.part_pk == found_pk]
            if not matched_items:
                self._set_status(f"Part not in this BOM: {raw}", color="orange")
                self._focus_input()
                return

            # On page 1 (location-first path): mark done if part belongs here
            if self._current_location is not None:
                loc_match = next(
                    (it for it in matched_items if it.location == self._current_location),
                    None,
                )
                if loc_match:
                    with self._scan_lock:
                        loc_match.scanned = True
                    self._set_status(f"✓ {loc_match.part_name}", color="green")
                    self._refresh_counter()
                    self._rebuild_part_table()
                    loc_items = [it for it in self._items if it.location == self._current_location]
                    remaining = [it for it in loc_items if not it.scanned and not it.checked]
                    if not remaining:
                        self._set_status(
                            f"All parts put down at {self._current_location}!",
                            color="green",
                        )
                        try:
                            self._status_text.update()
                        except Exception:
                            import logging

                            logging.exception("Exception updating status_text:")
                        import time

                        time.sleep(1.2)
                        self._go_page0()
                    else:
                        self._focus_input()
                    return
                # Part belongs elsewhere; switch to page 1b for it
                if not found_name:
                    found_name = matched_items[0].part_name
                self._go_page1b(found_pk, found_name)
                return

            # On page 0: go to page 1b — show destination locations, wait for location scan
            if not found_name:
                found_name = matched_items[0].part_name
            self._go_page1b(found_pk, found_name)
            return

        # ---- No part matched: try as a location barcode ----
        self._resolve_location_scan(raw)

    def _confirm_location_in(self, item: PickupItem):
        """Mark *item* done via a tap on its location row (page 1b)."""
        with self._scan_lock:
            item.scanned = True
        self._set_status(f"✓ {item.location.rsplit('/', 1)[-1]}", color="green")
        self._refresh_counter()
        self._rebuild_location_list_table()
        part_items = [it for it in self._items if it.part_pk == self._current_part_pk]
        remaining = [it for it in part_items if not it.scanned and not it.checked]
        if not remaining:
            try:
                self._status_text.update()
            except Exception:
                import logging

                logging.exception("Exception updating status_text:")
            import time

            time.sleep(1.2)
            self._go_page0()
        else:
            self._focus_input()

    def _resolve_location_confirm_in(self, raw: str):
        """Page-1b: resolve *raw* as a location and mark the matching part-item done."""
        api = get_inventree_api()
        location_name = None

        if api:
            try:
                result = api.post("barcode/", data={"barcode": raw})
                if isinstance(result, dict):
                    loc = result.get("stocklocation")
                    if loc:
                        loc_pk = loc.get("pk") or loc.get("id")
                        if loc_pk:
                            loc_obj = api.get(f"stock/location/{loc_pk}/")
                            if isinstance(loc_obj, dict):
                                location_name = (
                                    loc_obj.get("pathstring") or loc_obj.get("name") or str(loc_pk)
                                )
            except Exception:
                import logging

                logging.exception("Exception updating status_text (part location fallback):")

        # Fallback: match raw against locations of the current part's items
        if not location_name:
            part_locs = {it.location for it in self._items if it.part_pk == self._current_part_pk}
            raw_lower = raw.strip().lower()
            for candidate in part_locs:
                if (
                    candidate.lower() == raw_lower
                    or candidate.rsplit("/", 1)[-1].lower() == raw_lower
                ):
                    location_name = candidate
                    break

        if not location_name:
            self._set_status(f"Location not recognised: {raw}", color="orange")
            self._focus_input()
            return

        # Find the matching item for this part + location
        part_items = [it for it in self._items if it.part_pk == self._current_part_pk]
        target = next((it for it in part_items if it.location == location_name), None)

        # Also try prefix/suffix match (pathstring may differ in trailing detail)
        if not target:
            for it in part_items:
                if it.location.lower().endswith(
                    location_name.lower()
                ) or location_name.lower().endswith(it.location.lower()):
                    target = it
                    break

        if not target:
            self._set_status(f"Location not in destination list: {location_name}", color="orange")
            self._focus_input()
            return

        self._confirm_location_in(target)

    # ------------------------------------------------------------------ #
    #  Table builders                                                      #
    # ------------------------------------------------------------------ #

    def _rebuild_location_table(self):
        """Rebuild page-0 table, sorted by location, grouped visually.

        When ``_parent_filter`` is set, only locations whose pathstring starts
        with that parent are shown.
        """
        by_loc: Dict[str, List[PickupItem]] = defaultdict(list)
        for item in self._items:
            if self._parent_filter:
                # Keep locations that are the parent itself or a child of it
                if not (
                    item.location == self._parent_filter
                    or item.location.startswith(self._parent_filter + "/")
                ):
                    continue
            by_loc[item.location].append(item)

        rows = []
        for loc in sorted(by_loc.keys(), key=lambda line: (line == "(no location)", line.lower())):
            loc_items = by_loc[loc]
            done_count = sum(1 for it in loc_items if it.scanned or it.checked)
            all_done = done_count == len(loc_items)

            for i, item in enumerate(loc_items):
                done = item.scanned or item.checked
                qty_str = (
                    str(int(item.quantity))
                    if item.quantity == int(item.quantity)
                    else str(item.quantity)
                )

                # Show location only on the first row of each group; make it clickable
                if i == 0:
                    loc_cell = ft.DataCell(
                        ft.TextButton(
                            text=loc,
                            style=ft.ButtonStyle(
                                color="green" if all_done else ft.colors.PRIMARY,
                                padding=ft.padding.all(0),
                            ),
                            on_click=lambda e, line=loc: self._go_page1(line),
                        )
                    )
                else:
                    loc_cell = ft.DataCell(ft.Text("", size=12))

                name_cell = ft.DataCell(
                    ft.Text(
                        item.part_name,
                        size=12,
                        no_wrap=True,
                        color="green" if done else None,
                    )
                )
                qty_cell = ft.DataCell(ft.Text(qty_str, size=12))
                done_cell = ft.DataCell(
                    ft.Checkbox(
                        value=done,
                        on_change=lambda e, it=item: self._on_manual_tick(e, it),
                    )
                )
                rows.append(ft.DataRow(cells=[loc_cell, name_cell, qty_cell, done_cell]))

        self._location_table.rows = rows
        if self._location_table.page:
            try:
                self._location_table.update()
            except AssertionError:
                pass

    def _rebuild_location_list_table(self):
        """Rebuild page-1b table: destination locations for the current part (IN mode)."""
        part_items = [it for it in self._items if it.part_pk == self._current_part_pk]
        rows = []
        for item in part_items:
            done = item.scanned or item.checked
            qty_str = (
                str(int(item.quantity))
                if item.quantity == int(item.quantity)
                else str(item.quantity)
            )
            loc_leaf = item.location.rsplit("/", 1)[-1]
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.TextButton(
                                text=loc_leaf,
                                tooltip=item.location,
                                style=ft.ButtonStyle(
                                    color="green" if done else ft.colors.PRIMARY,
                                    padding=ft.padding.all(0),
                                ),
                                on_click=lambda e, it=item: self._confirm_location_in(it),
                            )
                        ),
                        ft.DataCell(ft.Text(qty_str, size=12)),
                        ft.DataCell(
                            ft.Checkbox(
                                value=done,
                                on_change=lambda e, it=item: self._on_manual_tick(e, it),
                            )
                        ),
                    ],
                    color=ft.colors.GREEN_50 if done else None,
                )
            )
        self._location_list_table.rows = rows
        try:
            self._location_list_table.update()
        except Exception:
            import logging

            logging.exception("Exception updating location_list_table:")

    def _rebuild_part_table(self):
        """Rebuild page-1 table for the current location."""
        loc_items = [it for it in self._items if it.location == self._current_location]
        rows = []
        for item in loc_items:
            done = item.scanned or item.checked
            qty_str = (
                str(int(item.quantity))
                if item.quantity == int(item.quantity)
                else str(item.quantity)
            )
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                item.part_name,
                                size=12,
                                no_wrap=True,
                                color="green" if done else None,
                                weight=ft.FontWeight.BOLD if done else ft.FontWeight.NORMAL,
                            )
                        ),
                        ft.DataCell(ft.Text(qty_str, size=12)),
                        ft.DataCell(
                            ft.Checkbox(
                                value=done,
                                on_change=lambda e, it=item: self._on_manual_tick(e, it),
                            )
                        ),
                    ],
                    color=ft.colors.GREEN_50 if done else None,
                )
            )
        self._part_table.rows = rows
        try:
            self._part_table.update()
        except Exception:
            import logging

            logging.exception("Exception updating part_table:")

    # ------------------------------------------------------------------ #
    #  Manual tick                                                         #
    # ------------------------------------------------------------------ #

    def _on_manual_tick(self, e, item: PickupItem):
        with self._scan_lock:
            item.checked = bool(e.control.value)
            item.scanned = item.checked

        self._refresh_counter()

        if self._current_part_pk is not None:
            # Page 1b — IN mode, location-confirm view
            self._rebuild_location_list_table()
            part_items = [it for it in self._items if it.part_pk == self._current_part_pk]
            remaining = [it for it in part_items if not it.scanned and not it.checked]
            if not remaining:
                import time

                time.sleep(0.6)
                self._go_page0()
        elif self._current_location is not None:
            # Page 1 — part scanning view
            self._rebuild_part_table()
            loc_items = [it for it in self._items if it.location == self._current_location]
            remaining = [it for it in loc_items if not it.scanned and not it.checked]
            if not remaining:
                import time

                time.sleep(0.6)
                self._go_page0()
        else:
            self._rebuild_location_table()

        self._focus_input()

    # ------------------------------------------------------------------ #
    #  Close / save                                                        #
    # ------------------------------------------------------------------ #

    def _on_close_click(self, e):
        """First click: save to history, then warn if incomplete."""
        # Always save current state before closing
        self._record_id = pickup_history.save_op(
            record_id=self._record_id,
            query=self._query,
            label=self._label,
            mode=self._mode,
            items=self._items,
        )

        incomplete = [it for it in self._items if not it.scanned and not it.checked]
        if incomplete:
            verb = "put down" if self._mode == self.MODE_IN else "picked"
            self._close_btn.text = f"Close anyway ({len(incomplete)} remaining)"
            self._close_btn.icon = ft.icons.WARNING_AMBER_ROUNDED
            self._close_btn.icon_color = "orange"
            self._status_text.value = (
                f"Saved. {len(incomplete)} item(s) not yet {verb} — resume from history."
            )
            self._status_text.color = "orange"
            self._close_btn.on_click = self._force_close
            try:
                self._close_btn.update()
                self._status_text.update()
            except Exception:
                import logging

                logging.exception("Exception updating close_btn or status_text:")
            return
        self._force_close(e)

    def _force_close(self, e):
        self._page.close(self._dialog)
        self._on_close(self._items, self._record_id)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _refresh_counter(self):
        done = sum(1 for it in self._items if it.scanned or it.checked)
        total = len(self._items)
        self._counter_text.value = f"{done} / {total}"
        self._counter_text.color = "green" if done == total else "grey"
        if self._counter_text.page:
            try:
                self._counter_text.update()
            except AssertionError:
                pass

    def _set_status(self, msg: str, color: str = "grey"):
        self._status_text.value = msg
        self._status_text.color = color
        try:
            self._status_text.update()
        except Exception:
            import logging

            logging.exception("Exception updating status_text:")

    def _focus_input(self):
        import time

        for _ in range(2):
            try:
                self._scanner_input.focus()
                self._scanner_input.update()
                return
            except Exception:
                import logging

                logging.exception("Exception focusing or updating scanner_input:")
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------


class PickupView(MainView):
    """Inventory pickup (Out) and put-down (In) view."""

    title = "Pickup"
    fields: Dict = {}

    MODE_OUT = "out"
    MODE_IN = "in"

    def __init__(self, page: ft.Page):
        self._mode = self.MODE_OUT
        self._items: List[PickupItem] = []
        self._search_thread: Optional[threading.Thread] = None
        self._guided_modal: Optional[GuidedPickupModal] = None

        # History state — set when loading from a saved record
        self._current_record_id: Optional[str] = None
        self._current_query: str = ""
        self._current_label: str = ""

        # Part lookup — same cache/lock/http attributes expected by _BarcodeApiHelpers
        self._http = requests.Session()
        self._part_lookup_cache: Dict[str, Optional[Dict]] = {}
        self._part_lookup_lock = threading.Lock()
        self._part_lookup_inflight: Dict[str, threading.Event] = {}

        super().__init__(page=page)
        self.build_page()

    # ------------------------------------------------------------------ #
    #  Build UI                                                            #
    # ------------------------------------------------------------------ #

    def build_page(self) -> None:
        self.fields["bom_search"] = ft.TextField(
            label="Search Assembly / BOM",
            hint_text="Part name  or  BO-123",
            width=GUI_PARAMS["textfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            prefix_icon=ft.icons.SEARCH,
            on_submit=self._on_search,
        )
        self.fields["search_btn"] = ft.ElevatedButton(
            text="Load",
            icon=ft.icons.DOWNLOAD_OUTLINED,
            on_click=self._on_search,
        )

        # Mode toggle
        self._mode_label = ft.Text(
            "Out (Pickup)",
            size=14,
            weight=ft.FontWeight.BOLD,
            color="blue",
        )
        self.fields["mode_toggle"] = ft.Switch(
            label="",
            value=False,
            on_change=self._on_mode_change,
        )

        self._status_text = ft.Text("", size=13, color="grey", italic=True)
        self._progress = ft.ProgressBar(visible=False, width=GUI_PARAMS["textfield_width"])

        search_row = ft.Row(
            controls=[
                self.fields["bom_search"],
                ft.Container(width=8),
                self.fields["search_btn"],
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        mode_row = ft.Row(
            controls=[
                ft.Text("Mode:", size=14),
                ft.Container(width=4),
                ft.Text("Out", size=13),
                self.fields["mode_toggle"],
                ft.Text("In", size=13),
                ft.Container(width=16),
                self._mode_label,
                ft.Container(expand=True),
                self._status_text,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # History table
        self._history_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Label")),
                ft.DataColumn(ft.Text("OUT")),
                ft.DataColumn(ft.Text("IN")),
                ft.DataColumn(ft.Text("Last edited")),
                ft.DataColumn(ft.Text("Actions")),
            ],
            rows=[],
            column_spacing=12,
            horizontal_margin=8,
            show_bottom_border=True,
        )
        self._history_section = ft.Column(
            controls=[
                ft.Divider(height=12),
                ft.Text(
                    "Operation History",
                    style=ft.TextThemeStyle.TITLE_MEDIUM,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.ListView(controls=[self._history_table], expand=False, height=220),
            ],
            spacing=4,
            visible=False,
        )

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "Inventory Pickup / Put-Down",
                                style=ft.TextThemeStyle.HEADLINE_SMALL,
                            ),
                            ft.Divider(height=8),
                            search_row,
                            self._progress,
                            ft.Divider(height=4),
                            mode_row,
                            self._history_section,
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        spacing=6,
                    ),
                    padding=20,
                    expand=True,
                ),
            ],
            expand=True,
        )

        # Populate history on initial load
        self._rebuild_history_table()

    # ------------------------------------------------------------------ #
    #  Event handlers                                                      #
    # ------------------------------------------------------------------ #

    def _on_search(self, e, _keep_record_id: bool = False):
        query = (self.fields["bom_search"].value or "").strip()
        if not query:
            self._set_status("Enter a part name or build order (BO-xxx).", color="orange")
            return

        self._current_query = query
        self._current_label = query
        if not _keep_record_id:
            self._current_record_id = None

        self.fields["search_btn"].disabled = True
        self.fields["bom_search"].disabled = True
        self._progress.visible = True
        self._set_status(f'Loading BOM for "{query}"…', color="grey")
        try:
            self.fields["search_btn"].update()
            self.fields["bom_search"].update()
            self._progress.update()
        except Exception:
            import logging

            logging.exception("Exception updating part_table:")

        def _run():
            ok, message, raw_items = pickup_api.resolve_query(query)
            # Expand each BOM entry into one PickupItem per distinct location
            items = []
            for r in raw_items:
                for loc in r.get("locations", ["(no location)"]):
                    items.append(
                        PickupItem(
                            part_pk=r["part_pk"],
                            part_name=r["part_name"],
                            location=loc,
                            quantity=r["quantity"],
                            reference=r.get("reference", ""),
                        )
                    )
            # Use the server-returned label if available (e.g. "Part: Resistor 10k")
            if ok and message:
                # message format: "Part: Foo — N item(s)"  or  "Build order BO-1 — N item(s)"
                self._current_label = message.split(" — ")[0].strip()

            self.fields["search_btn"].disabled = False
            self.fields["bom_search"].disabled = False
            self._progress.visible = False
            try:
                self.fields["search_btn"].update()
                self.fields["bom_search"].update()
                self._progress.update()
            except Exception:
                import logging

                logging.exception(
                    "Exception updating search_btn, bom_search, or progress (threaded):"
                )

            self._load_items(items)
            self._set_status(message, color="green" if ok else "red")

            if ok and items:
                if _keep_record_id:
                    # Called from history action — go straight to modal
                    self._open_guided_modal()
                else:
                    match = self._find_history_match(query)
                    if match:
                        self._show_continuation_dialog(match, items)
                    else:
                        self._open_guided_modal()

        self._search_thread = threading.Thread(target=_run, daemon=True)
        self._search_thread.start()

    def _on_mode_change(self, e):
        if e.control.value:
            self._mode = self.MODE_IN
            self._mode_label.value = "In (Put-Down)"
            self._mode_label.color = "green"
        else:
            self._mode = self.MODE_OUT
            self._mode_label.value = "Out (Pickup)"
            self._mode_label.color = "blue"
        try:
            self._mode_label.update()
        except Exception:
            import logging

            logging.exception("Exception updating search_btn, bom_search, or progress:")

    # ------------------------------------------------------------------ #
    #  Guided modal                                                        #
    # ------------------------------------------------------------------ #

    def _find_history_match(self, query: str) -> Optional[Dict]:
        """Return the most-recent history record whose query matches *query*, or None."""
        q = query.strip().lower()
        for rec in pickup_history.list_records():
            if rec.get("query", "").strip().lower() == q:
                return rec
        return None

    def _show_continuation_dialog(self, record: Dict, fresh_items: List["PickupItem"]):
        """Show a dialog offering to resume/continue an existing op or start fresh."""
        out_st = record.get("out", {}).get("status", "not_started")
        in_st = record.get("in", {}).get("status", "not_started")
        label = record.get("label", record.get("query", ""))

        _ST = {
            "complete": "✓ Complete",
            "incomplete": "… Incomplete",
            "not_started": "— Not started",
        }
        summary = f"OUT: {_ST.get(out_st, out_st)}    |    IN: {_ST.get(in_st, in_st)}"

        actions = []

        def _close_dlg(dlg):
            try:
                self._page.close(dlg)
            except Exception:
                pass

        # OUT buttons
        if out_st == "incomplete":

            def _do_resume_out(e, d=record):
                _close_dlg(dlg)
                self._history_resume(d, "out")

            actions.append(
                ft.TextButton(
                    "Resume OUT",
                    icon=ft.icons.PLAY_ARROW,
                    style=ft.ButtonStyle(color="blue"),
                    on_click=_do_resume_out,
                )
            )
        elif out_st == "not_started":

            def _do_start_out(e, d=record):
                _close_dlg(dlg)
                self._history_start_out(d)

            actions.append(
                ft.TextButton(
                    "Start OUT",
                    icon=ft.icons.OUTPUT,
                    style=ft.ButtonStyle(color="blue"),
                    on_click=_do_start_out,
                )
            )

        # IN buttons
        if in_st == "incomplete":

            def _do_resume_in(e, d=record):
                _close_dlg(dlg)
                self._history_resume(d, "in")

            actions.append(
                ft.TextButton(
                    "Resume IN",
                    icon=ft.icons.PLAY_ARROW,
                    style=ft.ButtonStyle(color="teal"),
                    on_click=_do_resume_in,
                )
            )
        elif in_st == "not_started":

            def _do_start_in(e, d=record):
                _close_dlg(dlg)
                self._history_start_mode(d, "in")

            actions.append(
                ft.TextButton(
                    "Start IN",
                    icon=ft.icons.INPUT,
                    style=ft.ButtonStyle(color="teal"),
                    on_click=_do_start_in,
                )
            )

        # Fresh OUT — new record, use already-loaded fresh_items (no server refetch)
        def _do_fresh(e):
            _close_dlg(dlg)
            self._current_record_id = None
            self._mode = self.MODE_OUT
            self.fields["mode_toggle"].value = False
            self._mode_label.value = "Out (Pickup)"
            self._mode_label.color = "blue"
            try:
                self.fields["mode_toggle"].update()
                self._mode_label.update()
            except Exception:
                pass
            self._load_items(fresh_items)
            self._open_guided_modal()

        actions.append(
            ft.TextButton(
                "New OUT",
                icon=ft.icons.REFRESH,
                style=ft.ButtonStyle(color="grey"),
                on_click=_do_fresh,
            )
        )

        # Dismiss (do nothing, items already loaded)
        def _do_dismiss(e):
            _close_dlg(dlg)

        actions.append(ft.TextButton("Dismiss", on_click=_do_dismiss))

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Previous op found: {label}"),
            content=ft.Text(summary, size=13),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.open(dlg)

    def _open_guided_modal(self):
        self._guided_modal = GuidedPickupModal(
            page=self._page,
            items=self._items,
            find_part_fn=self._find_part_by_lookup_simple,
            on_close_fn=self._on_guided_close,
            mode=self._mode,
            query=self._current_query,
            label=self._current_label,
            record_id=self._current_record_id,
        )
        self._guided_modal.open()

    def _on_guided_close(self, items: List[PickupItem], record_id: Optional[str]):
        """Called when the modal closes; sync state and refresh history."""
        self._items = items
        self._current_record_id = record_id
        self._rebuild_history_table()
        done = sum(1 for it in items if it.scanned or it.checked)
        verb = "put down" if self._mode == self.MODE_IN else "picked"
        self._set_status(
            f"Saved. {done}/{len(items)} item(s) {verb}.",
            color="green" if done == len(items) else "orange",
        )

    def _find_part_by_lookup_simple(self, lookup_value: str) -> Optional[Dict]:
        """Part lookup reusing _BarcodeApiHelpers.find_part_by_lookup.

        Requires the cache/lock attributes that are initialised in __init__.
        """
        return _BarcodeApiHelpers.find_part_by_lookup(self, lookup_value)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _load_items(self, items: List[PickupItem]):
        self._items = items

    # ------------------------------------------------------------------ #
    #  History panel                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mode_counter(mode_dict: Dict) -> tuple:
        """Return (text, color) like '3 / 5' for a saved mode dict."""
        items = mode_dict.get("items", [])
        if not items:
            return ("—", "grey")
        total = len(items)
        done = sum(1 for it in items if it.get("scanned") or it.get("checked"))
        text = f"{done} / {total}"
        color = "green" if done == total else ("orange" if done > 0 else "grey")
        return (text, color)

    def _rebuild_history_table(self):
        """Reload history records from disk and repopulate the history table."""
        records = pickup_history.list_records()
        self._history_section.visible = bool(records)
        rows = []
        for rec in records:
            label = rec.get("label", rec.get("query", "?"))
            out_st = rec.get("out", {}).get("status", "not_started")
            in_st = rec.get("in", {}).get("status", "not_started")
            updated = rec.get("updated_at", "")[:16].replace("T", " ")

            out_icon, out_col = self._mode_counter(rec.get("out", {}))
            in_icon, in_col = self._mode_counter(rec.get("in", {}))

            # Action buttons
            actions = []
            if out_st == "incomplete":
                actions.append(
                    ft.TextButton(
                        "Resume OUT",
                        icon=ft.icons.PLAY_ARROW,
                        style=ft.ButtonStyle(color="blue"),
                        on_click=lambda e, r=rec: self._history_resume(r, "out"),
                    )
                )
            elif out_st == "not_started":
                actions.append(
                    ft.TextButton(
                        "Start OUT",
                        icon=ft.icons.OUTPUT,
                        style=ft.ButtonStyle(color="blue"),
                        on_click=lambda e, r=rec: self._history_start_out(r),
                    )
                )
            if in_st == "incomplete":
                actions.append(
                    ft.TextButton(
                        "Resume IN",
                        icon=ft.icons.PLAY_ARROW,
                        style=ft.ButtonStyle(color="teal"),
                        on_click=lambda e, r=rec: self._history_resume(r, "in"),
                    )
                )
            elif in_st == "not_started":
                actions.append(
                    ft.TextButton(
                        "Start IN",
                        icon=ft.icons.INPUT,
                        style=ft.ButtonStyle(color="teal"),
                        on_click=lambda e, r=rec: self._history_start_mode(r, "in"),
                    )
                )
            actions.append(
                ft.IconButton(
                    icon=ft.icons.COPY_ALL_OUTLINED,
                    tooltip="Copy list (fresh search)",
                    on_click=lambda e, r=rec: self._history_copy(r),
                )
            )
            actions.append(
                ft.IconButton(
                    icon=ft.icons.DELETE_OUTLINE,
                    tooltip="Delete record",
                    icon_color="red",
                    on_click=lambda e, r=rec: self._history_delete(r),
                )
            )

            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(label, size=12, no_wrap=True, selectable=True)),
                        ft.DataCell(
                            ft.Text(
                                out_icon,
                                size=13,
                                color=out_col,
                                weight=ft.FontWeight.BOLD,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                in_icon,
                                size=13,
                                color=in_col,
                                weight=ft.FontWeight.BOLD,
                            )
                        ),
                        ft.DataCell(ft.Text(updated, size=11, color="grey")),
                        ft.DataCell(ft.Row(controls=actions, spacing=0, tight=True)),
                    ]
                )
            )

        self._history_table.rows = rows
        try:
            # Only update if added to the page
            if getattr(self._history_section, "page", None) is not None:
                self._history_section.update()
            if getattr(self._history_table, "page", None) is not None:
                self._history_table.update()
        except Exception:
            import logging

            logging.exception("Exception updating mode_label:")

    def _history_resume(self, record: Dict, mode: str):
        """Load saved items for *mode* and open the guided modal to resume."""
        mode_data = record.get(mode, {})
        raw_items = mode_data.get("items", [])
        items = []
        for d in raw_items:
            it = PickupItem(
                part_pk=d["part_pk"],
                part_name=d["part_name"],
                location=d["location"],
                quantity=d["quantity"],
                reference=d.get("reference", ""),
            )
            it.scanned = bool(d.get("scanned", False))
            it.checked = bool(d.get("checked", False))
            items.append(it)

        self._current_record_id = record.get("id")
        self._current_query = record.get("query", "")
        self._current_label = record.get("label", self._current_query)
        self._mode = mode
        self.fields["mode_toggle"].value = mode == self.MODE_IN
        self._mode_label.value = "In (Put-Down)" if mode == self.MODE_IN else "Out (Pickup)"
        self._mode_label.color = "green" if mode == self.MODE_IN else "blue"
        self.fields["bom_search"].value = self._current_query
        try:
            self.fields["mode_toggle"].update()
            self._mode_label.update()
            self.fields["bom_search"].update()
        except Exception:
            pass

        self._load_items(items)
        self._set_status(f"Resumed: {self._current_label}", color="grey")
        if items:
            self._open_guided_modal()

    def _items_from_record(self, record: Dict, reset_state: bool = True) -> List[PickupItem]:
        """Extract PickupItems from whichever mode in *record* has items.

        Preference order: out → in (first non-empty list wins).
        If *reset_state* is True, scanned/checked flags are cleared.
        """
        raw: List[Dict] = []
        for mode_key in ("out", "in"):
            candidate = record.get(mode_key, {}).get("items", [])
            if candidate:
                raw = candidate
                break
        items = []
        for d in raw:
            it = PickupItem(
                part_pk=d["part_pk"],
                part_name=d["part_name"],
                location=d["location"],
                quantity=d["quantity"],
                reference=d.get("reference", ""),
            )
            if not reset_state:
                it.scanned = bool(d.get("scanned", False))
                it.checked = bool(d.get("checked", False))
            items.append(it)
        return items

    def _history_start_mode(self, record: Dict, mode: str):
        """Start a fresh *mode* session using saved item list (no server fetch)."""
        items = self._items_from_record(record, reset_state=True)

        self._current_record_id = record.get("id")
        self._current_query = record.get("query", "")
        self._current_label = record.get("label", self._current_query)
        self._mode = mode
        self.fields["mode_toggle"].value = mode == self.MODE_IN
        self._mode_label.value = "In (Put-Down)" if mode == self.MODE_IN else "Out (Pickup)"
        self._mode_label.color = "green" if mode == self.MODE_IN else "blue"
        self.fields["bom_search"].value = self._current_query
        try:
            self.fields["mode_toggle"].update()
            self._mode_label.update()
            self.fields["bom_search"].update()
        except Exception:
            pass

        self._load_items(items)
        verb = "IN" if mode == self.MODE_IN else "OUT"
        self._set_status(f"Starting {verb} for: {self._current_label}", color="grey")
        if items:
            self._open_guided_modal()

    def _history_start_out(self, record: Dict):
        """Start a fresh OUT session using saved item list (no server fetch)."""
        self._history_start_mode(record, self.MODE_OUT)

    def _history_copy(self, record: Dict):
        """Duplicate the saved item list into a brand-new operation (no server fetch)."""
        items = self._items_from_record(record, reset_state=True)
        if not items:
            return
        self._current_record_id = None
        self._current_query = record.get("query", "")
        self._current_label = record.get("label", self._current_query)
        self._mode = self.MODE_OUT
        self.fields["mode_toggle"].value = False
        self._mode_label.value = "Out (Pickup)"
        self._mode_label.color = "blue"
        self.fields["bom_search"].value = self._current_query
        try:
            self.fields["mode_toggle"].update()
            self._mode_label.update()
            self.fields["bom_search"].update()
        except Exception:
            pass
        self._load_items(items)
        self._set_status(f"Copied: {self._current_label}", color="grey")
        self._open_guided_modal()

    def _history_delete(self, record: Dict):
        """Delete a history record and refresh the table."""
        pickup_history.delete_record(record.get("id", ""))
        self._rebuild_history_table()

    def _set_status(self, msg: str, color: str = "grey"):
        self._status_text.value = msg
        self._status_text.color = color
        try:
            self._status_text.update()
        except Exception:
            pass
