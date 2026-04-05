"""Barcode scanner GUI view for rapid part import into InvenTree.

This module provides the main GUI interface for barcode scanning and bulk part
import. Users can scan multiple barcodes, configure import settings (category,
location, stock creation), and import all items to InvenTree in one operation.

Supports: TME (key-value), Mouser (GS1-128), Digi-Key (GS1-128) barcodes.
"""

import flet as ft
import threading
import requests
import re
import time
from typing import Dict, List, Optional

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
        supplier: Detected supplier ('tme', 'mouser', 'digikey')
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
        if self.supplier == 'mouser':
            self.search_name = self.manufacturer_pn or self.supplier_pn
        else:
            self.search_name = self.supplier_pn or self.manufacturer_pn
        
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
            on_click=lambda _: setattr(self.fields['barcode_input'], 'value', '') or self.fields['barcode_input'].update(),
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
                ft.DataColumn(ft.Text('Supplier')),
                ft.DataColumn(ft.Text('Supplier PN')),
                ft.DataColumn(ft.Text('MFN PN')),
                ft.DataColumn(ft.Text('Barcode')),
                ft.DataColumn(ft.Text('Qty')),
                ft.DataColumn(ft.Text('Category')),
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Create Stock')),
                ft.DataColumn(ft.Text('Remove')),
            ],
            rows=[],
            horizontal_lines=ft.border.BorderSide(1, ft.colors.OUTLINE),
        )
        
        # Category search/dropdown control (scan-friendly)
        self.fields['category_select'] = DropdownWithSearch(
            label='Category (All Items)',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_category_changed,
        )
        
        # Location search/dropdown control (scan-friendly)
        self.fields['location_select'] = DropdownWithSearch(
            label='Stock Location (All Items)',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_location_changed,
        )

        self.fields['reload_categories'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload categories from InvenTree',
            on_click=self._reload_categories,
        )

        self.fields['reload_locations'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload stock locations from InvenTree',
            on_click=self._reload_locations,
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
            on_change=lambda _: self._update_results_table(),
        )
        
        # Submit button
        self.fields['barcode_submit'] = ft.ElevatedButton(
            text='Import All',
            on_click=self._on_submit,
            color='white',
            bgcolor='green',
            width=200,
        )
        
        # Status message
        self.fields['status_message'] = ft.Text(value='Ready to scan', size=12, color='blue')
        self.fields['import_progress'] = ft.ProgressBar(value=0, visible=False, height=8)
        self.fields['import_progress_message'] = ft.Text(value='', size=11, color='blue')
        
        # Build layout - set self.column instead of self.page.content
        self.column = ft.Column(
            controls=[
                ft.Container(
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
                                    ]),
                                    ft.Row([
                                        self.fields['location_select'],
                                        self.fields['reload_locations'],
                                    ]),
                                ],
                                spacing=8,
                            ),
                            self.fields['create_stock_check'],
                            self.fields['use_manufacturer_barcode_check'],
                            
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
            ],
            expand=True,
        )
        
        self.focus_barcode_input()

    def did_mount(self):
        self._load_categories_and_locations()
        self.focus_barcode_input()
        return super().did_mount()

    def focus_barcode_input(self):
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
            category_list = inventree_interface.build_category_tree(reload=False)

            location_list = inventree_interface.build_stock_location_tree(reload=False)

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

            location_list = inventree_interface.build_stock_location_tree(reload=True)
            self.fields['location_select'].options = [ft.dropdown.Option(location) for location in location_list]
            self._page.update()
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

        if '\n' not in text and '\r' not in text:
            return

        lines = [line.strip() for line in text.replace('\r', '\n').split('\n') if line.strip()]
        if not lines:
            self.fields['barcode_input'].value = ''
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
        self.fields['barcode_input'].value = ''
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
        if recent_ts is not None and (now - recent_ts) < 0.5:
            cprint(f'[BARCODE]\tIgnored duplicate scan: {normalized}', silent=False)
            return False

        self._recent_scan_codes[normalized] = now
        self._last_scan_code = normalized
        self._last_scan_ts = now

        parsed = self.parser.parse(normalized)
        if parsed.get('supplier') == 'unknown':
            if update_status:
                self._show_status('Unknown barcode format', color='red')
                self.show_dialog(DialogType.ERROR, 'Unrecognized barcode format')
            return False

        row = BarcodeScannedRow(normalized, parsed)
        self.scanned_rows.append(row)
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
    
    def _update_results_table(self):
        """Refresh the results table with current scanned items."""
        rows = []
        use_manufacturer_barcode = self.fields.get('use_manufacturer_barcode_check', None)
        show_barcode = bool(use_manufacturer_barcode.value) if use_manufacturer_barcode else True

        for idx, row in enumerate(self.scanned_rows):
            remove_btn = ft.IconButton(
                icon=ft.icons.DELETE,
                on_click=lambda _, i=idx: self._remove_row(i),
            )

            rows.append(ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(row.supplier.upper(), size=13, no_wrap=True)),
                    ft.DataCell(
                        ft.Text(
                            row.supplier_pn or '-',
                            selectable=True,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            size=12,
                        )
                    ),
                    ft.DataCell(
                        ft.Text(
                            row.manufacturer_pn or '-',
                            selectable=True,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            size=12,
                        )
                    ),
                    ft.DataCell(
                        ft.Text(
                            row.barcode if show_barcode else '',
                            selectable=True,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            size=11,
                            color='gray',
                        )
                    ),
                    ft.DataCell(ft.Text(str(row.quantity), size=13, no_wrap=True)),
                    ft.DataCell(ft.Text(row.category or '(default)', size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Text(row.location or '(default)', size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.DataCell(ft.Checkbox(
                        value=row.create_stock,
                        on_change=lambda _, i=idx: self._toggle_create_stock(i),
                    )),
                    ft.DataCell(remove_btn),
                ],
            ))

        self.fields['results_table'].rows = rows
        self._page.update()
    
    def _remove_row(self, idx: int):
        """Remove a scanned row."""
        if 0 <= idx < len(self.scanned_rows):
            self.scanned_rows.pop(idx)
            self._update_results_table()

    def _clear_all_rows(self, _):
        """Clear all scanned rows."""
        if not self.scanned_rows:
            return
        self.scanned_rows.clear()
        self._recent_scan_codes.clear()
        self._update_results_table()
        self._show_status('Cleared all scanned items', color='blue')
    
    def _on_category_changed(self, *args, **kwargs):
        """Apply selected category to all items."""
        category = self.fields['category_select'].value
        if category:
            for row in self.scanned_rows:
                row.category = category
            self._update_results_table()
    
    def _on_location_changed(self, *args, **kwargs):
        """Apply selected location to all items."""
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
                row.location = location
            self._update_results_table()
    
    def _on_create_stock_changed(self, _):
        """Apply create_stock flag to all items."""
        create_stock = self.fields['create_stock_check'].value
        for row in self.scanned_rows:
            row.create_stock = create_stock
        self._update_results_table()
    
    def _toggle_create_stock(self, idx: int):
        """Toggle create_stock for a specific row."""
        if 0 <= idx < len(self.scanned_rows):
            self.scanned_rows[idx].create_stock = not self.scanned_rows[idx].create_stock
            self._update_results_table()
    
    def _show_status(self, message: str, color: str = 'black'):
        """Show status message."""
        self.fields['status_message'].value = message
        self.fields['status_message'].color = color
        try:
            self.fields['status_message'].update()
        except AssertionError:
            pass

    def _normalize_stock_location_value(self, location: str) -> str:
        return '/'.join(
            part.strip()
            for part in re.sub(r'^-+\s+', '', str(location or '').strip()).split('/')
            if part.strip()
        )

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

            if not row.category:
                self.show_dialog(DialogType.ERROR, 'Please select a category for all items')
                return

            if row.create_stock and not row.location:
                self.show_dialog(DialogType.ERROR, 'Please select a location for stock items')
                return

        # Show confirmation dialog with summary
        summary = f'Ready to import {len(self.scanned_rows)} items:\n\n'
        for row in self.scanned_rows[:7]:
            summary += f'  • {row.supplier.upper()}: {row.search_name} (qty {row.quantity})\n'
        if len(self.scanned_rows) > 7:
            summary += f'  ... and {len(self.scanned_rows) - 7} more\n'
        summary += '\nProceed?'

        def confirm_import(_):
            self._page.dialog.open = False
            self._page.update()
            self._show_status('Import running...', color='blue')
            self._execute_import()

        dlg = ft.AlertDialog(
            title=ft.Text('Confirm Import'),
            content=ft.Text(summary),
            actions=[
                ft.TextButton('Cancel', on_click=lambda _: setattr(self._page.dialog, 'open', False) or self._page.update()),
                ft.TextButton('Import', on_click=confirm_import),
            ],
        )
        self._page.dialog = dlg
        self._page.dialog.open = True
        self._page.update()
    
    def _execute_import(self):
        """Execute the import process."""
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
        
        for idx, row in enumerate(self.scanned_rows):
            item_label = f'{row.supplier.upper()} - {row.search_name}'
            self._show_status(f'Importing {idx + 1}/{total}: {item_label}', color='blue')
            self.fields['import_progress'].value = idx / total if total else 1.0
            self.fields['import_progress_message'].value = f'Importing {idx + 1}/{total}: {item_label}'
            self.fields['import_progress'].update()
            self.fields['import_progress_message'].update()

            try:
                # Search for part using parsed data
                supplier_name = self._resolve_supplier_key(row.supplier)
                supplier_data = inventree_interface.supplier_search(
                    supplier=supplier_name,
                    part_number=row.search_name,
                )
                
                if not supplier_data:
                    # Part not found in supplier, create with barcode data only
                    part_form = {
                        'name': row.search_name,
                        'description': f'Imported from {row.supplier.upper()}',
                        'category_tree': [row.category],
                    }
                else:
                    part_form = inventree_interface.translate_supplier_to_form(
                        supplier=supplier_name,
                        part_info=supplier_data,
                    )
                    part_form['name'] = row.search_name
                    part_form['category_tree'] = [row.category]
                
                # Prepare stock payload
                stock_payload = None
                if row.create_stock:
                    location_pk = self._get_stock_location_pk(row.location)
                    if location_pk <= 0:
                        failed += 1
                        failures.append(f'{row.search_name}: Location not found')
                        continue
                    
                    stock_payload = {
                        'location': location_pk,
                        'quantity': row.quantity or 1,
                        'make_default': False,
                    }
                
                # Create part
                new_part, part_pk, _ = inventree_interface.inventree_create(
                    part_info=part_form,
                    kicad=False,
                    show_progress=False,
                    is_custom=False,
                    stock=None,
                )
                
                if part_pk:
                    # Always handle stock here so existing parts are not skipped.
                    if stock_payload is not None:
                        stock_data = dict(stock_payload)
                        stock_data['part'] = part_pk
                        stock_result = inventree_interface.inventree_api.create_stock(stock_data)
                        if not stock_result:
                            failed += 1
                            failures.append(f'{row.search_name}: Failed to create stock')
                            cprint(f'[BARCODE]\tFailed to add stock for {item_label}', silent=False)
                            continue

                    # Add barcode if available
                    use_manufacturer_barcode = self.fields['use_manufacturer_barcode_check'].value
                    barcode_value = row.barcode if use_manufacturer_barcode else ''

                    if barcode_value:
                        try:
                            inventree_interface.inventree_api.link_barcode(barcode_value, part_pk=part_pk)
                        except Exception as e:
                            # Log but don't fail - barcode linking is optional
                            cprint(f'[WARN]\tBarcode linking failed for {row.search_name}: {str(e)}', silent=False)
                    
                    success += 1
                    cprint(f'[BARCODE]\tImported {item_label} (part_pk={part_pk})', silent=False)
                else:
                    failed += 1
                    failures.append(f'{row.search_name}: Failed to create')
                    cprint(f'[BARCODE]\tFailed to import {item_label}', silent=False)
            
            except Exception as e:
                failed += 1
                failures.append(f'{row.search_name}: {str(e)[:50]}')
                cprint(f'[BARCODE]\tException importing {item_label}: {str(e)[:120]}', silent=False)

            self.fields['import_progress'].value = (idx + 1) / total if total else 1.0
            self.fields['import_progress'].update()
            self.fields['import_progress_message'].value = f'Processed {idx + 1}/{total}'
            self.fields['import_progress_message'].update()
        
        # Show summary
        summary = f'Import complete:\n  ✓ Success: {success}\n  ✗ Failed: {failed}'
        if failures:
            summary += '\n\nFailures:'
            for fail in failures[:5]:
                summary += f'\n  • {fail}'
            if len(failures) > 5:
                summary += f'\n  ... and {len(failures) - 5} more'
        
        if failed == 0:
            dlg_type = DialogType.VALID
            # Clear and reset
            self.scanned_rows.clear()
            self._update_results_table()
        else:
            dlg_type = DialogType.WARNING

        self.fields['import_progress'].value = 1.0
        self.fields['import_progress'].color = 'green' if failed == 0 else ('amber' if success > 0 else 'red')
        self.fields['import_progress_message'].value = f'Import finished: {success} success, {failed} failed'
        self.fields['import_progress_message'].color = 'green' if failed == 0 else 'orange'
        self.fields['import_progress'].update()
        self.fields['import_progress_message'].update()

        cprint(f'[BARCODE]\tImport finished: success={success} failed={failed}', silent=False)
        
        self.show_dialog(dlg_type, summary)
        self._show_status(f'Import complete: {success} success, {failed} failed', color='green' if failed == 0 else 'orange')
    
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
        self._location_name_cache: Dict[int, str] = {}
        self._location_path_to_pk_cache: Dict[str, int] = {}
        self._location_pk_to_path_cache: Dict[int, str] = {}
        self._location_path_map_loaded = False
        self._part_barcodes_cache: Dict[int, List[str]] = {}
        self._barcode_endpoint_available: Optional[bool] = None
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
            label='Force barcode reassignment if barcode already exists',
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

        for attempt in range(1, attempts + 1):
            if row is not None:
                row.status = f'Checking server ({attempt}/{attempts})...'
                self._update_results_table()

            if inventree_interface.connect_to_server(force_reconnect=(attempt > 1)):
                return True

            if attempt < attempts:
                if row is not None:
                    row.status = f'Server offline, retrying ({attempt}/{attempts - 1})...'
                    self._update_results_table()
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
                        self._update_results_table()
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
        with self._rows_lock:
            row = next((r for r in self.scanned_rows if r.row_id == row_id), None)
        if not row:
            return

        try:
            row.status = 'Checking server...'
            self._update_results_table()

            if not self._connect_server_with_retries(attempts=3, delay_seconds=1.0, row=row):
                row.status = 'Server offline (after retries)'
                self._update_results_table()
                return

            row.status = 'Checking part...'
            self._update_results_table()

            part = self._find_part_by_lookup(row.lookup_value)
            if not part:
                row.status = 'Part not found'
            else:
                row.part_pk = int(part.get('pk') or part.get('id'))
                row.part_name = str(part.get('name') or part.get('IPN') or row.lookup_value)
                try:
                    row.default_location_pk = int(part.get('default_location') or 0)
                except Exception:
                    row.default_location_pk = 0
                full_location = self._resolve_location_string(part)
                row.location = self._location_leaf(full_location)

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

        self._update_results_table()

    def _find_part_by_lookup(self, lookup_value: str) -> Optional[Dict]:
        cache_key = str(lookup_value or '').strip().lower()
        if cache_key in self._part_lookup_cache:
            return self._part_lookup_cache[cache_key]

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        if not api_obj:
            self._part_lookup_cache[cache_key] = None
            return None

        token = getattr(api_obj, 'token', None)
        base_url = getattr(api_obj, 'base_url', '')
        if not token or not base_url:
            self._part_lookup_cache[cache_key] = None
            return None

        endpoint = f"{base_url.rstrip('/')}/api/part/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
        }
        response = self._request_with_retries(
            method='GET',
            url=endpoint,
            headers=headers,
            params={'search': lookup_value, 'limit': 5},
            timeout=20,
        )
        if response is None:
            self._part_lookup_cache[cache_key] = None
            return None
        payload = response.json()

        if isinstance(payload, dict):
            rows = payload.get('results') or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []

        if not rows:
            self._part_lookup_cache[cache_key] = None
            return None

        needle = lookup_value.strip().lower()
        for candidate in rows:
            ipn = str(candidate.get('IPN') or '').strip().lower()
            name = str(candidate.get('name') or '').strip().lower()
            if needle and (needle == ipn or needle == name):
                self._part_lookup_cache[cache_key] = candidate
                return candidate

        self._part_lookup_cache[cache_key] = rows[0]
        return rows[0]

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
        return '/'.join(
            part.strip()
            for part in re.sub(r'^-+\s+', '', str(location or '').strip()).split('/')
            if part.strip()
        )

    def _get_stock_location_pk(self, location: str) -> int:
        location_pk = inventree_interface.resolve_stock_location_pk(location, self._location_path_to_pk_cache)
        if location_pk > 0:
            cprint(f'[ASSIGN]\tResolved stock location from cache: {location} -> pk={location_pk}', silent=False)
        else:
            cprint(f'[ASSIGN]\tStock location cache miss: {location}', silent=False)
        return location_pk

    def _resolve_location_string(self, part: Dict) -> str:
        """Resolve location display string from part payload (name or id)."""
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

        cached_location = self._location_name_cache.get(location_id)
        if cached_location is not None:
            return cached_location

        # Prefer one-time location map load (single API sweep) over per-row tree calls.
        self._ensure_location_path_cache()
        cached_location = self._location_name_cache.get(location_id)
        if cached_location is not None:
            return cached_location

        try:
            location_tree = inventree_interface.inventree_api.get_stock_location_tree(location_id)
            # API helper returns leaf->root insertion order, so reverse for root->leaf
            names = [str(name) for name in reversed(list(location_tree.values())) if str(name).strip()]
            if names:
                resolved = '/'.join(names)
                self._location_name_cache[location_id] = resolved
                return resolved
        except Exception:
            pass

        fallback = str(location_id)
        self._location_name_cache[location_id] = fallback
        return fallback

    def _fetch_part_barcodes(self, part_pk: int) -> List[str]:
        """Fetch current external barcode values for a part via /api/barcode/."""
        cached_values = self._part_barcodes_cache.get(int(part_pk))
        if cached_values is not None:
            return list(cached_values)

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

        # Probe endpoint only once: some server versions disable GET /api/barcode/.
        if self._barcode_endpoint_available is None:
            try:
                probe_response = self._http.get(
                    endpoint,
                    headers=headers,
                    params={'limit': 1},
                    timeout=6,
                )
                self._barcode_endpoint_available = probe_response.status_code not in [404, 405]
            except Exception:
                self._barcode_endpoint_available = False

        if not self._barcode_endpoint_available:
            self._part_barcodes_cache[int(part_pk)] = []
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
                # Server variants can expose different key names
                barcode_value = item.get('data') or item.get('barcode') or item.get('value')
                if barcode_value:
                    values.append(str(barcode_value))
            return values

        # Try filtered query first.
        try:
            response = self._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={'part': part_pk, 'limit': 100},
                timeout=20,
            )
            if response is None:
                self._part_barcodes_cache[int(part_pk)] = []
                return []
            values = _extract_values(response.json())
            if values:
                self._part_barcodes_cache[int(part_pk)] = list(values)
                return values
        except Exception:
            pass

        # Fallback for servers where filtering differs: fetch and client-filter.
        try:
            response = self._request_with_retries(
                method='GET',
                url=endpoint,
                headers=headers,
                params={'limit': 200},
                timeout=20,
            )
            if response is None:
                self._part_barcodes_cache[int(part_pk)] = []
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
            self._part_barcodes_cache[int(part_pk)] = list(values)
            return values
        except Exception:
            self._part_barcodes_cache[int(part_pk)] = []
            return []

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

                url = next_url
                params = {}

            return updated, failed, ''
        except Exception as exc:
            return updated, failed, str(exc)

    def _set_part_default_location(self, part_pk: int, location_pk: int) -> bool:
        """Set part default location using direct API patch with retries."""
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

        response = self._request_with_retries(
            method='PATCH',
            url=endpoint,
            headers=headers,
            json={'default_location': int(location_pk)},
            timeout=20,
        )
        return response is not None and response.status_code in [200, 202]

    def _link_part_barcode(self, part_pk: int, barcode_value: str) -> bool:
        """Link barcode to part using direct API call with retries."""
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

        response = self._request_with_retries(
            method='POST',
            url=endpoint,
            headers=headers,
            json=payload,
            timeout=20,
        )
        return response is not None and response.status_code in [200, 201, 202]

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

    def _apply_assignments(self, _):
        with self._rows_lock:
            valid_rows = [row for row in self.scanned_rows if row.part_pk]

        if not valid_rows:
            self.show_dialog(DialogType.ERROR, 'No valid items to update')
            return

        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            self.show_dialog(DialogType.ERROR, 'Failed to connect to InvenTree server after 3 retries')
            return

        apply_location = bool(self.fields['assign_location_check'].value)
        apply_stock_items_location = bool(self.fields['assign_all_stock_items_location_check'].value)
        reassign_barcode = bool(self.fields['reassign_name_barcode_check'].value)
        force_reassign = bool(self.fields['force_barcode_reassign_check'].value)

        if apply_stock_items_location:
            # Stock item location assignment requires a selected location.
            apply_location = True

        location_pk = 0
        if apply_location:
            location_value = self.fields['location_select'].value
            if not location_value:
                self.show_dialog(DialogType.ERROR, 'Select a stock location before applying')
                return
            cprint(f'[ASSIGN]\tResolving stock location from selection: {location_value}', silent=False)
            location_pk = self._get_stock_location_pk(location_value)
            cprint(f'[ASSIGN]\tStock location resolve result: pk={location_pk}', silent=False)
            if location_pk <= 0:
                self.show_dialog(DialogType.ERROR, 'Selected stock location is not valid')
                return

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

        for idx, row in enumerate(valid_rows, start=1):
            row_ok = True
            try:
                if apply_location:
                    if int(row.default_location_pk or 0) != int(location_pk):
                        set_ok = self._set_part_default_location(row.part_pk, location_pk)
                        if not set_ok:
                            row_ok = False
                            failures.append(f'{row.part_name}: default location update failed')
                        else:
                            row.default_location_pk = int(location_pk)

                    if apply_stock_items_location and row_ok:
                        updated, stock_failed, stock_error = self._update_all_stock_items_location(
                            part_pk=row.part_pk,
                            location_pk=location_pk,
                        )
                        if stock_failed > 0 or stock_error:
                            row_ok = False
                            details = stock_error if stock_error else f'{stock_failed} stock item updates failed'
                            failures.append(f'{row.part_name}: stock location assignment failed ({details})')
                        elif updated == 0:
                            cprint(f'[ASSIGN]\tNo stock items found for part_pk={row.part_pk}', silent=False)

                if reassign_barcode and row.part_name:
                    if row.has_barcode and not force_reassign:
                        # Respect existing barcode unless explicitly forced.
                        pass
                    else:
                        barcode_ok = self._link_part_barcode(row.part_pk, row.part_name)
                        if not barcode_ok:
                            row_ok = False
                            failures.append(f'{row.part_name}: barcode reassignment failed')
            except Exception as exc:
                row_ok = False
                failures.append(f'{row.part_name or row.lookup_value}: {str(exc)[:60]}')

            if row_ok:
                success += 1
                successful_row_ids.append(row.row_id)
            else:
                failed += 1

            self.fields['progress'].value = idx / total if total else 1.0
            self.fields['progress_message'].value = f'Processing {idx}/{total}'
            self.fields['progress'].update()
            self.fields['progress_message'].update()

        self.fields['progress'].value = 1.0
        self.fields['progress'].color = 'green' if failed == 0 else ('amber' if success > 0 else 'red')
        self.fields['progress_message'].value = f'Update finished: {success} success, {failed} failed'
        self.fields['progress_message'].color = 'green' if failed == 0 else 'orange'
        self.fields['progress'].update()
        self.fields['progress_message'].update()

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
