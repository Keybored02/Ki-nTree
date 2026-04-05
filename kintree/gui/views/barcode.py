"""Barcode scanner GUI view for rapid part import into InvenTree.

This module provides the main GUI interface for barcode scanning and bulk part
import. Users can scan multiple barcodes, configure import settings (category,
location, stock creation), and import all items to InvenTree in one operation.

Supports: TME (key-value), Mouser (GS1-128), Digi-Key (GS1-128) barcodes.
"""

import flet as ft
from typing import Dict, List

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
        
        # Load categories and locations
        self._load_categories_and_locations()
        self.focus_barcode_input()

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
            if not category_list and inventree_interface.connect_to_server():
                category_list = inventree_interface.build_category_tree(reload=True)

            location_list = inventree_interface.build_stock_location_tree(reload=False)
            if not location_list and inventree_interface.connect_to_server():
                location_list = inventree_interface.build_stock_location_tree(reload=True)

            category_options = [ft.dropdown.Option(category) for category in category_list]
            location_options = [ft.dropdown.Option(location) for location in location_list]

            self.categories = category_list
            self.stock_locations = location_list
            self.fields['category_select'].options = category_options
            self.fields['location_select'].options = location_options
            
            self._page.update()
        except Exception as e:
            cprint(f'[ERROR] Failed to load categories/locations: {e}', silent=False)

    def _reload_categories(self, _):
        """Reload the category tree from InvenTree."""
        try:
            if not inventree_interface.connect_to_server():
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
            if not inventree_interface.connect_to_server():
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
        else:
            self._show_status('Unknown barcode format', color='red')

    def _append_parsed_barcode(self, barcode: str, update_table: bool = True, update_status: bool = True) -> bool:
        """Parse and append one barcode row without removing existing entries."""
        parsed = self.parser.parse(barcode)
        if parsed.get('supplier') == 'unknown':
            if update_status:
                self._show_status('Unknown barcode format', color='red')
            return False

        row = BarcodeScannedRow(barcode, parsed)
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
    
    def _on_category_changed(self, *args, **kwargs):
        """Apply selected category to all items."""
        category = self.fields['category_select'].value
        if category:
            for row in self.scanned_rows:
                row.category = category
            self._update_results_table()
    
    def _on_location_changed(self, *args, **kwargs):
        """Apply selected location to all items."""
        location = self.fields['location_select'].value
        if location:
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

        if not inventree_interface.connect_to_server():
            cprint('[BARCODE]\tImport aborted: failed to connect to InvenTree', silent=False)
            self.fields['import_progress_message'].value = 'Import failed: could not connect'
            self.fields['import_progress_message'].color = 'red'
            self.fields['import_progress_message'].update()
            self.show_dialog(DialogType.ERROR, 'Failed to connect to InvenTree server')
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
                    location_pk = inventree_interface.get_inventree_stock_location_id(
                        [row.location]
                    )
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
            dlg_type = DialogType.SUCCESS
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
