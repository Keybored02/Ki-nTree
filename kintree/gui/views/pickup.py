"""Inventory pickup / put-down view for Ki-nTree."""

from typing import Dict, List, Optional

import flet as ft

from .common import GUI_PARAMS, DialogType
from .main import MainView


class PickupItem:
    """A single BOM line shown in the pickup list."""

    def __init__(self, reference: str, part_name: str, location: str, quantity: int = 1):
        self.reference = reference
        self.part_name = part_name
        self.location = location
        self.quantity = quantity
        self.checked = False


class PickupView(MainView):
    """Inventory pickup (Out) and put-down (In) view."""

    title = 'Pickup'
    fields: Dict = {}

    # Mode constants
    MODE_OUT = 'out'
    MODE_IN = 'in'

    def __init__(self, page: ft.Page):
        self._mode = self.MODE_OUT
        self._items: List[PickupItem] = []

        super().__init__(page=page)
        self.build_page()

    # ------------------------------------------------------------------ #
    #  Build UI                                                            #
    # ------------------------------------------------------------------ #

    def build_page(self) -> None:
        # --- BOM search bar ---
        self.fields['bom_search'] = ft.TextField(
            label='Search Assembly / BOM',
            hint_text='Assembly name or IPN (not yet connected)',
            width=GUI_PARAMS['textfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            prefix_icon=ft.icons.SEARCH,
            on_submit=self._on_search,
        )
        self.fields['search_btn'] = ft.ElevatedButton(
            text='Load',
            icon=ft.icons.DOWNLOAD_OUTLINED,
            on_click=self._on_search,
            disabled=False,
        )

        # --- Mode toggle ---
        self._mode_label = ft.Text(
            'Out (Pickup)',
            size=14,
            weight=ft.FontWeight.BOLD,
            color='blue',
        )
        self.fields['mode_toggle'] = ft.Switch(
            label='',
            value=False,          # False = Out, True = In
            on_change=self._on_mode_change,
        )

        # --- Status / info bar ---
        self._status_text = ft.Text('', size=13, color='grey', italic=True)

        # --- Results table ---
        self._table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text('', width=32)),     # checkbox placeholder
                ft.DataColumn(ft.Text('Reference')),
                ft.DataColumn(ft.Text('Part Name')),
                ft.DataColumn(ft.Text('Location')),
                ft.DataColumn(ft.Text('Qty', tooltip='Quantity')),
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

        # --- Clear / confirm buttons ---
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

        # ---- Layout ----
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
        """Triggered when the user submits a BOM search.  Backend not yet wired."""
        query = (self.fields['bom_search'].value or '').strip()
        if not query:
            self._set_status('Enter an assembly name or IPN first.', color='orange')
            return
        # Placeholder: show a stub row so the UI is exercisable.
        self._set_status(f'Backend not connected — showing stub for "{query}"', color='orange')
        stub_items = [
            PickupItem('C1', 'Capacitor 100nF 0402', 'Shelf A / Bin 3', 10),
            PickupItem('R1', 'Resistor 10k 0402', 'Shelf B / Bin 7', 4),
            PickupItem('U1', 'STM32F103C8T6', 'Shelf C / Drawer 2', 1),
        ]
        self._load_items(stub_items)

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
        self._set_status('Cleared.', color='grey')
        self._rebuild_table()
        self.fields['bom_search'].update()
        self.fields['confirm_btn'].update()

    def _on_confirm(self, e):
        checked = [it for it in self._items if it.checked]
        mode_label = 'picked up' if self._mode == self.MODE_OUT else 'put down'
        self._set_status(
            f'{len(checked)} item(s) {mode_label} (backend not connected).',
            color='green',
        )

    def _on_row_check(self, e, item: PickupItem):
        item.checked = e.control.value
        any_checked = any(it.checked for it in self._items)
        self.fields['confirm_btn'].disabled = not any_checked
        self.fields['confirm_btn'].update()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _load_items(self, items: List[PickupItem]):
        self._items = items
        self._rebuild_table()
        has_items = bool(items)
        self.fields['confirm_btn'].disabled = True   # need at least one checkbox tick
        self.fields['confirm_btn'].update()

    def _rebuild_table(self):
        mode_verb = 'Pickup' if self._mode == self.MODE_OUT else 'Put-Down'
        self._table.columns[0].label = ft.Checkbox(
            value=False,
            on_change=self._on_select_all,
            tooltip=f'Select all for {mode_verb}',
        )
        rows = []
        for item in self._items:
            row = ft.DataRow(
                cells=[
                    ft.DataCell(
                        ft.Checkbox(
                            value=item.checked,
                            on_change=lambda e, it=item: self._on_row_check(e, it),
                        )
                    ),
                    ft.DataCell(ft.Text(item.reference, size=12)),
                    ft.DataCell(ft.Text(item.part_name, size=12, no_wrap=True)),
                    ft.DataCell(ft.Text(item.location, size=12, italic=True)),
                    ft.DataCell(ft.Text(str(item.quantity), size=12)),
                ],
            )
            rows.append(row)
        self._table.rows = rows
        self._table.update()

    def _on_select_all(self, e):
        checked = e.control.value
        for item in self._items:
            item.checked = checked
        self.fields['confirm_btn'].disabled = not (checked and bool(self._items))
        self.fields['confirm_btn'].update()
        self._rebuild_table()

    def _set_status(self, msg: str, color: str = 'grey'):
        self._status_text.value = msg
        self._status_text.color = color
        self._status_text.update()
