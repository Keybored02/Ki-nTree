import os
import threading
import time

import flet as ft

from ..config import settings
from .views.barcode import BarcodeImportView
from .views.common import handle_transition
from .views.common import update_theme
from .views.locations import LocationsView
from .views.main import CreateView
from .views.main import InventreeView
from .views.main import KicadView
from .views.main import PartSearchView
from .views.pickup import PickupView
from .views.verification import VerificationView
from .views.settings import InvenTreeSettingsView
from .views.settings import KiCadSettingsView
from .views.settings import SupplierSettingsView
from .views.settings import UserSettingsView


def _stabilize_layout(page: ft.Page):
    """Trigger a full page refresh. Safe from any thread."""
    try:
        page.update()
    except Exception:
        pass


def init_gui(page: ft.Page):
    """Initialize page"""
    # Alignments
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 0

    # Window Icon
    page.window.icon = os.path.join(settings.PROJECT_DIR, "gui", "logo.ico")
    # Use the native title bar to avoid intermittent black/half-rendered chrome on Windows.
    page.window.title_bar_hidden = False
    page.window.maximizable = True
    page.window.resizable = True
    # Keep the window hidden until the first route has fully rendered.
    # This prevents the half-rendered / black-box artifact on Windows startup.
    page.window.visible = False

    # Reflow when the window is resized / restored.
    page.on_resize = lambda e: _stabilize_layout(page)

    # Theme
    update_theme(page)

    # Creating a progress bar that will be used
    # to show the user that the app is busy doing something
    page.splash = ft.ProgressBar(visible=False)

    page.update()


def kintree_gui(page: ft.Page):
    """Ki-nTree GUI"""
    # Init
    init_gui(page)
    main_views = {
        "part": None,
        "inventree": None,
        "kicad": None,
        "create": None,
        "barcode": None,
        "locations": None,
        "pickup": None,
        "verification": None,
    }
    settings_views = {
        "user": None,
        "supplier": None,
        "inventree": None,
        "kicad": None,
    }

    def get_main_view(key: str):
        if key == "part" and main_views["part"] is None:
            main_views["part"] = PartSearchView(page)
        elif key == "inventree" and main_views["inventree"] is None:
            main_views["inventree"] = InventreeView(page)
        elif key == "kicad" and main_views["kicad"] is None:
            main_views["kicad"] = KicadView(page)
        elif key == "create" and main_views["create"] is None:
            main_views["create"] = CreateView(page)
        elif key == "barcode" and main_views["barcode"] is None:
            main_views["barcode"] = BarcodeImportView(page)
        elif key == "locations" and main_views["locations"] is None:
            main_views["locations"] = LocationsView(page)
        elif key == "pickup" and main_views["pickup"] is None:
            main_views["pickup"] = PickupView(page)
        elif key == "verification" and main_views["verification"] is None:
            main_views["verification"] = VerificationView(page)
        return main_views[key]

    def get_settings_view(key: str):
        if key == "user" and settings_views["user"] is None:
            settings_views["user"] = UserSettingsView(page)
        elif key == "supplier" and settings_views["supplier"] is None:
            settings_views["supplier"] = SupplierSettingsView(page)
        elif key == "inventree" and settings_views["inventree"] is None:
            settings_views["inventree"] = InvenTreeSettingsView(page)
        elif key == "kicad" and settings_views["kicad"] is None:
            settings_views["kicad"] = KiCadSettingsView(page)
        return settings_views[key]

    # Routing
    def route_change(route):
        current_route = page.route or "/"
        if "/main/assign" in current_route:
            page.go("/main/barcode")
            return
        # print(f'\n--> Routing to {route.route}')
        if "/main" in current_route or current_route == "/":
            page.views.clear()
            if "part" in current_route or current_route == "/":
                page.views.append(get_main_view("part"))
            elif not settings.COMPACT_LAYOUT and "inventree" in current_route:
                page.views.append(get_main_view("inventree"))
            elif not settings.COMPACT_LAYOUT and "kicad" in current_route:
                page.views.append(get_main_view("kicad"))
            elif not settings.COMPACT_LAYOUT and "create" in current_route:
                page.views.append(get_main_view("create"))
            elif "barcode" in current_route:
                page.views.append(get_main_view("barcode"))
            elif "locations" in current_route:
                page.views.append(get_main_view("locations"))
            elif "pickup" in current_route:
                page.views.append(get_main_view("pickup"))
            elif "verification" in current_route:
                page.views.append(get_main_view("verification"))
        elif "/settings" in current_route:
            if page.views and "/settings" in page.views[-1].route:
                page.views.pop()
            if "user" in current_route:
                page.views.append(get_settings_view("user"))
            elif "supplier" in current_route:
                page.views.append(get_settings_view("supplier"))
            elif "inventree" in current_route:
                page.views.append(get_settings_view("inventree"))
            elif "kicad" in current_route:
                page.views.append(get_settings_view("kicad"))
            else:
                page.views.append(get_settings_view("user"))
        page.update()
        if "/main/barcode" in current_route:
            main_views["barcode"].focus_barcode_input()
        if "/main/locations" in current_route:
            main_views["locations"].focus_input()
        if "/main/verification" in current_route:
            main_views["verification"].focus_input()

    def view_pop(view):
        """Pop setting view"""
        page.views.pop()
        top_view = page.views[-1]
        if "main" in top_view.route:
            handle_transition(page, transition=True)
        # Route and render
        page.go(top_view.route)
        if "main" in top_view.route:
            handle_transition(
                page,
                transition=False,
                update_page=True,
                timeout=0.3,
            )
        if "/main/part" in top_view.route or "/main/inventree" in top_view.route:
            top_view.partial_update()

    page.on_route_change = route_change
    page.on_view_pop = view_pop

    page.go(page.route or "/")

    # Reveal the window after the first route has been rendered.
    # A short delay on a background thread gives the Flutter engine time to
    # finish compositing the first frame before the OS shows the window,
    # eliminating the half-rendered / black-box artifact on Windows.
    def _reveal():
        time.sleep(0.25)
        try:
            page.window.maximized = True
            page.window.visible = True
            page.update()
        except Exception:
            import logging

            logging.exception("Exception in _reveal window logic:")

    threading.Thread(target=_reveal, daemon=True).start()
