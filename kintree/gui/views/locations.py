"""Location management GUI view for moving stock locations in InvenTree."""

import threading
import time
from typing import Dict, List, Optional

import flet as ft
import requests

from ...common.tools import cprint
from ...database import inventree_interface
from .common import DialogType, DropdownWithSearch, GUI_PARAMS
from .main import MainView


class LocationsView(MainView):
    """Manage stock location hierarchy operations."""

    title = 'Locations'
    fields = {}

    _ROOT_LABEL = '(root)'

    def __init__(self, page: ft.Page):
        self.stock_locations: List[str] = []
        self.stock_location_id_map: Dict[str, int] = {}
        self._connect_lock = threading.Lock()
        self._http = requests.Session()

        super().__init__(page=page)
        self.build_page()

    def build_page(self) -> None:
        self.fields['source_location'] = DropdownWithSearch(
            label='Location To Move',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_source_changed,
        )

        self.fields['target_parent'] = DropdownWithSearch(
            label='New Parent Location',
            dr_width=GUI_PARAMS['textfield_width'],
            sr_width=GUI_PARAMS['searchfield_width'],
            dense=GUI_PARAMS['textfield_dense'],
            options=[],
            on_change=self._on_target_changed,
        )

        self.fields['reload_locations'] = ft.IconButton(
            icon=ft.icons.REPLAY,
            tooltip='Reload stock locations from InvenTree',
            on_click=self._reload_locations,
        )

        self.fields['clear_selection'] = ft.IconButton(
            icon=ft.icons.CLEAR,
            tooltip='Clear selected source/parent',
            on_click=self._clear_selection,
        )

        self.fields['source_preview'] = ft.Text(value='-', size=12)
        self.fields['target_preview'] = ft.Text(value='-', size=12)
        self.fields['move_preview'] = ft.Text(value='-', size=12, color='blue')

        self.fields['move_button'] = ft.ElevatedButton(
            text='Move Location',
            on_click=self._on_move,
            color='white',
            bgcolor='green',
            width=200,
        )

        self.fields['status_message'] = ft.Text(value='Ready', size=12, color='blue')

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row([ft.Text('Location Manager', style=ft.TextThemeStyle.HEADLINE_MEDIUM)]),
                            ft.Divider(),
                            ft.Text('1. Select Source And Target:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Row([
                                self.fields['source_location'],
                                self.fields['reload_locations'],
                            ]),
                            ft.Row([
                                self.fields['target_parent'],
                                self.fields['clear_selection'],
                            ]),
                            ft.Divider(),
                            ft.Text('2. Review Move:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Row([ft.Text('Source:', width=100), self.fields['source_preview']]),
                            ft.Row([ft.Text('Target Parent:', width=100), self.fields['target_preview']]),
                            ft.Row([ft.Text('Result Path:', width=100), self.fields['move_preview']]),
                            ft.Divider(),
                            ft.Text('3. Apply:', style=ft.TextThemeStyle.BODY_LARGE),
                            ft.Row([
                                self.fields['move_button'],
                                ft.ElevatedButton(
                                    text='Back',
                                    on_click=lambda _: self._page.go('/main/create'),
                                ),
                            ]),
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
        self._load_locations(reload=False)
        self.focus_input()
        return super().did_mount()

    def focus_input(self):
        try:
            self.fields['source_location'].search_field.focus()
            self.fields['source_location'].update()
        except Exception:
            pass

    def _show_status(self, message: str, color: str = 'black'):
        self.fields['status_message'].value = message
        self.fields['status_message'].color = color
        try:
            self.fields['status_message'].update()
        except AssertionError:
            pass

    def _connect_server_with_retries(self, attempts: int = 3, delay_seconds: float = 1.5) -> bool:
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

    def _load_locations(self, reload: bool = False):
        try:
            location_list = inventree_interface.build_stock_location_tree(reload=reload)
            self.stock_locations = list(location_list)
            self.stock_location_id_map = inventree_interface.get_stock_location_id_map() or {}

            source_options = [ft.dropdown.Option(location) for location in self.stock_locations]
            target_options = [ft.dropdown.Option(self._ROOT_LABEL)] + [
                ft.dropdown.Option(location) for location in self.stock_locations
            ]

            self.fields['source_location'].options = source_options
            self.fields['target_parent'].options = target_options
            self.fields['source_location'].disabled = False
            self.fields['target_parent'].disabled = False
            self.fields['source_location'].done_search()
            self.fields['target_parent'].done_search()
            self._page.update()
        except Exception as exc:
            cprint(f'[ERROR] Failed to load stock locations: {exc}', silent=False)
            self._show_status('Failed to load stock locations', color='red')

    def _reload_locations(self, _):
        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            self.show_dialog(DialogType.ERROR, 'ERROR: Failed to connect to InvenTree server')
            return
        self._load_locations(reload=True)
        self._show_status('Stock locations reloaded', color='green')

    def _clear_selection(self, _):
        self.fields['source_location'].value = None
        self.fields['target_parent'].value = None
        self.fields['source_preview'].value = '-'
        self.fields['target_preview'].value = '-'
        self.fields['move_preview'].value = '-'
        try:
            self.fields['source_location'].update()
            self.fields['target_parent'].update()
            self.fields['source_preview'].update()
            self.fields['target_preview'].update()
            self.fields['move_preview'].update()
        except AssertionError:
            pass
        self._show_status('Selection cleared', color='blue')

    def _on_source_changed(self, *_args, **_kwargs):
        self._update_preview()

    def _on_target_changed(self, *_args, **_kwargs):
        self._update_preview()

    def _update_preview(self):
        source = str(self.fields['source_location'].value or '').strip()
        target = str(self.fields['target_parent'].value or '').strip()

        self.fields['source_preview'].value = source or '-'
        self.fields['target_preview'].value = target or '-'

        if source and target:
            if target == self._ROOT_LABEL:
                leaf = source.split('/')[-1]
                self.fields['move_preview'].value = leaf
            else:
                leaf = source.split('/')[-1]
                self.fields['move_preview'].value = f'{target}/{leaf}'
        else:
            self.fields['move_preview'].value = '-'

        try:
            self.fields['source_preview'].update()
            self.fields['target_preview'].update()
            self.fields['move_preview'].update()
        except AssertionError:
            pass

    def _resolve_location_pk(self, path_text: str) -> int:
        return inventree_interface.resolve_stock_location_pk(path_text, self.stock_location_id_map)

    def _on_move(self, _):
        source = str(self.fields['source_location'].value or '').strip()
        target = str(self.fields['target_parent'].value or '').strip()

        if not source:
            self.show_dialog(DialogType.ERROR, 'Select source location')
            return

        if not target:
            self.show_dialog(DialogType.ERROR, 'Select target parent location')
            return

        if target != self._ROOT_LABEL and source == target:
            self.show_dialog(DialogType.ERROR, 'Source and target parent cannot be the same')
            return

        if target != self._ROOT_LABEL and target.startswith(f'{source}/'):
            self.show_dialog(DialogType.ERROR, 'Cannot move a location into its own child')
            return

        if not self._connect_server_with_retries(attempts=3, delay_seconds=1.5):
            self.show_dialog(DialogType.ERROR, 'Failed to connect to InvenTree server after retries')
            return

        source_pk = self._resolve_location_pk(source)
        if source_pk <= 0:
            self.show_dialog(DialogType.ERROR, f'Source location not found: {source}')
            return

        parent_pk: Optional[int]
        if target == self._ROOT_LABEL:
            parent_pk = None
        else:
            resolved_parent = self._resolve_location_pk(target)
            if resolved_parent <= 0:
                self.show_dialog(DialogType.ERROR, f'Target parent not found: {target}')
                return
            parent_pk = int(resolved_parent)

        api_obj = getattr(inventree_interface.inventree_api, 'inventree_api', None)
        token = getattr(api_obj, 'token', None) if api_obj else None
        base_url = getattr(api_obj, 'base_url', '') if api_obj else ''
        if not token or not base_url:
            self.show_dialog(DialogType.ERROR, 'InvenTree auth context unavailable')
            return

        endpoint = f"{base_url.rstrip('/')}/api/stock/location/{int(source_pk)}/"
        headers = {
            'Authorization': f'Token {token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        try:
            response = self._http.patch(endpoint, headers=headers, json={'parent': parent_pk}, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            message = str(exc)
            try:
                body = exc.response.text[:300] if exc.response is not None else ''
            except Exception:
                body = ''
            self.show_dialog(DialogType.ERROR, f'Failed to move location: {message}\n{body}')
            return

        self._show_status('Location moved successfully', color='green')
        self._load_locations(reload=True)
