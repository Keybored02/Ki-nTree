"""Inventory pickup / put-down view for Ki-nTree."""

import threading
from collections import defaultdict
from typing import Dict, List, Optional

import flet as ft
import requests

from ...database import pickup_api
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

    def __init__(self, part_pk: int, part_name: str, location: str,
                 quantity: float = 1, reference: str = ''):
        self.part_pk = part_pk
        self.part_name = part_name
        self.location = location   # single location string for this row
        self.quantity = quantity
        self.reference = reference
        self.checked = False       # manually ticked
        self.scanned = False       # confirmed via barcode scan


# ---------------------------------------------------------------------------
# Guided-mode modal
# ---------------------------------------------------------------------------

class GuidedPickupModal:
    """
    Two-page modal for guided OUT (pickup) flow.

    Page 0 — location overview:
        • Table grouped by location (location column first).
        • Scanner input resolves InvenTree location barcodes.
        • Scanning a location navigates to page 1.

    Page 1 — part scanning for one location:
        • Shows only the parts belonging to the scanned location.
        • Scanner input resolves part barcodes via _find_part_by_lookup.
        • Matched parts turn green; once all are scanned, auto-returns to page 0.
        • Back arrow and manual tick also available.
    """

    def __init__(self, page: ft.Page, items: List[PickupItem],
                 find_part_fn, on_close_fn):
        """
        Parameters
        ----------
        page            Flet page reference.
        items           Full list of PickupItems from the BOM resolve.
        find_part_fn    Callable(lookup_value) -> Optional[dict]  (from BarcodeApiMixin)
        on_close_fn     Called when the modal closes; receives list of PickupItems.
        """
        self._page = page
        self._items = items
        self._find_part = find_part_fn
        self._on_close = on_close_fn
        self._current_location: Optional[str] = None
        self._parent_filter: Optional[str] = None   # set when a parent location was scanned
        self._scan_lock = threading.Lock()

        # Build controls
        self._scanner_input = ft.TextField(
            hint_text='Scan location or part barcode…',
            prefix_icon=ft.icons.QR_CODE_SCANNER,
            dense=True,
            autofocus=True,
            on_submit=self._on_scan_submit,
            on_change=self._on_scan_input_changed,
        )
        self._status_text = ft.Text('', size=12, italic=True, color='grey')

        # Page 0 — location overview table
        self._location_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Part Name')),
                ft.DataColumn(ft.Text('Qty')),
                ft.DataColumn(ft.Text('Done')),
            ],
            rows=[],
            column_spacing=14,
            horizontal_margin=8,
            show_bottom_border=True,
        )
        # Header row for page 0 — swaps between hint text and parent-filter banner
        self._page0_back_btn = ft.IconButton(
            icon=ft.icons.ARROW_BACK,
            tooltip='Show all locations',
            on_click=lambda e: self._go_page0(),
            visible=False,
        )
        self._page0_header = ft.Text(
            'Scan a location barcode to begin',
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
            '',
            style=ft.TextThemeStyle.HEADLINE_SMALL,
            text_align=ft.TextAlign.CENTER,
            weight=ft.FontWeight.BOLD,
        )
        self._part_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('Part Name')),
                ft.DataColumn(ft.Text('Qty')),
                ft.DataColumn(ft.Text('✓')),
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
                            tooltip='Back to locations',
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

        # Close button
        self._close_btn = ft.TextButton(
            'Close',
            icon=ft.icons.CLOSE,
            on_click=self._on_close_click,
        )

        total = len(self._items)
        self._counter_text = ft.Text(
            f'0 / {total}',
            size=13,
            weight=ft.FontWeight.BOLD,
            color='grey',
        )

        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                controls=[
                    ft.Text('Guided Pickup — OUT', expand=True),
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
        self._parent_filter = None
        self._page0_content.visible = True
        self._page1_content.visible = False
        self._scanner_input.hint_text = 'Scan location or part barcode…'
        self._page0_header.value = 'Scan a location barcode to begin'
        self._page0_header.style = ft.TextThemeStyle.TITLE_MEDIUM
        self._page0_header.weight = ft.FontWeight.NORMAL
        self._page0_header.text_align = ft.TextAlign.LEFT
        self._page0_back_btn.visible = False
        self._rebuild_location_table()
        self._set_status('', color='grey')
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._scanner_input.update()
        except Exception:
            pass

    def _go_page_parent(self, parent_name: str):
        """Filter page 0 to show only child locations of *parent_name*."""
        self._parent_filter = parent_name
        self._current_location = None
        self._page0_content.visible = True
        self._page1_content.visible = False
        self._scanner_input.hint_text = 'Scan a child location barcode…'
        self._page0_header.value = parent_name
        self._page0_header.style = ft.TextThemeStyle.HEADLINE_SMALL
        self._page0_header.weight = ft.FontWeight.BOLD
        self._page0_header.text_align = ft.TextAlign.CENTER
        self._page0_back_btn.visible = True
        self._rebuild_location_table()
        self._set_status('Scan or tap a child location.', color='grey')
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._scanner_input.update()
        except Exception:
            pass

    def _go_page1(self, location: str):
        self._current_location = location
        # Show only the leaf segment (last part after the final '/')
        self._location_header.value = location.rsplit('/', 1)[-1]
        self._page0_content.visible = False
        self._page1_content.visible = True
        self._scanner_input.hint_text = 'Scan a part barcode…'
        self._rebuild_part_table()
        self._set_status('Scan parts from this location.', color='grey')
        self._focus_input()
        try:
            self._page0_content.update()
            self._page1_content.update()
            self._location_header.update()
            self._scanner_input.update()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Scanner input                                                       #
    # ------------------------------------------------------------------ #

    def _on_scan_input_changed(self, e):
        """Handle scanners that embed newlines inside the barcode payload."""
        text = e.control.value or ''
        normalized = text.replace('\r', '\n')
        if '\n' not in normalized:
            return
        # Split into completed lines; keep any trailing fragment in the field.
        has_trailing = normalized.endswith('\n')
        raw_lines = normalized.split('\n')
        completed = raw_lines if has_trailing else raw_lines[:-1]
        pending = '' if has_trailing else (raw_lines[-1] if raw_lines else '')
        lines = [l.strip() for l in completed if l.strip()]
        e.control.value = pending
        try:
            e.control.update()
        except Exception:
            pass
        for line in lines:
            self._dispatch_scan(line)

    def _on_scan_submit(self, e):
        raw = (e.control.value or '').strip()
        e.control.value = ''
        try:
            e.control.update()
        except Exception:
            pass
        if not raw:
            return
        self._dispatch_scan(raw)

    def _dispatch_scan(self, raw: str):
        """Route a completed scan string to location or part resolution."""
        if self._current_location is None:
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
            self._set_status('Not connected to InvenTree.', color='red')
            return

        location_name = None
        sublocations = 0

        # POST /api/barcode/ — InvenTree's universal barcode resolver
        try:
            result = api.post('barcode/', data={'barcode': raw})
            if isinstance(result, dict):
                loc = result.get('stocklocation')
                if loc:
                    loc_pk = loc.get('pk') or loc.get('id')
                    if loc_pk:
                        loc_obj = api.get(f'stock/location/{loc_pk}/')
                        if isinstance(loc_obj, dict):
                            location_name = (
                                loc_obj.get('pathstring')
                                or loc_obj.get('name')
                                or str(loc_pk)
                            )
                            sublocations = int(loc_obj.get('sublocations') or 0)
        except Exception:
            pass

        # Fallback: match raw text against known location strings (exact or suffix)
        if not location_name:
            known = {item.location for item in self._items if item.location and item.location != '(no location)'}
            # Also collect all unique path prefixes as potential parent names
            all_prefixes: set = set()
            for loc in known:
                parts = loc.split('/')
                for i in range(1, len(parts)):
                    all_prefixes.add('/'.join(parts[:i]))
            raw_lower = raw.strip().lower()
            for candidate in known | all_prefixes:
                if candidate.lower() == raw_lower or candidate.lower().endswith('/' + raw_lower):
                    location_name = candidate
                    break

        if not location_name:
            self._set_status(f'Location not recognised: {raw}', color='orange')
            self._focus_input()
            return

        # Check if this is a parent location: has children that contain BOM items
        child_items = [
            it for it in self._items
            if it.location.startswith(location_name + '/') and it.location != location_name
        ]
        direct_items = [it for it in self._items if it.location == location_name]

        if (sublocations > 0 or child_items) and not direct_items:
            # Pure parent — filter to its children
            if not child_items:
                self._set_status(f'No BOM items under: {location_name}', color='orange')
                self._focus_input()
                return
            self._go_page_parent(location_name)
        elif direct_items:
            # Leaf or mixed — go straight to part scanning
            self._go_page1(location_name)
        else:
            self._set_status(f'No BOM items for location: {location_name}', color='orange')
            self._focus_input()

    def _resolve_part_scan(self, raw: str):
        """Try to match *raw* against BOM items for the current location.

        The raw scan is first decoded by BarcodeParser so that manufacturer
        2D barcodes (DigiKey, Mouser, LCSC, TME) are understood; the
        manufacturer PN or supplier PN extracted from the parsed result is
        used as the lookup value fed to InvenTree.
        """
        loc_items = [
            it for it in self._items
            if it.location == self._current_location
        ]

        matched: Optional[PickupItem] = None

        # Decode the raw scan with BarcodeParser — same logic as barcode page
        parsed = _parser.parse(raw)
        supplier = parsed.get('supplier', 'unknown')
        if supplier == 'unknown':
            # Unknown format: treat raw value as-is (InvenTree internal barcode,
            # plain IPN, part name, etc.)
            lookup_candidates = [raw.strip()]
        else:
            mpn = str(parsed.get('manufacturer_pn') or '').strip()
            spn = str(parsed.get('supplier_pn') or '').strip()
            # Prefer MPN then SPN; fall back to raw if both are empty
            lookup_candidates = [v for v in [mpn, spn] if v] or [raw.strip()]

        # Try InvenTree lookup for each candidate
        for candidate in lookup_candidates:
            result = self._find_part(candidate)
            if result:
                found_pk = int(result.get('pk') or result.get('id') or 0)
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
            self._set_status(f'Part not found in this location: {raw}', color='orange')
            self._focus_input()
            return

        with self._scan_lock:
            matched.scanned = True

        self._set_status(f'✓ {matched.part_name}', color='green')
        self._refresh_counter()
        self._rebuild_part_table()

        # All done for this location? Auto-return to page 0.
        remaining = [it for it in loc_items if not it.scanned and not it.checked]
        if not remaining:
            self._set_status(f'All parts picked for {self._current_location}!', color='green')
            try:
                self._status_text.update()
            except Exception:
                pass
            import time; time.sleep(1.2)
            self._go_page0()
        else:
            self._focus_input()

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
                if not (item.location == self._parent_filter
                        or item.location.startswith(self._parent_filter + '/')):
                    continue
            by_loc[item.location].append(item)

        rows = []
        for loc in sorted(by_loc.keys(), key=lambda l: (l == '(no location)', l.lower())):
            loc_items = by_loc[loc]
            done_count = sum(1 for it in loc_items if it.scanned or it.checked)
            all_done = done_count == len(loc_items)

            for i, item in enumerate(loc_items):
                done = item.scanned or item.checked
                qty_str = str(int(item.quantity)) if item.quantity == int(item.quantity) else str(item.quantity)

                # Show location only on the first row of each group; make it clickable
                if i == 0:
                    loc_cell = ft.DataCell(
                        ft.TextButton(
                            text=loc,
                            style=ft.ButtonStyle(
                                color='green' if all_done else ft.colors.PRIMARY,
                                padding=ft.padding.all(0),
                            ),
                            on_click=lambda e, l=loc: self._go_page1(l),
                        )
                    )
                else:
                    loc_cell = ft.DataCell(ft.Text('', size=12))

                name_cell = ft.DataCell(
                    ft.Text(
                        item.part_name, size=12, no_wrap=True,
                        color='green' if done else None,
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
        try:
            self._location_table.update()
        except Exception:
            pass

    def _rebuild_part_table(self):
        """Rebuild page-1 table for the current location."""
        loc_items = [it for it in self._items if it.location == self._current_location]
        rows = []
        for item in loc_items:
            done = item.scanned or item.checked
            qty_str = str(int(item.quantity)) if item.quantity == int(item.quantity) else str(item.quantity)
            rows.append(ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(
                        item.part_name, size=12, no_wrap=True,
                        color='green' if done else None,
                        weight=ft.FontWeight.BOLD if done else ft.FontWeight.NORMAL,
                    )),
                    ft.DataCell(ft.Text(qty_str, size=12)),
                    ft.DataCell(ft.Checkbox(
                        value=done,
                        on_change=lambda e, it=item: self._on_manual_tick(e, it),
                    )),
                ],
                color=ft.colors.GREEN_50 if done else None,
            ))
        self._part_table.rows = rows
        try:
            self._part_table.update()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Manual tick                                                         #
    # ------------------------------------------------------------------ #

    def _on_manual_tick(self, e, item: PickupItem):
        with self._scan_lock:
            item.checked = bool(e.control.value)
            item.scanned = item.checked

        self._refresh_counter()

        # Refresh whichever page is visible
        if self._current_location is not None:
            self._rebuild_part_table()
            loc_items = [it for it in self._items if it.location == self._current_location]
            remaining = [it for it in loc_items if not it.scanned and not it.checked]
            if not remaining:
                import time; time.sleep(0.6)
                self._go_page0()
        else:
            self._rebuild_location_table()

        self._focus_input()

    # ------------------------------------------------------------------ #
    #  Close                                                               #
    # ------------------------------------------------------------------ #

    def _on_close_click(self, e):
        incomplete = [it for it in self._items if not it.scanned and not it.checked]
        if incomplete:
            # Swap close button to a confirm-anyway button
            self._close_btn.text = f'Close anyway ({len(incomplete)} item(s) remaining)'
            self._close_btn.icon = ft.icons.WARNING_AMBER_ROUNDED
            self._close_btn.icon_color = 'orange'
            self._status_text.value = f'Warning: {len(incomplete)} item(s) not yet picked.'
            self._status_text.color = 'orange'
            self._close_btn.on_click = self._force_close
            try:
                self._close_btn.update()
                self._status_text.update()
            except Exception:
                pass
            return
        self._force_close(e)

    def _force_close(self, e):
        self._page.close(self._dialog)
        self._on_close(self._items)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _refresh_counter(self):
        done = sum(1 for it in self._items if it.scanned or it.checked)
        total = len(self._items)
        self._counter_text.value = f'{done} / {total}'
        self._counter_text.color = 'green' if done == total else 'grey'
        try:
            self._counter_text.update()
        except Exception:
            pass

    def _set_status(self, msg: str, color: str = 'grey'):
        self._status_text.value = msg
        self._status_text.color = color
        try:
            self._status_text.update()
        except Exception:
            pass

    def _focus_input(self):
        import time
        for _ in range(2):
            try:
                self._scanner_input.focus()
                self._scanner_input.update()
                return
            except Exception:
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------

class PickupView(MainView):
    """Inventory pickup (Out) and put-down (In) view."""

    title = 'Pickup'
    fields: Dict = {}

    MODE_OUT = 'out'
    MODE_IN = 'in'

    def __init__(self, page: ft.Page):
        self._mode = self.MODE_OUT
        self._items: List[PickupItem] = []
        self._search_thread: Optional[threading.Thread] = None
        self._guided_modal: Optional[GuidedPickupModal] = None

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
        self.fields['bom_search'] = ft.TextField(
            label='Search Assembly / BOM',
            hint_text='Part name  or  BO-123',
            width=GUI_PARAMS['textfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            prefix_icon=ft.icons.SEARCH,
            on_submit=self._on_search,
        )
        self.fields['search_btn'] = ft.ElevatedButton(
            text='Load',
            icon=ft.icons.DOWNLOAD_OUTLINED,
            on_click=self._on_search,
        )

        # Mode toggle
        self._mode_label = ft.Text(
            'Out (Pickup)',
            size=14,
            weight=ft.FontWeight.BOLD,
            color='blue',
        )
        self.fields['mode_toggle'] = ft.Switch(
            label='',
            value=False,
            on_change=self._on_mode_change,
        )

        # Guided mode checkbox
        self.fields['guided_mode'] = ft.Checkbox(
            label='Guided mode',
            value=True,
            tooltip='Step through locations one at a time with barcode scanner',
        )

        self._status_text = ft.Text('', size=13, color='grey', italic=True)
        self._progress = ft.ProgressBar(visible=False, width=GUI_PARAMS['textfield_width'])

        # Results table
        self._table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('', width=32)),
                ft.DataColumn(ft.Text('Part Name')),
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Qty')),
            ],
            rows=[],
            column_spacing=16,
            horizontal_margin=8,
            show_bottom_border=True,
            expand=True,
        )
        self._table_scroll = ft.ListView(
            controls=[self._table],
            expand=True,
            spacing=0,
        )

        self.fields['clear_btn'] = ft.OutlinedButton(
            text='Clear',
            icon=ft.icons.CLEAR_ALL,
            on_click=self._on_clear,
        )
        self.fields['confirm_btn'] = ft.ElevatedButton(
            text='Confirm',
            icon=ft.icons.CHECK_CIRCLE_OUTLINE,
            bgcolor='green',
            color='white',
            disabled=True,
            on_click=self._on_confirm,
        )

        search_row = ft.Row(
            controls=[
                self.fields['bom_search'],
                ft.Container(width=8),
                self.fields['search_btn'],
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        mode_row = ft.Row(
            controls=[
                ft.Text('Mode:', size=14),
                ft.Container(width=4),
                ft.Text('Out', size=13),
                self.fields['mode_toggle'],
                ft.Text('In', size=13),
                ft.Container(width=16),
                self._mode_label,
                ft.Container(width=24),
                self.fields['guided_mode'],
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        action_row = ft.Row(
            controls=[
                self.fields['clear_btn'],
                ft.Container(width=8),
                self.fields['confirm_btn'],
                ft.Container(expand=True),
                self._status_text,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text('Inventory Pickup / Put-Down',
                                    style=ft.TextThemeStyle.HEADLINE_SMALL),
                            ft.Divider(height=8),
                            search_row,
                            self._progress,
                            ft.Divider(height=4),
                            mode_row,
                            ft.Divider(height=4),
                            action_row,
                            ft.Divider(height=2),
                            self._table_scroll,
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

    # ------------------------------------------------------------------ #
    #  Event handlers                                                      #
    # ------------------------------------------------------------------ #

    def _on_search(self, e):
        query = (self.fields['bom_search'].value or '').strip()
        if not query:
            self._set_status('Enter a part name or build order (BO-xxx).', color='orange')
            return

        self.fields['search_btn'].disabled = True
        self.fields['bom_search'].disabled = True
        self._progress.visible = True
        self._set_status(f'Loading BOM for "{query}"…', color='grey')
        try:
            self.fields['search_btn'].update()
            self.fields['bom_search'].update()
            self._progress.update()
        except Exception:
            pass

        def _run():
            ok, message, raw_items = pickup_api.resolve_query(query)
            # Expand each BOM entry into one PickupItem per distinct location
            items = []
            for r in raw_items:
                for loc in r.get('locations', ['(no location)']):
                    items.append(PickupItem(
                        part_pk=r['part_pk'],
                        part_name=r['part_name'],
                        location=loc,
                        quantity=r['quantity'],
                        reference=r.get('reference', ''),
                    ))
            self.fields['search_btn'].disabled = False
            self.fields['bom_search'].disabled = False
            self._progress.visible = False
            try:
                self.fields['search_btn'].update()
                self.fields['bom_search'].update()
                self._progress.update()
            except Exception:
                pass

            self._load_items(items)
            self._set_status(message, color='green' if ok else 'red')

            # Auto-open guided modal if enabled and load succeeded
            if ok and items and self.fields['guided_mode'].value and self._mode == self.MODE_OUT:
                self._open_guided_modal()

        self._search_thread = threading.Thread(target=_run, daemon=True)
        self._search_thread.start()

    def _on_mode_change(self, e):
        if e.control.value:
            self._mode = self.MODE_IN
            self._mode_label.value = 'In (Put-Down)'
            self._mode_label.color = 'green'
        else:
            self._mode = self.MODE_OUT
            self._mode_label.value = 'Out (Pickup)'
            self._mode_label.color = 'blue'
        self._mode_label.update()
        self._rebuild_table()

    def _on_clear(self, e):
        self._items.clear()
        self.fields['bom_search'].value = ''
        self.fields['confirm_btn'].disabled = True
        self._set_status('', color='grey')
        self._rebuild_table()
        try:
            self.fields['bom_search'].update()
            self.fields['confirm_btn'].update()
        except Exception:
            pass

    def _on_confirm(self, e):
        if self.fields['guided_mode'].value and self._mode == self.MODE_OUT:
            self._open_guided_modal()
            return
        checked = [it for it in self._items if it.checked]
        mode_label = 'picked up' if self._mode == self.MODE_OUT else 'put down'
        self._set_status(f'{len(checked)} item(s) marked as {mode_label}.', color='green')

    def _on_row_check(self, e, item: PickupItem):
        item.checked = bool(e.control.value)
        any_checked = any(it.checked for it in self._items)
        self.fields['confirm_btn'].disabled = not any_checked
        try:
            self.fields['confirm_btn'].update()
        except Exception:
            pass

    def _on_select_all(self, e):
        checked = bool(e.control.value)
        for item in self._items:
            item.checked = checked
        self.fields['confirm_btn'].disabled = not (checked and bool(self._items))
        try:
            self.fields['confirm_btn'].update()
        except Exception:
            pass
        self._rebuild_table()

    # ------------------------------------------------------------------ #
    #  Guided modal                                                        #
    # ------------------------------------------------------------------ #

    def _open_guided_modal(self):
        self._guided_modal = GuidedPickupModal(
            page=self._page,
            items=self._items,
            find_part_fn=self._find_part_by_lookup_simple,
            on_close_fn=self._on_guided_close,
        )
        self._guided_modal.open()

    def _on_guided_close(self, items: List[PickupItem]):
        """Called when the modal closes; sync checked/scanned state back to main table."""
        self._items = items
        self._rebuild_table()
        done = sum(1 for it in items if it.scanned or it.checked)
        self._set_status(
            f'Guided session complete: {done}/{len(items)} item(s) picked.',
            color='green' if done == len(items) else 'orange',
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
        self._rebuild_table()
        self.fields['confirm_btn'].disabled = not bool(items)
        try:
            self.fields['confirm_btn'].update()
        except Exception:
            pass

    def _rebuild_table(self):
        mode_verb = 'Pickup' if self._mode == self.MODE_OUT else 'Put-Down'
        self._table.columns[0].label = ft.Checkbox(
            value=False,
            on_change=self._on_select_all,
            tooltip=f'Select all for {mode_verb}',
        )
        rows = []
        for item in self._items:
            done = item.scanned or item.checked
            qty_str = str(int(item.quantity)) if item.quantity == int(item.quantity) else str(item.quantity)
            loc_color = 'grey' if item.location == '(no location)' else None
            name_color = 'green' if done else None
            row = ft.DataRow(
                cells=[
                    ft.DataCell(ft.Checkbox(
                        value=item.checked,
                        on_change=lambda e, it=item: self._on_row_check(e, it),
                    )),
                    ft.DataCell(ft.Text(item.part_name, size=12, no_wrap=True, color=name_color)),
                    ft.DataCell(ft.Text(item.location, size=12, italic=True, color=loc_color)),
                    ft.DataCell(ft.Text(qty_str, size=12)),
                ],
                color=ft.colors.GREEN_50 if done else None,
            )
            rows.append(row)
        self._table.rows = rows
        try:
            self._table.update()
        except Exception:
            pass

    def _set_status(self, msg: str, color: str = 'grey'):
        self._status_text.value = msg
        self._status_text.color = color
        try:
            self._status_text.update()
        except Exception:
            pass
