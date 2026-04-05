import os
import flet as ft

from ..config import settings

from .views.common import update_theme, handle_transition
from .views.main import (
    PartSearchView,
    InventreeView,
    KicadView,
    CreateView,
)
from .views.barcode import BarcodeImportView, BarcodeAssignmentView
from .views.settings import (
    UserSettingsView,
    SupplierSettingsView,
    InvenTreeSettingsView,
    KiCadSettingsView,
)


def _stabilize_layout(page: ft.Page):
    """Force a full layout refresh to avoid intermittent compressed rendering."""
    try:
        for view in page.views:
            try:
                view.update()
            except Exception:
                pass
        page.update()
    except Exception:
        pass


def init_gui(page: ft.Page):
    '''Initialize page'''
    # Alignments
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 0

    # Window Icon
    page.window.icon = os.path.join(settings.PROJECT_DIR, 'gui', 'logo.ico')
    # Use the native title bar to avoid intermittent black/half-rendered chrome on Windows.
    page.window.title_bar_hidden = False
    page.window.maximizable = True
    page.window.resizable = True

    # Reflow when the window is resized / restored.
    page.on_resize = lambda e: _stabilize_layout(page)
    
    # Theme
    update_theme(page)

    # Creating a progress bar that will be used
    # to show the user that the app is busy doing something
    page.splash = ft.ProgressBar(visible=False)

    # Update
    page.update()
    _stabilize_layout(page)


def kintree_gui(page: ft.Page):
    '''Ki-nTree GUI'''
    # Init
    init_gui(page)
    # Create main views
    part_view = PartSearchView(page)
    inventree_view = InventreeView(page)
    kicad_view = KicadView(page)
    create_view = CreateView(page)
    barcode_view = BarcodeImportView(page)
    assign_view = BarcodeAssignmentView(page)
    # Create settings views
    user_settings_view = UserSettingsView(page)
    supplier_settings_view = SupplierSettingsView(page)
    inventree_settings_view = InvenTreeSettingsView(page)
    kicad_settings_view = KiCadSettingsView(page)

    # Routing
    def route_change(route):
        # print(f'\n--> Routing to {route.route}')
        if '/main' in page.route or page.route == '/':
            page.views.clear()
            if 'part' in page.route or page.route == '/':
                page.views.append(part_view)
            if 'inventree' in page.route:
                page.views.append(inventree_view)
            elif 'kicad' in page.route:
                page.views.append(kicad_view)
            elif 'create' in page.route:
                page.views.append(create_view)
            elif 'assign' in page.route:
                page.views.append(assign_view)
            elif 'barcode' in page.route:
                page.views.append(barcode_view)
        elif '/settings' in page.route:
            if '/settings' in page.views[-1].route:
                page.views.pop()
            if 'user' in page.route:
                page.views.append(user_settings_view)
            elif 'supplier' in page.route:
                page.views.append(supplier_settings_view)
            elif 'inventree' in page.route:
                page.views.append(inventree_settings_view)
            elif 'kicad' in page.route:
                page.views.append(kicad_settings_view)
            else:
                page.views.append(user_settings_view)
        page.update()
        _stabilize_layout(page)
        if '/main/barcode' in page.route:
            barcode_view.focus_barcode_input()
        if '/main/assign' in page.route:
            assign_view.focus_input()

    def view_pop(view):
        '''Pop setting view'''
        page.views.pop()
        top_view = page.views[-1]
        if 'main' in top_view.route:
            handle_transition(page, transition=True)
        # Route and render
        page.go(top_view.route)
        if 'main' in top_view.route:
            handle_transition(
                page,
                transition=False,
                update_page=True,
                timeout=0.3,
            )
        if '/main/part' in top_view.route or '/main/inventree' in top_view.route:
            top_view.partial_update()

    page.on_route_change = route_change
    page.on_view_pop = view_pop

    page.go(page.route)
