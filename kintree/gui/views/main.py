from concurrent.futures import as_completed
from concurrent.futures import ThreadPoolExecutor
import copy
from importlib import import_module
import os
import time

import flet as ft

# Version
from ... import __version__

# Settings
from ...common import progress

# Tools
from ...common.tools import cprint
from ...common.tools import download_with_retry
from ...config import config_interface
from ...config import settings

# InvenTree
from ...database import inventree_interface

# KiCad
from ...kicad import kicad_interface

# SnapEDA
from ...search import snapeda_api

# Common view
from .common import CommonView
from .common import data_from_views
from .common import DialogType
from .common import DropdownWithSearch
from .common import GUI_PARAMS
from .common import handle_transition
from .common import SwitchWithRefs

# Main AppBar
main_appbar = ft.AppBar(
    leading=ft.Container(
        content=ft.Image(
            src=os.path.join(settings.PROJECT_DIR, "gui", "logo.ico"),
            fit=ft.ImageFit.CONTAIN,
        ),
        padding=ft.padding.only(left=10),
    ),
    leading_width=40,
    title=ft.Container(ft.Text(f"Ki-nTree | {__version__}"), width=10000),
    center_title=False,
    bgcolor=ft.colors.SURFACE_VARIANT,
    actions=[],
)

# Navigation Controls
# When COMPACT_LAYOUT is True, InvenTree / KiCad / Create are embedded inside
# PartSearchView and use nav_index=None (hidden from the sidebar).
# When False they get their own sidebar entries and routes (classic layout).


def _build_navigation():
    """Return (MAIN_NAVIGATION dict, NAV_BAR_INDEX dict, NavigationRail)
    appropriate for the current COMPACT_LAYOUT setting."""
    compact = settings.COMPACT_LAYOUT

    if compact:
        nav = {
            "Part Search": {"nav_index": 0, "route": "/main/part"},
            "InvenTree": {"nav_index": None, "route": "/main/inventree"},
            "KiCad": {"nav_index": None, "route": "/main/kicad"},
            "Create": {"nav_index": None, "route": "/main/create"},
            "Barcode": {"nav_index": 1, "route": "/main/barcode"},
            "Locations": {"nav_index": 2, "route": "/main/locations"},
            "Pickup": {"nav_index": 3, "route": "/main/pickup"},
            "Verification": {"nav_index": 4, "route": "/main/verification"},
        }
        destinations = [
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.SCREEN_SEARCH_DESKTOP_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.SCREEN_SEARCH_DESKTOP_SHARP, size=40),
                label_content=ft.Text("Part Search", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.QR_CODE_2_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.QR_CODE_2, size=40),
                label_content=ft.Text("Barcode", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.EDIT_LOCATION_ALT_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.EDIT_LOCATION_ALT, size=40),
                label_content=ft.Text("Locations", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.SHOPPING_BASKET_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.SHOPPING_BASKET, size=40),
                label_content=ft.Text("Pickup", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.FACT_CHECK_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.FACT_CHECK, size=40),
                label_content=ft.Text("Verification", size=16),
                padding=10,
            ),
        ]
    else:
        nav = {
            "Part Search": {"nav_index": 0, "route": "/main/part"},
            "InvenTree": {"nav_index": 1, "route": "/main/inventree"},
            "KiCad": {"nav_index": 2, "route": "/main/kicad"},
            "Create": {"nav_index": 3, "route": "/main/create"},
            "Barcode": {"nav_index": 4, "route": "/main/barcode"},
            "Locations": {"nav_index": 5, "route": "/main/locations"},
            "Pickup": {"nav_index": 6, "route": "/main/pickup"},
            "Verification": {"nav_index": 7, "route": "/main/verification"},
        }
        destinations = [
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.SCREEN_SEARCH_DESKTOP_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.SCREEN_SEARCH_DESKTOP_SHARP, size=40),
                label_content=ft.Text("Part Search", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.INVENTORY_2_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.INVENTORY_2, size=40),
                label_content=ft.Text("InvenTree", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.SETTINGS_INPUT_COMPONENT_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.SETTINGS_INPUT_COMPONENT, size=40),
                label_content=ft.Text("KiCad", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.BUILD_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.BUILD, size=40),
                label_content=ft.Text("Create", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.QR_CODE_2_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.QR_CODE_2, size=40),
                label_content=ft.Text("Barcode", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.EDIT_LOCATION_ALT_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.EDIT_LOCATION_ALT, size=40),
                label_content=ft.Text("Locations", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.SHOPPING_BASKET_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.SHOPPING_BASKET, size=40),
                label_content=ft.Text("Pickup", size=16),
                padding=10,
            ),
            ft.NavigationRailDestination(
                icon_content=ft.Icon(name=ft.icons.FACT_CHECK_OUTLINED, size=40),
                selected_icon_content=ft.Icon(name=ft.icons.FACT_CHECK, size=40),
                label_content=ft.Text("Verification", size=16),
                padding=10,
            ),
        ]

    nav_bar_index = {v["nav_index"]: v["route"] for v in nav.values() if v["nav_index"] is not None}
    rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=100,
        min_extended_width=400,
        group_alignment=-0.9,
        destinations=destinations,
        on_change=None,
    )
    return nav, nav_bar_index, rail


MAIN_NAVIGATION, NAV_BAR_INDEX, main_navrail = _build_navigation()


class MainView(CommonView):
    """Main view"""

    route = None
    data = None

    def __init__(self, page: ft.Page):
        # Get route
        self.route = MAIN_NAVIGATION[self.title].get("route", "/")

        # Init view
        super().__init__(page=page, appbar=main_appbar, navigation_rail=main_navrail)

        # Update application bar
        if not self.appbar.actions:
            self.appbar.actions.extend(
                [
                    ft.IconButton(
                        ft.icons.SETTINGS,
                        on_click=self.call_settings,
                    ),
                ]
            )
        else:
            self.appbar.actions[0].on_click = self.call_settings

        # Update navigation rail
        self.navigation_rail.on_change = self.nav_rail_redirect

        # Init data
        self.data = {}

        # Process enable switch
        if "enable" in self.fields:
            self.fields["enable"].on_change = self.process_enable

        # Add floating button to reset view
        self.floating_action_button = ft.FloatingActionButton(
            icon=ft.icons.REPLAY,
            on_click=self.reset_view,
        )

    def nav_rail_redirect(self, e):
        self._page.go(NAV_BAR_INDEX[e.control.selected_index])

    def call_settings(self, e):
        handle_transition(self._page, transition=True)
        self._page.go("/settings")

    def reset_view(self, e, ignore=None, hidden=None):
        if ignore is None:
            ignore = ["enable"]
        if hidden is None:
            hidden = {}

        def reset_field(field):
            if isinstance(field, ft.ProgressBar):
                field.value = 0
            else:
                field.value = None

        for name, field in self.fields.items():
            if isinstance(field, dict):
                for _key, value in field.items():
                    value.disabled = True
                    reset_field(value)
            else:
                if name not in ignore:
                    reset_field(field)

        if hidden:
            for key, value in hidden.items():
                if not value:
                    self.data[key] = value
                else:
                    self.data[key] = None

        # Clear data
        self.push_data()
        self._page.update()

    def partial_update(self):
        """Process partial view updates"""
        return

    def process_enable(self, e, value=None, ignore=None):
        if ignore is None:
            ignore = ["enable"]
        disabled = False
        if e.data.lower() == "false":
            disabled = True

        # Overwrite with value
        if value is not None:
            disabled = not value

        key = e.control.label.lower()
        settings.set_enable_flag(key, not disabled)

        for name, field in self.fields.items():
            if name not in ignore:
                field.disabled = disabled
                _safe_update(field)
        self.push_data(e)

    def sanitize_data(self):
        return

    def push_data(self, e=None, hidden=None, **kwargs):
        if hidden is None:
            hidden = {}
        for key, field in self.fields.items():
            try:
                self.data[key] = field.value
            except AttributeError:
                pass

        if hidden:
            for key, value in hidden.items():
                self.data[key] = value

        # Sanitize data before pushing
        self.sanitize_data()
        # Push to shared data store
        data_from_views[self.title] = self.data


def _safe_update(ctrl):
    """Call ctrl.update() only when the control is already mounted in the page."""
    try:
        ctrl.update()
    except AssertionError:
        pass


def _compact_dropdown(field: "DropdownWithSearch", sr_w: int) -> ft.Container:
    """Wrap a DropdownWithSearch so it fills its column and clips search expansion.

    The dropdown itself expands to fill available width (no fixed pixel width).
    The search box animates to sr_w but is clipped at the container boundary so
    it never overflows into adjacent columns.
    """
    # Let the dropdown expand to fill whatever the column gives it
    field.dropdown.width = None
    field.dropdown.expand = True
    # Keep the search field at a reasonable fixed width; it animates from 0
    field.search_field.width = sr_w
    field.search_width = sr_w
    return ft.Container(
        content=field,
        expand=True,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
    )


def _compact_inventree_column(view: "InventreeView", sr_w: int):
    """Rebuild InventreeView.column controls with a narrow, wrapping-safe layout."""
    # Let text fields expand rather than use fixed pixel widths
    for key in (
        "IPN: Category Code",
        "New Category Code",
        "Existing Part ID",
        "Existing Part IPN",
        "Stock quantity",
    ):
        if key in view.fields and hasattr(view.fields[key], "expand"):
            view.fields[key].width = None
            view.fields[key].expand = True
    # Resize buttons to fit label (remove fixed width, let text determine size)
    for key in ("load_categories", "load_stock_locations"):
        view.fields[key].width = None

    view.column.controls = [
        ft.Row([view.fields["enable"], view.fields["alternate"]], spacing=8),
        ft.Row([view.fields["load_categories"]], spacing=0),
        _compact_dropdown(view.fields["Category"], sr_w),
        ft.Row(
            ref=view.ipncode_row_ref,
            controls=[
                view.fields["IPN: Category Code"],
                view.fields["Create New Code"],
            ],
            spacing=4,
        ),
        ft.Row([view.fields["New Category Code"]]),
        ft.Row([view.fields["check_existing"]]),
        ft.Column(
            ref=view.alternate_row_ref,
            controls=[
                ft.Row(
                    [view.fields["Existing Part ID"], view.fields["Existing Part IPN"]],
                    spacing=4,
                ),
                ft.Row([view.fields["Update Parameter"]]),
            ],
        ),
        ft.Column(
            ref=view.create_stock_widgets_ref,
            controls=[ft.Row([view.fields["Create stock"]])],
        ),
        _compact_dropdown(view.fields["Stock location"], sr_w),
        ft.Row([view.fields["load_stock_locations"]], spacing=0),
        ft.Row([view.fields["Part barcode"]]),
        ft.Column(
            ref=view.create_stock_widgets_ref,
            controls=[
                ft.Row([view.fields["Stock quantity"]]),
                ft.Row([view.fields["Make stock location default"]]),
            ],
        ),
    ]


def _compact_kicad_column(view: "KicadView", sr_w: int):
    """Rebuild KicadView.column controls replacing raw field list with wrapped dropdowns."""
    view.fields["New Footprint Name"].width = None
    view.fields["New Footprint Name"].expand = True
    view.fields["Check SnapEDA"].width = None  # auto-size to content

    view.column.controls = [
        ft.Row([view.fields["enable"]]),
        _compact_dropdown(view.fields["Symbol Library"], sr_w),
        _compact_dropdown(view.fields["Symbol Template"], sr_w),
        _compact_dropdown(view.fields["Footprint Library"], sr_w),
        _compact_dropdown(view.fields["Footprint"], sr_w),
        ft.Row([view.fields["New Footprint"], view.fields["New Footprint Name"]], spacing=4),
        ft.Row([view.fields["Check SnapEDA"]]),
    ]


def _compact_create_column(view: "CreateView"):
    """Rebuild CreateView.column controls without hardcoded row widths."""
    # Progress bars — let them expand to fill the column
    view.fields["inventree_progress"].width = None
    view.fields["inventree_progress"].expand = True
    view.fields["kicad_progress"].width = None
    view.fields["kicad_progress"].expand = True
    # Bulk excel text field — expand to fill the column
    view.fields["bulk_excel_path"].width = None
    view.fields["bulk_excel_path"].expand = True
    view.fields["bulk_excel_path"].hint_text = "Select an Excel file…"
    # Resize buttons to auto-width
    for key in ("bulk_excel_pick", "bulk_import", "create", "cancel"):
        view.fields[key].width = None
    # Strip extra icons from Create Part / Cancel to save space
    view.fields["create"].content = ft.Row(
        [ft.Icon("build_circle"), ft.Text("Create Part", size=16)], spacing=4
    )
    view.fields["cancel"].content = ft.Row(
        [ft.Icon("highlight_remove"), ft.Text("Cancel", size=16)], spacing=4
    )
    view.fields["bulk_excel_pick"].content = ft.Row(
        [ft.Icon(ft.icons.UPLOAD_FILE), ft.Text("Select Excel", size=14)], spacing=4
    )
    view.fields["bulk_import"].content = ft.Row(
        [ft.Icon(ft.icons.PLAYLIST_ADD_CHECK_CIRCLE), ft.Text("Bulk Import", size=14)],
        spacing=4,
    )

    view.column.controls = [
        ft.Row([view.fields["bulk_status"]]),
        ft.Row([view.fields["bulk_excel_path"]], expand=True),
        ft.Row([view.fields["bulk_excel_pick"], view.fields["bulk_import"]], spacing=6),
        ft.Row([view.fields["create"], view.fields["cancel"]], spacing=6),
        ft.Row(height=8),
        ft.Row(
            ref=view.inventree_progress_row,
            controls=[
                ft.Icon(ft.icons.INVENTORY_2, size=24),
                ft.Text("InvenTree", size=14, weight=ft.FontWeight.BOLD, width=80),
                view.fields["inventree_progress"],
            ],
            expand=True,
            visible=settings.ENABLE_INVENTREE,
        ),
        ft.Row(
            ref=view.kicad_progress_row,
            controls=[
                ft.Icon(ft.icons.SETTINGS_INPUT_COMPONENT, size=24),
                ft.Text("KiCad", size=14, weight=ft.FontWeight.BOLD, width=80),
                view.fields["kicad_progress"],
            ],
            expand=True,
            visible=settings.ENABLE_KICAD,
        ),
    ]


class PartSearchView(MainView):
    """Part search view"""

    title = "Part Search"

    # List of search fields
    search_fields_list = [
        "name",
        "description",
        "revision",
        "keywords",
        "supplier_name",
        "supplier_part_number",
        "supplier_link",
        "manufacturer_name",
        "manufacturer_part_number",
        "datasheet",
        "image",
    ]

    fields = {
        "part_number": ft.TextField(
            label="Part Number",
            dense=True,
            hint_text="Part Number",
            width=250,
            expand=True,
        ),
        "supplier": ft.Dropdown(label="Supplier", dense=True, width=250),
        "search_button": ft.IconButton(
            icon=ft.icons.SEND,
            icon_color="blue900",
            icon_size=32,
            height=48,
            width=48,
            tooltip="Submit",
        ),
        "parameter_view": ft.Switch(label="View Parameters", disabled=True),
        "search_form": {},
        "parameter_form": {},
    }

    def reset_view(self, e, ignore=None):
        if ignore is None:
            ignore = ["enable"]
        hidden_fields = {
            "searched_part_number": "",
            "custom_part": None,
        }
        self.fields["parameter_form"] = {}
        try:
            self.fields["part_number"].focus()
        except AssertionError:
            pass
        return super().reset_view(e, ignore=ignore, hidden=hidden_fields)

    def enable_search_fields(self):
        for form_field in self.fields["search_form"].values():
            form_field.disabled = False
        self.fields["parameter_view"].disabled = False
        self._page.update()
        return

    def run_search(self, e):
        # Reset view
        self.reset_view(e, ignore=["part_number", "supplier"])
        self.switch_view()
        # Validate form
        if (
            bool(self.fields["part_number"].value) != bool(self.fields["supplier"].value)
            or not self.fields["part_number"].value
            and not self.fields["supplier"].value
        ):
            if not self.fields["part_number"].value:
                error_msg = "Missing Part Number"
            else:
                error_msg = "Missing Supplier"
            self.show_dialog(
                d_type=DialogType.ERROR,
                message=error_msg,
            )
        else:
            self.fields["part_number"].value = self.fields["part_number"].value.strip()
            self._page.splash.visible = True
            self._page.update()

            if not self.fields["part_number"].value and not self.fields["supplier"].value:
                self.data["custom_part"] = True
                self.enable_search_fields()
            else:
                self.data["custom_part"] = False

                # Get supplier
                supplier = inventree_interface.get_supplier_name(self.fields["supplier"].value)
                # Supplier search
                part_supplier_info = inventree_interface.supplier_search(
                    supplier, self.fields["part_number"].value
                )

                part_supplier_form = None

                if part_supplier_info:
                    # Translate to user form format
                    part_supplier_form = inventree_interface.translate_supplier_to_form(
                        supplier=supplier,
                        part_info=part_supplier_info,
                    )
                    if part_supplier_form:
                        for _field_idx, field_name in enumerate(self.fields["search_form"].keys()):
                            # print(field_idx, field_name, get_default_search_keys()[field_idx], search_form_field[field_name])
                            try:
                                self.fields["search_form"][
                                    field_name
                                ].value = part_supplier_form.get(field_name, "")
                            except IndexError:
                                pass
                            # Enable editing
                            self.enable_search_fields()
                    # Stitch parameters
                    if part_supplier_info.get("parameters", None):
                        self.data["parameters"] = part_supplier_info["parameters"]
                        for parameter, value in self.data["parameters"].items():
                            text_field = ft.TextField(
                                label=parameter,
                                value=value,
                                expand=True,
                                on_change=self.push_data,
                            )
                            self.fields["parameter_form"][parameter] = text_field
                    # and pricing
                    if part_supplier_info.get("pricing", None):
                        self.data["pricing"] = part_supplier_info["pricing"]
                        self.data["currency"] = part_supplier_info.get("currency", None)

            # Add to data buffer
            self.push_data()
            self._page.splash.visible = False

            if not self.data["supplier_part_number"] and not self.data["custom_part"]:
                self.show_dialog(
                    d_type=DialogType.ERROR,
                    message="Part not found",
                )
            elif not self.data["manufacturer_part_number"]:
                self.show_dialog(
                    d_type=DialogType.ERROR,
                    message="Found part has no manufacturer part number",
                )
            elif (
                self.data["searched_part_number"].lower()
                != self.data["manufacturer_part_number"].lower()
            ):
                self.show_dialog(
                    d_type=DialogType.WARNING,
                    message="Found manufacturer part number does not match the requested part number",
                )
            self._page.update()
        return

    def push_data(self, e=None, **kwargs):
        hidden_fields = {
            "searched_part_number": self.fields["part_number"].value,
            "custom_part": self.data.get("custom_part", None),
        }
        for key, field in self.fields["search_form"].items():
            self.data[key] = field.value
        for key, field in self.fields["parameter_form"].items():
            self.data["parameters"][key] = field.value
        return super().push_data(e, hidden=hidden_fields)

    def partial_update(self):
        # Update supplier options
        self.update_suppliers()
        # Refresh embedded sub-views after settings changes (compact layout only)
        if settings.COMPACT_LAYOUT and self._inventree_view is not None:
            self._inventree_view.partial_update()
        if settings.COMPACT_LAYOUT and self._kicad_view is not None:
            self._kicad_view.partial_update()

    def update_suppliers(self):
        # Reload suppliers
        self.fields["supplier"].options = [
            ft.dropdown.Option(supplier) for supplier in settings.SUPPORTED_SUPPLIERS_API
        ]
        if len(self.fields["supplier"].options) == 1:
            self.fields["supplier"].value = self.fields["supplier"].options[0].key
        else:
            self.fields["supplier"].value = None
        try:
            self.fields["supplier"].update()
        except AssertionError:
            # Control not added to page yet
            pass

    def switch_view(self, e=None):
        # show parameters instead of part information
        parameters_view = self.fields["parameter_view"].value
        self.column.controls[0].content.controls = [
            ft.Row(),
            ft.Row(
                controls=[
                    self.fields["part_number"],
                    self.fields["supplier"],
                    self.fields["search_button"],
                    self.fields["parameter_view"],
                ],
            ),
            ft.Divider(),
        ]
        if not parameters_view:
            for _field, text_field in self.fields["search_form"].items():
                self.column.controls[0].content.controls.append(ft.Row([text_field]))
        else:
            for _field, text_field in self.fields["parameter_form"].items():
                self.column.controls[0].content.controls.append(ft.Row([text_field]))
        # Re-append embedded sub-view sections (compact layout only)
        if settings.COMPACT_LAYOUT and self._embedded_sections:
            self.column.controls[0].content.controls.append(ft.Divider(height=16))
            self.column.controls[0].content.controls.extend(self._embedded_sections)
        self._page.update()

    def perform_pn_search(self, e):
        self.run_search(e)
        try:
            self.fields["part_number"].focus()
        except AssertionError:
            pass

    def build_column(self):
        self.update_suppliers()
        # Enable search method
        self.fields["search_button"].on_click = self.run_search
        self.fields["parameter_view"].on_change = self.switch_view
        self.fields["part_number"].on_submit = self.perform_pn_search

        self.column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(),
                            ft.Row(
                                controls=[
                                    self.fields["part_number"],
                                    self.fields["supplier"],
                                    self.fields["search_button"],
                                    self.fields["parameter_view"],
                                ],
                            ),
                            ft.Divider(),
                        ],
                        scroll=ft.ScrollMode.HIDDEN,
                    ),
                    expand=True,
                ),
            ],
            alignment=ft.MainAxisAlignment.END,
            expand=True,
        )

        # Create search form
        for field in self.search_fields_list:
            label = field.replace("_", " ").title()
            text_field = ft.TextField(
                label=label,
                dense=True,
                hint_text=label,
                disabled=True,
                expand=True,
                on_change=self.push_data,
            )
            self.column.controls[0].content.controls.append(ft.Row([text_field]))
            self.fields["search_form"][field] = text_field

        # ---- Embedded sub-views (compact layout only) ----
        self._inventree_view = None
        self._kicad_view = None
        self._create_view = None
        self._embedded_sections = []

        if settings.COMPACT_LAYOUT:
            _SR_W = 180  # search field width inside DropdownWithSearch

            self._inventree_view = InventreeView(self._page)
            self._inventree_view.build_column()
            _compact_inventree_column(self._inventree_view, _SR_W)

            self._kicad_view = KicadView(self._page)
            self._kicad_view.build_column()
            _compact_kicad_column(self._kicad_view, _SR_W)

            self._create_view = CreateView(self._page)
            self._create_view.build_column()
            _compact_create_column(self._create_view)

            # When InvenTree or KiCad is toggled, also refresh the Create progress bars.
            def _wrap_enable(view):
                original_on_change = view.fields["enable"].on_change

                def _on_change_with_progress_reset(e):
                    if original_on_change:
                        original_on_change(e)
                    self._create_view.reset_progress_bars()

                view.fields["enable"].on_change = _on_change_with_progress_reset

            _wrap_enable(self._inventree_view)
            _wrap_enable(self._kicad_view)

            def _panel(title: str, icon, sub_col: ft.Column) -> ft.Column:
                return ft.Column(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Icon(icon, size=20),
                                ft.Text(
                                    title,
                                    style=ft.TextThemeStyle.TITLE_MEDIUM,
                                    weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=6,
                        ),
                        ft.Divider(height=6),
                        sub_col,
                    ],
                    spacing=4,
                    expand=True,
                )

            self._embedded_sections = [
                ft.Row(
                    controls=[
                        _panel(
                            "InvenTree",
                            ft.icons.INVENTORY_2,
                            self._inventree_view.column,
                        ),
                        ft.VerticalDivider(width=1),
                        _panel(
                            "KiCad",
                            ft.icons.SETTINGS_INPUT_COMPONENT,
                            self._kicad_view.column,
                        ),
                        ft.VerticalDivider(width=1),
                        _panel("Create", ft.icons.BUILD, self._create_view.column),
                    ],
                    spacing=12,
                    expand=True,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
            ]
            self.column.controls[0].content.controls.extend(
                [
                    ft.Divider(height=16),
                    *self._embedded_sections,
                ]
            )

    def did_mount(self, enable=False):
        if (
            not self.fields["part_number"].value
            and self.fields["supplier"].value is None
            and self.data.get("custom_part", None) is None
        ):
            self.show_dialog(
                d_type=DialogType.WARNING,
                message="To create a Custom Part click on the Submit button",
            )
        if settings.COMPACT_LAYOUT and self._inventree_view is not None:
            # Initialize embedded sub-views (populate dropdowns, apply enable flags).
            # Call process_enable directly to avoid ft.View lifecycle on non-mounted views.
            def _make_enable_event(view, enabled):
                return ft.ControlEvent(
                    target=None,
                    name="did_mount_enable",
                    data="true" if enabled else "false",
                    page=self._page,
                    control=view.fields["enable"],
                )

            self._inventree_view.process_enable(
                _make_enable_event(self._inventree_view, settings.ENABLE_INVENTREE)
            )
            self._kicad_view.process_enable(
                _make_enable_event(self._kicad_view, settings.ENABLE_KICAD)
            )
            self._create_view.reset_progress_bars()
        return super().did_mount()


class InventreeView(MainView):
    """InvenTree categories view"""

    title = "InvenTree"
    fields = {
        "enable": ft.Switch(
            label="InvenTree",
            value=settings.ENABLE_INVENTREE,
        ),
        "alternate": ft.Switch(
            label="Update existing",
            value=settings.ENABLE_ALTERNATE if settings.ENABLE_INVENTREE else False,
            disabled=not settings.ENABLE_INVENTREE,
        ),
        "load_categories": ft.ElevatedButton(
            "Reload InvenTree Categories",
            width=GUI_PARAMS["button_width"] * 2.6,
            height=36,
            icon=ft.icons.REPLAY,
            disabled=False,
        ),
        "load_stock_locations": ft.ElevatedButton(
            "Reload InvenTree Stock locations",
            width=GUI_PARAMS["button_width"] * 2.8,
            height=36,
            icon=ft.icons.REPLAY,
            disabled=False,
        ),
        "Category": DropdownWithSearch(
            label="Category",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            disabled=settings.ENABLE_ALTERNATE,
            options=[],
        ),
        "IPN: Category Code": ft.Dropdown(
            label="IPN: Category Code",
            width=GUI_PARAMS["textfield_width"] / 2 - 5,
            dense=GUI_PARAMS["textfield_dense"],
            # disabled=settings.CONFIG_IPN.get('IPN_CATEGORY_CODE', False),
            options=[],
        ),
        "Create New Code": SwitchWithRefs(
            label="Create New Code",
        ),
        "check_existing": ft.Switch(
            label="Check for existing Parts",
            value=settings.CHECK_EXISTING if settings.ENABLE_INVENTREE else False,
            disabled=not settings.ENABLE_INVENTREE,
        ),
        "New Category Code": ft.TextField(
            label="New Category Code",
            width=GUI_PARAMS["textfield_width"] / 2 - 5,
            dense=GUI_PARAMS["textfield_dense"],
            visible=False,
        ),
        "Existing Part ID": ft.TextField(
            label="Existing Part ID",
            width=GUI_PARAMS["textfield_width"] / 2 - 5,
            dense=GUI_PARAMS["textfield_dense"],
            visible=True,
        ),
        "Existing Part IPN": ft.TextField(
            label="Existing Part IPN",
            width=GUI_PARAMS["textfield_width"] / 2 - 5,
            dense=GUI_PARAMS["textfield_dense"],
            visible=True,
        ),
        "Update Parameter": SwitchWithRefs(
            label="Update Parameter",
            value=settings.UPDATE_INVENTREE if settings.ENABLE_INVENTREE else False,
            disabled=not settings.ENABLE_INVENTREE,
        ),
        "Create stock": SwitchWithRefs(
            label="Create Stock",
            disabled=not settings.ENABLE_INVENTREE,
        ),
        "Stock location": DropdownWithSearch(
            label="Stock Location (optional)",
            disabled=not settings.ENABLE_INVENTREE,
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        ),
        "Part barcode": ft.Checkbox(
            label="Assign barcode (supplier number → supplier part, manufacturer number → part)",
            disabled=not settings.ENABLE_INVENTREE,
            value=False,
        ),
        "Stock quantity": ft.TextField(
            label="Stock Quantity",
            disabled=not settings.ENABLE_INVENTREE,
            keyboard_type=ft.KeyboardType.NUMBER,
            value="1",
        ),
        "Make stock location default": ft.Checkbox(
            label="Set this location as the part's default location",
            disabled=not settings.ENABLE_INVENTREE,
            value=False,
        ),
    }

    def __init__(self, page: ft.Page):
        self.category_row_ref = ft.Ref[ft.Row]()
        self.ipncode_row_ref = ft.Ref[ft.Row]()
        self.alternate_row_ref = ft.Ref[ft.Row]()
        self.create_stock_widgets_ref = ft.Ref[ft.Row]()
        super().__init__(page)

    def partial_update(self):
        # Update IPN row
        self.process_ipncode()

    def sanitize_data(self):
        category_tree = self.data.get("Category", None)
        if category_tree:
            self.data["Category"] = inventree_interface.split_category_tree(category_tree)
        stock_location_tree = self.data.get("Stock location", None)
        if stock_location_tree:
            self.data["Stock location"] = inventree_interface.split_category_tree(
                stock_location_tree
            )

    def process_enable(self, e):
        inventree_enable = True
        # Switch control: override
        if e.data.lower() == "false":
            inventree_enable = False

        super().process_enable(e, value=inventree_enable, ignore=["enable", "IPN: Category Code"])
        if not inventree_enable:
            # If InvenTree disabled
            self.fields["alternate"].value = inventree_enable
            _safe_update(self.fields["alternate"])
            self.process_alternate(e, value=inventree_enable)
            self.process_create_stock(e, value=inventree_enable)
        else:
            alternate_enable = self.fields["alternate"].value
            self.process_alternate(e, value=alternate_enable)
            stock_create_enabled = self.fields["Create stock"].value
            self.process_create_stock(e, value=stock_create_enabled)

        self.process_ipncode()

    def process_alternate(self, e, value=None):
        if value is not None:
            alt_visible = value
        else:
            # Switch control
            # Reset view
            self.reset_view(e, ignore=["enable", "alternate"])
            self.fields["New Category Code"].visible = False
            # Get switch value
            alt_visible = False
            if e.data.lower() == "true":
                alt_visible = True

        # Load category button
        self.fields["load_categories"].disabled = alt_visible
        _safe_update(self.fields["load_categories"])

        # Category row visibility
        self.category_row_ref.current.visible = not alt_visible
        _safe_update(self.category_row_ref.current)

        # Alternate row visibility
        self.alternate_row_ref.current.visible = alt_visible
        _safe_update(self.alternate_row_ref.current)

        # Update settings
        settings.set_enable_flag("alternate", alt_visible)
        settings.set_enable_flag("update", alt_visible)
        # User dialog
        if alt_visible:
            self.show_dialog(
                d_type=DialogType.WARNING,
                message="Alternate Mode Enabled: Enter Existing Part ID or Part IPN",
            )

        self.push_data(e)

    def process_update(self, e, value=None):
        if value is not None:
            update_enabled = value
        else:
            # Get switch value
            update_enabled = False
            if e.data.lower() == "true":
                update_enabled = True
        settings.set_enable_flag("update", update_enabled)
        self.push_data(e)

    def process_button(self, e, value=None):
        if value is not None:
            button_enabled = value
        else:
            # Get switch value
            button_enabled = False
            if e.data.lower() == "true":
                button_enabled = True
        if e.control.label == "Update existing":
            settings.set_enable_flag("update", button_enabled)
        elif e.control.label == "Check for existing Parts":
            settings.set_enable_flag("check_existing", button_enabled)
        self.push_data(e)

    def process_category(self, e=None, label=None, value=None):
        parent_category = None
        if isinstance(self.fields["Category"].value, str):
            parent_category = inventree_interface.split_category_tree(
                self.fields["Category"].value
            )[0]
        # Check for category codes
        options = self.get_code_options()
        if options:
            self.fields["IPN: Category Code"].options = options
            # Select category code corresponding to selected category
            code = config_interface.load_file(settings.CONFIG_CATEGORIES)["CODES"].get(
                parent_category, None
            )
            if code and not self.fields["Create New Code"].value:
                self.fields["IPN: Category Code"].value = code
            _safe_update(self.fields["IPN: Category Code"])
        self.push_data(e)

    def process_location(self, e=None, label=None, value=None):
        self.fields["Stock location"].options = self.get_stock_location_options()
        self.push_data(e)

    def process_ipncode(self):
        ipncode_enable = bool(
            settings.CONFIG_IPN.get("IPN_ENABLE_CREATE", False)
            and settings.CONFIG_IPN.get("IPN_CATEGORY_CODE", False)
        )
        self.ipncode_row_ref.current.visible = ipncode_enable
        _safe_update(self.ipncode_row_ref.current)

    def process_create_stock(self, e, value=None):
        if value is not None:
            create_stock_visible = value
        else:
            self.fields["New Category Code"].visible = False
            # Get switch value
            create_stock_visible = False
            if e.data.lower() == "true":
                create_stock_visible = True

        # Stock create row visibility
        self.create_stock_widgets_ref.current.visible = create_stock_visible
        _safe_update(self.create_stock_widgets_ref.current)

    def get_code_options(self):
        try:
            return [
                ft.dropdown.Option(code)
                for code in config_interface.load_file(settings.CONFIG_CATEGORIES)["CODES"].values()
            ]
        except AttributeError:
            return []

    def get_category_options(self, reload=False):
        if reload:
            inventree_interface.reload_category_cache()
        return [
            ft.dropdown.Option(category)
            for category in inventree_interface.get_cached_category_tree()
        ]

    def get_stock_location_options(self, reload=False):
        if reload:
            inventree_interface.reload_location_cache()
        return [
            ft.dropdown.Option(location)
            for location in inventree_interface.get_cached_location_tree()
        ]

    def reload_categories(self, e):
        self._page.splash.visible = True
        self._page.update()

        # Check connection
        if not inventree_interface.connect_to_server():
            self.show_dialog(DialogType.ERROR, "ERROR: Failed to connect to InvenTree server")
        else:
            self.fields["Category"].options = self.get_category_options(reload=True)
            self.fields["Category"].update()

        self._page.splash.visible = False
        self._page.update()

    def reload_stock_locations(self, e):
        self._page.splash.visible = True
        self._page.update()

        # Check connection
        if not inventree_interface.connect_to_server():
            self.show_dialog(DialogType.ERROR, "ERROR: Failed to connect to InvenTree server")
        else:
            self.fields["Stock location"].options = self.get_stock_location_options(reload=True)
            self.fields["Stock location"].update()

        self._page.splash.visible = False
        self._page.update()

    def create_ipn_code(self, e):
        # Get switch value
        new_code = True
        if e.data.lower() == "false":
            new_code = False

        self.fields["IPN: Category Code"].disabled = new_code
        self.fields["IPN: Category Code"].update()
        if not new_code:
            self.process_category()
        else:
            self.push_data(e)

    def build_column(self):
        # Update dropdown with category options
        self.fields["Category"].options = self.get_category_options()
        self.fields["Category"].on_change = self.process_category
        self.fields["load_categories"].on_click = self.reload_categories
        # Category codes
        self.fields["IPN: Category Code"].options = self.get_code_options()
        self.fields["IPN: Category Code"].on_change = self.push_data
        self.fields["Create New Code"].on_change = self.create_ipn_code
        self.fields["New Category Code"].on_change = self.push_data
        # Other Settings
        self.fields["check_existing"].on_change = self.process_button
        # Alternate fields
        self.fields["alternate"].on_change = self.process_alternate
        self.fields["Existing Part ID"].on_change = self.push_data
        self.fields["Existing Part IPN"].on_change = self.push_data
        self.fields["Update Parameter"].on_change = self.process_update
        # Create stock location
        self.fields["Stock location"].options = self.get_stock_location_options()
        self.fields["Stock location"].on_change = self.push_data
        self.fields["load_stock_locations"].on_click = self.reload_stock_locations
        self.fields["Part barcode"].on_change = self.push_data
        self.fields["Create stock"].on_change = self.process_create_stock
        self.fields["Stock quantity"].on_change = self.push_data
        self.fields["Make stock location default"].on_change = self.push_data

        self.column = ft.Column(
            controls=[
                ft.Row(),
                ft.Row(
                    [
                        self.fields["enable"],
                        self.fields["alternate"],
                        self.fields["load_categories"],
                    ],
                    width=GUI_PARAMS["dropdown_width"],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Row(
                    ref=self.category_row_ref,
                    controls=[
                        ft.Column(
                            [
                                ft.Row(
                                    [
                                        self.fields["Category"],
                                    ]
                                ),
                                ft.Row(
                                    ref=self.ipncode_row_ref,
                                    controls=[
                                        ft.Column(
                                            [
                                                ft.Row(
                                                    [
                                                        self.fields["IPN: Category Code"],
                                                        self.fields["Create New Code"],
                                                    ]
                                                ),
                                                ft.Row([self.fields["New Category Code"]]),
                                            ],
                                        ),
                                    ],
                                ),
                                ft.Row(
                                    [
                                        self.fields["check_existing"],
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Column(
                    ref=self.alternate_row_ref,
                    controls=[
                        ft.Row(
                            controls=[
                                self.fields["Existing Part ID"],
                                self.fields["Existing Part IPN"],
                            ],
                        ),
                        ft.Row(controls=[self.fields["Update Parameter"]]),
                    ],
                ),
                ft.Column(
                    ref=self.create_stock_widgets_ref,
                    controls=[ft.Row(controls=[self.fields["Create stock"]])],
                ),
                ft.Row(
                    controls=[
                        self.fields["Stock location"],
                    ],
                ),
                ft.Row(
                    controls=[
                        self.fields["load_stock_locations"],
                    ],
                ),
                ft.Row(
                    controls=[
                        self.fields["Part barcode"],
                    ],
                    width=GUI_PARAMS["dropdown_width"],
                ),
                ft.Column(
                    ref=self.create_stock_widgets_ref,
                    controls=[
                        ft.Row(
                            controls=[self.fields["Stock quantity"]],
                        ),
                        ft.Row(
                            controls=[self.fields["Make stock location default"]],
                        ),
                    ],
                ),
            ],
        )

        # Connect New Category Code fields
        cc_ref = ft.Ref[ft.TextField]()
        cc_ref.current = self.fields["New Category Code"]
        self.fields["Create New Code"].refs = [cc_ref]

    def did_mount(self):
        return super().did_mount(enable=settings.ENABLE_INVENTREE)


class KicadView(MainView):
    """KiCad view"""

    title = "KiCad"
    fields = {
        "enable": ft.Switch(
            label="KiCad",
            value=settings.ENABLE_KICAD,
        ),
        "Symbol Library": DropdownWithSearch(
            label="",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        ),
        "Symbol Template": DropdownWithSearch(
            label="",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        ),
        "Footprint Library": DropdownWithSearch(
            label="",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        ),
        "Footprint": DropdownWithSearch(
            label="",
            dr_width=GUI_PARAMS["textfield_width"],
            sr_width=GUI_PARAMS["searchfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            options=[],
        ),
        "New Footprint": SwitchWithRefs(
            label="New Footprint",
        ),
        "New Footprint Name": ft.TextField(
            label="New Footprint Name",
            width=GUI_PARAMS["textfield_width"],
            dense=GUI_PARAMS["textfield_dense"],
            visible=False,
        ),
        "Check SnapEDA": ft.ElevatedButton(
            content=ft.Row(
                [
                    ft.Icon("search"),
                    ft.Text("Check SnapEDA", size=16),
                ]
            ),
            height=GUI_PARAMS["button_height"],
            width=GUI_PARAMS["button_width"] * 2,
        ),
    }

    def build_alert_dialog(self, symbol: str, footprint: str, download: str, single_result=False):
        modal_content = ft.Row()
        modal_msg = ft.Text("Symbol and footprint are not available on SnapEDA")
        # Build content
        if symbol:
            modal_content.controls.append(ft.Image(symbol))
            modal_msg = ft.Text("Symbol is available on SnapEDA")
        if footprint:
            modal_content.controls.append(ft.Image(footprint))
            if symbol:
                modal_msg = ft.Text("Symbol and footprint are available on SnapEDA")
            else:
                modal_msg = ft.Text("Footprint is available on SnapEDA")
        # Build actions
        modal_actions = []
        if download:
            if not symbol and not footprint:
                if single_result:
                    modal_actions.append(
                        ft.TextButton(
                            "Check Part",
                            on_click=lambda _: self._page.launch_url(download),
                        )
                    )
                else:
                    modal_msg = ft.Text("Multiple matches found on SnapEDA")
                    modal_actions.append(
                        ft.TextButton(
                            "See Results",
                            on_click=lambda _: self._page.launch_url(download),
                        )
                    )
            else:
                modal_actions.append(
                    ft.TextButton("Download", on_click=lambda _: self._page.launch_url(download))
                )
        modal_actions.append(
            ft.TextButton("Close", on_click=lambda _: self.show_dialog(open=False))
        )

        return ft.AlertDialog(
            modal=True,
            title=modal_msg,
            content=modal_content,
            actions=modal_actions,
            actions_alignment=ft.MainAxisAlignment.END,
            # on_dismiss=None,
        )

    def process_enable(self, e, value=None, ignore=None):
        if ignore is None:
            ignore = ["enable"]
        super().process_enable(e, value, ignore)
        if self.fields["enable"].value:
            self.fields["Footprint"].disabled = self.fields["New Footprint"].value
            _safe_update(self.fields["Footprint"])

    def push_data(self, e=None, label=None, value=None, **_):
        super().push_data(e)
        if label or e:
            try:
                if "Footprint Library" in [label, e.control.label]:
                    if value:
                        selected_footprint_library = value
                    else:
                        selected_footprint_library = e.data
                    self.update_footprint_options(selected_footprint_library)
            except AttributeError:
                # Handles condition where search field tries to reset dropdown
                pass

    def check_snapeda(self, e):
        if not data_from_views.get("Part Search", {}).get("manufacturer_part_number", ""):
            self.show_dialog(
                d_type=DialogType.ERROR,
                message="Missing Manufacturer Part Number",
            )
            return

        self._page.splash.visible = True
        self._page.update()

        response = snapeda_api.fetch_snapeda_part_info(
            data_from_views["Part Search"]["manufacturer_part_number"]
        )
        data = snapeda_api.parse_snapeda_response(response)

        images = {}
        if data["has_symbol"] or data["has_footprint"]:
            images = snapeda_api.download_snapeda_images(data)

        self._page.splash.visible = False
        self._page.update()

        self.dialog = self.build_alert_dialog(
            images.get("symbol", ""),
            images.get("footprint", ""),
            data.get("part_url", ""),
            data.get("has_single_result", False),
        )
        self.show_dialog(snackbar=False, open=True)

    def update_footprint_options(self, library: str):
        footprint_options = []
        if library is None:
            return footprint_options

        # Load paths
        footprint_paths = self.get_footprint_libraries()
        # Get path matching selected footprint library
        footprint_lib_path = footprint_paths[library]
        # Load footprints
        footprints = [
            item.replace(".kicad_mod", "")
            for item in sorted(os.listdir(footprint_lib_path))
            if os.path.isfile(os.path.join(footprint_lib_path, item))
        ]
        # Find folder matching value
        for footprint in footprints:
            footprint_options.append(ft.dropdown.Option(footprint))

        self.fields["Footprint"].options = footprint_options
        self.fields["Footprint"].update()

    def get_footprint_libraries(self) -> dict:
        footprint_libraries = {}
        try:
            for folder in sorted(os.listdir(settings.KICAD_SETTINGS["KICAD_FOOTPRINTS_PATH"])):
                if os.path.isdir(
                    os.path.join(settings.KICAD_SETTINGS["KICAD_FOOTPRINTS_PATH"], folder)
                ):
                    footprint_libraries[folder.replace(".pretty", "")] = os.path.join(
                        settings.KICAD_SETTINGS["KICAD_FOOTPRINTS_PATH"], folder
                    )
        except FileNotFoundError:
            pass
        return footprint_libraries

    def find_libraries(self, type: str) -> list:
        found_libraries = []
        if type == "symbol":
            try:
                found_libraries = [
                    file.replace(".kicad_sym", "")
                    for file in sorted(os.listdir(settings.KICAD_SETTINGS["KICAD_SYMBOLS_PATH"]))
                    if file.endswith(".kicad_sym")
                ]
            except FileNotFoundError:
                pass
        elif type == "template":
            templates = config_interface.load_templates_paths(
                user_config_path=settings.KICAD_CONFIG_CATEGORY_MAP,
                template_path=settings.KICAD_SETTINGS["KICAD_TEMPLATES_PATH"],
            )
            for key in templates:
                for template in templates[key]:
                    found_libraries.append(f"{key}/{template}")
        elif type == "footprint":
            found_libraries = list(self.get_footprint_libraries().keys())
        return found_libraries

    def build_library_options(self, type: str):
        options = []
        found_libraries = self.find_libraries(type)
        if found_libraries:
            options = [ft.dropdown.Option(lib_name) for lib_name in found_libraries]
        return options

    def create_footprint(self, e):
        # Get switch value
        new_footprint = True
        if e.data.lower() == "false":
            new_footprint = False

        self.fields["Footprint"].disabled = new_footprint
        self.fields["Footprint"].update()
        if not new_footprint:
            self.update_footprint_options(self.fields["Footprint Library"].value)
        self.push_data(e)

    def build_column(self):
        # Library options checks
        self.checks = []

        self.column = ft.Column(
            controls=[ft.Row()],
            alignment=ft.MainAxisAlignment.START,
            expand=True,
        )
        kicad_inputs = []
        for name, field in self.fields.items():
            # Update callbacks
            if isinstance(field, ft.ElevatedButton):
                field.on_click = self.check_snapeda
            # Update options
            elif isinstance(field, DropdownWithSearch):
                field.label = name
                if name == "Symbol Library":
                    field.options = self.build_library_options(type="symbol")
                elif name == "Symbol Template":
                    field.options = self.build_library_options(type="template")
                elif name == "Footprint Library":
                    field.options = self.build_library_options(type="footprint")
                if not field.options and name != "Footprint":
                    self.checks.append(f"KiCad {name} path does not exists or folder is empty")

            if name != "enable":
                field.on_change = self.push_data

            kicad_inputs.append(field)

        self.column.controls.extend(kicad_inputs)

        # Connect New Footprint fields
        fp_ref = ft.Ref[ft.TextField]()
        fp_ref.current = self.fields["New Footprint Name"]
        self.fields["New Footprint"].refs = [fp_ref]
        self.fields["New Footprint"].on_change = self.create_footprint

    def did_mount(self):
        if "InvenTree" in data_from_views:
            # Get value of alternate switch
            if data_from_views["InvenTree"].get("alternate", False):
                self.fields["enable"].disabled = True
                self.fields["enable"].value = False
                self.show_dialog(
                    d_type=DialogType.ERROR,
                    message="InvenTree Alternate switch is enabled",
                )
                return super().did_mount(enable=False)
            else:
                self.fields["enable"].disabled = False

        # Process checks
        if self.checks:
            error_msg = f"{self.checks[0]}"
            for check in self.checks[1:]:
                error_msg += f"\n{check}"
            self.show_dialog(
                d_type=DialogType.ERROR,
                message=error_msg,
            )

        return super().did_mount(enable=settings.ENABLE_KICAD)


class CreateView(MainView):
    """Create view"""

    title = "Create"
    fields = {
        "inventree_progress": ft.ProgressBar(height=32, width=420, value=0),
        "kicad_progress": ft.ProgressBar(height=32, width=420, value=0),
        "bulk_status": ft.Text(value="Bulk import idle", size=16),
        "bulk_excel_path": ft.TextField(
            label="Bulk Excel File",
            width=440,
            dense=True,
            read_only=True,
            hint_text="Columns: search_name, supplier, inventree_category, [location], [barcode], [ipn], [create_stock], [stock_quantity], [make_default]",
        ),
        "bulk_excel_pick": ft.ElevatedButton(
            content=ft.Row(
                [
                    ft.Icon(ft.icons.UPLOAD_FILE),
                    ft.Text("Select Excel", size=16),
                ]
            ),
            height=GUI_PARAMS["button_height"],
            width=GUI_PARAMS["button_width"] * 1.8,
        ),
        "bulk_import": ft.ElevatedButton(
            content=ft.Row(
                [
                    ft.Icon(ft.icons.PLAYLIST_ADD_CHECK_CIRCLE),
                    ft.Text("Bulk Add From Excel", size=16),
                ]
            ),
            height=GUI_PARAMS["button_height"],
            width=GUI_PARAMS["button_width"] * 2.2,
        ),
        "create": ft.ElevatedButton(
            content=ft.Row(
                [
                    ft.Icon("build_circle"),
                    ft.Text("Create Part", size=20),
                    ft.Icon("build_circle"),
                ]
            ),
            height=GUI_PARAMS["button_height"],
            width=GUI_PARAMS["button_width"] * 2,
        ),
        "cancel": ft.ElevatedButton(
            content=ft.Row(
                [
                    ft.Icon("highlight_remove"),
                    ft.Text("Cancel", size=20),
                    ft.Icon("highlight_remove"),
                ]
            ),
            height=GUI_PARAMS["button_height"],
            width=GUI_PARAMS["button_width"] * 1.6,
            bgcolor=ft.colors.RED_50,
            disabled=True,
        ),
    }
    inventree_progress_row = None
    kicad_progress_row = None
    create_continue = True
    bulk_picker = None

    def __init__(self, page: ft.Page):
        self._stock_location_id_map = {}
        self._stock_location_pk_cache = {}
        super().__init__(page)

    def _resolve_stock_location_pk(self, location_value) -> int:
        if not location_value:
            return -1

        if isinstance(location_value, list):
            normalized_location = "/".join(
                str(segment).strip() for segment in location_value if str(segment).strip()
            )
        else:
            normalized_location = str(location_value).strip()

        if not normalized_location:
            return -1

        if normalized_location in self._stock_location_pk_cache:
            return int(self._stock_location_pk_cache[normalized_location])

        if not self._stock_location_id_map:
            self._stock_location_id_map = inventree_interface.get_cached_location_id_map()

        location_pk = inventree_interface.resolve_stock_location_pk(
            normalized_location,
            self._stock_location_id_map,
        )
        self._stock_location_pk_cache[normalized_location] = int(location_pk)
        return int(location_pk)

    @staticmethod
    def _normalize_header(value):
        if value is None:
            return ""
        header = str(value).strip().lower()
        for old, new in [("-", "_"), (" ", "_"), ("/", "_"), ("\\", "_")]:
            header = header.replace(old, new)
        return header

    @staticmethod
    def _resolve_bulk_supplier_key(supplier_value: str):
        supplier_name = inventree_interface.get_supplier_name(supplier_value)
        if supplier_name in settings.CONFIG_SUPPLIERS:
            return supplier_name

        needle = str(supplier_value or "").strip().lower()
        for key, value in settings.CONFIG_SUPPLIERS.items():
            display_name = str(value.get("name", "")).strip().lower()
            if needle in {key.strip().lower(), display_name}:
                return key
        return None

    @staticmethod
    def _normalize_location_tree(location_value):
        if not location_value:
            return None

        if isinstance(location_value, list):
            tree = [str(segment).strip() for segment in location_value if str(segment).strip()]
            return tree or None

        text = str(location_value).strip()
        if not text:
            return None

        tree = [
            segment.strip()
            for segment in inventree_interface.split_category_tree(text)
            if str(segment).strip()
        ]
        return tree or None

    @staticmethod
    def _post_process_part(
        part_pk: int,
        location_tree=None,
        create_stock_enabled: bool = False,
        assign_barcode: bool = False,
        part_info: dict = None,
    ):
        if not part_pk:
            return

        if location_tree and not create_stock_enabled:
            inventree_interface.inventree_set_part_default_location(part_pk, location_tree)

        if assign_barcode and part_info:
            manufacturer_pn = str(part_info.get("manufacturer_part_number") or "").strip()
            supplier_pn = str(part_info.get("supplier_part_number") or "").strip()
            # Assign supplier number to supplier part barcode, manufacturer number to part barcode.
            # Use manufacturer_pn as the part barcode (same logic as barcode import page).
            barcode_target = manufacturer_pn or supplier_pn
            if barcode_target:
                # Fetch existing barcodes and skip if already assigned.
                try:
                    api_obj = getattr(inventree_interface.inventree_api, "inventree_api", None)
                    current_barcodes = []
                    if api_obj:
                        try:
                            response = api_obj.get("/api/barcode/", params={"part": part_pk})
                            if response and hasattr(response, "json"):
                                data = response.json()
                                current_barcodes = [
                                    str(
                                        item.get("barcode_data") or item.get("barcode") or ""
                                    ).strip()
                                    for item in (
                                        data if isinstance(data, list) else data.get("results", [])
                                    )
                                ]
                            elif isinstance(response, list):
                                current_barcodes = [
                                    str(item.get("barcode_data") or "").strip() for item in response
                                ]
                        except Exception:
                            import logging

                            logging.exception(
                                "Exception fetching current barcodes in _import_barcode:"
                            )
                    current_barcodes_normalized = {b.lower() for b in current_barcodes if b}
                    if barcode_target.lower() in current_barcodes_normalized:
                        cprint(
                            f"[MAIN]\tBarcode already assigned to part {part_pk}, skipping",
                            silent=settings.SILENT,
                        )
                    else:
                        barcode_ok = False
                        for attempt in range(1, 4):
                            try:
                                barcode_ok = inventree_interface.inventree_link_part_barcode(
                                    part_pk, barcode_target
                                )
                                if barcode_ok:
                                    break
                            except Exception as exc:
                                cprint(
                                    f"[MAIN]\tBarcode link attempt {attempt} failed: {str(exc)[:60]}",
                                    silent=False,
                                )
                            if attempt < 3:
                                time.sleep(0.5)
                        if not barcode_ok:
                            cprint(
                                f"[MAIN]\tBarcode assignment failed for part {part_pk} after retries",
                                silent=False,
                            )
                except Exception as exc:
                    cprint(
                        f"[MAIN]\tBarcode post-process error: {str(exc)[:80]}",
                        silent=False,
                    )

    @staticmethod
    def _parse_optional_bool(value):
        if value is None:
            return True, None

        text = str(value).strip().lower()
        if text == "":
            return True, None

        true_values = {"1", "true", "yes", "y", "on"}
        false_values = {"0", "false", "no", "n", "off"}

        if text in true_values:
            return True, True
        if text in false_values:
            return True, False
        return False, None

    def _parse_bulk_excel_rows(self, file_path: str):
        try:
            load_workbook = import_module("openpyxl").load_workbook
        except ModuleNotFoundError as exc:
            raise RuntimeError("openpyxl is required for Excel import") from exc

        workbook = load_workbook(filename=file_path, data_only=True)
        sheet = workbook.active

        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return [], ["Excel file is empty"]

        header = [self._normalize_header(cell) for cell in rows[0]]

        search_aliases = {
            "search_name",
            "name_to_search",
            "search",
            "part_number",
            "supplier_part_number",
            "mpn",
            "name",
        }
        supplier_aliases = {"supplier", "supplier_name"}
        category_aliases = {"inventree_category", "category", "category_tree"}
        location_aliases = {"location", "stock_location", "inventree_location"}
        barcode_aliases = {"barcode", "part_barcode"}
        ipn_aliases = {"ipn", "ipn_code", "category_code", "ipn_category_code"}
        create_stock_aliases = {"create_stock", "stock", "add_stock"}
        stock_quantity_aliases = {"stock_quantity", "quantity", "qty"}
        make_default_aliases = {
            "make_default",
            "stock_make_default",
            "make_stock_location_default",
        }

        search_idx = next((i for i, h in enumerate(header) if h in search_aliases), None)
        supplier_idx = next((i for i, h in enumerate(header) if h in supplier_aliases), None)
        category_idx = next((i for i, h in enumerate(header) if h in category_aliases), None)
        location_idx = next((i for i, h in enumerate(header) if h in location_aliases), None)
        barcode_idx = next((i for i, h in enumerate(header) if h in barcode_aliases), None)
        ipn_idx = next((i for i, h in enumerate(header) if h in ipn_aliases), None)
        create_stock_idx = next(
            (i for i, h in enumerate(header) if h in create_stock_aliases), None
        )
        stock_quantity_idx = next(
            (i for i, h in enumerate(header) if h in stock_quantity_aliases), None
        )
        make_default_idx = next(
            (i for i, h in enumerate(header) if h in make_default_aliases), None
        )

        data_start_row = 1

        # If headers are not found, fallback to 3-column mode:
        # col A = search_name, col B = supplier, col C = inventree_category
        if search_idx is None or supplier_idx is None or category_idx is None:
            first_row = rows[0]
            if len(first_row) >= 3 and any(
                cell is not None and str(cell).strip() for cell in first_row[:3]
            ):
                search_idx, supplier_idx, category_idx = 0, 1, 2
                location_idx = 3 if len(first_row) >= 4 else None
                barcode_idx = 4 if len(first_row) >= 5 else None
                ipn_idx = 5 if len(first_row) >= 6 else None
                create_stock_idx = 6 if len(first_row) >= 7 else None
                stock_quantity_idx = 7 if len(first_row) >= 8 else None
                make_default_idx = 8 if len(first_row) >= 9 else None
                data_start_row = 0
            else:
                return [], [
                    "Missing required columns. Expected headers: search_name, supplier, inventree_category",
                    "Optional columns: location, barcode, ipn, create_stock, stock_quantity, make_default",
                    "Or provide 3-9 columns without headers in this order: search_name | supplier | inventree_category | [location] | [barcode] | [ipn] | [create_stock] | [stock_quantity] | [make_default]",
                    f"Detected first row: {header}",
                ]

        parsed_rows = []
        errors = []

        def value_at(row_data, index):
            if index is None:
                return ""
            if index >= len(row_data):
                return ""
            cell = row_data[index]
            return str(cell).strip() if cell is not None else ""

        for excel_row_index, row in enumerate(rows[data_start_row:], start=(data_start_row + 1)):
            search_value = value_at(row, search_idx)
            supplier_value = value_at(row, supplier_idx)
            category_value = value_at(row, category_idx)
            location_value = value_at(row, location_idx)
            barcode_value = value_at(row, barcode_idx)
            ipn_value = value_at(row, ipn_idx)
            create_stock_raw = value_at(row, create_stock_idx)
            stock_quantity_value = value_at(row, stock_quantity_idx)
            make_default_raw = value_at(row, make_default_idx)

            if not search_value and not supplier_value and not category_value:
                continue

            if not search_value or not supplier_value or not category_value:
                errors.append(f"Row {excel_row_index}: missing one of required values")
                continue

            ok_create_stock, create_stock_value = self._parse_optional_bool(create_stock_raw)
            if not ok_create_stock:
                errors.append(
                    f"Row {excel_row_index}: invalid create_stock value '{create_stock_raw}'"
                )
                continue

            ok_make_default, make_default_value = self._parse_optional_bool(make_default_raw)
            if not ok_make_default:
                errors.append(
                    f"Row {excel_row_index}: invalid make_default value '{make_default_raw}'"
                )
                continue

            parsed_rows.append(
                {
                    "search_name": search_value,
                    "supplier": supplier_value,
                    "inventree_category": category_value,
                    "location": location_value,
                    "barcode": barcode_value,
                    "ipn": ipn_value,
                    "create_stock": create_stock_value,
                    "stock_quantity": stock_quantity_value,
                    "make_default": make_default_value,
                    "excel_row": excel_row_index,
                }
            )

        return parsed_rows, errors

    def _on_bulk_dialog_result(self, e: ft.FilePickerResultEvent):
        if e.files:
            picked = e.files[0].path
            self.fields["bulk_excel_path"].value = picked
            self._page.update()

    def _pick_bulk_excel(self, _):
        if self._page.overlay:
            self._page.overlay.pop()
        self.bulk_picker = ft.FilePicker(on_result=self._on_bulk_dialog_result)
        self._page.overlay.append(self.bulk_picker)
        self._page.update()

        initial_dir = settings.HOME_DIR
        if self.fields["bulk_excel_path"].value:
            initial_dir = os.path.dirname(self.fields["bulk_excel_path"].value)

        self.bulk_picker.pick_files(
            dialog_title="Select Excel for Bulk Add",
            initial_directory=initial_dir,
            allowed_extensions=["xlsx", "xlsm", "xltx", "xltm"],
            allow_multiple=False,
        )

    def _bulk_create_from_excel(self, _):
        file_path = self.fields["bulk_excel_path"].value
        if not file_path:
            self.show_dialog(DialogType.ERROR, "Select an Excel file first")
            return

        if not os.path.isfile(file_path):
            self.show_dialog(DialogType.ERROR, f"Excel file not found: {file_path}")
            return

        try:
            rows, parse_errors = self._parse_bulk_excel_rows(file_path)
        except Exception as exc:
            self.show_dialog(DialogType.ERROR, f"Failed to read Excel file: {exc}")
            return

        if not rows:
            self.show_dialog(DialogType.ERROR, "No valid rows found in Excel file")
            return

        if not inventree_interface.connect_to_server():
            self.show_dialog(DialogType.ERROR, "ERROR: Failed to connect to InvenTree server")
            return

        self._stock_location_id_map = inventree_interface.get_cached_location_id_map()
        self._stock_location_pk_cache = {}

        self.reset_progress_bars()
        self.enable_create(False)

        total = len(rows)
        success = 0
        failed = 0
        failures = []
        inv_data = data_from_views.get("InvenTree", {})

        progress.reset_progress_bar(self.fields["inventree_progress"])
        self.fields["bulk_status"].value = f"Preparing bulk import: 0/{total}"
        self.fields["inventree_progress"].update()
        self.fields["bulk_status"].update()

        category_codes_cfg = config_interface.load_file(settings.CONFIG_CATEGORIES).get("CODES", {})
        existing_category_codes = {
            str(code).strip() for code in category_codes_cfg.values() if str(code).strip()
        }

        location_map = dict(self._stock_location_id_map or {})

        def _process_bulk_row(idx: int, row_data: dict) -> dict:
            row_start_ts = time.perf_counter()
            result = {
                "idx": idx,
                "row": row_data,
                "ok": False,
                "part_pk": 0,
                "failure": "",
            }

            if not self.create_continue:
                result["failure"] = "Cancelled"
                return result

            cprint(
                f"[BULK]\tProcessing row {idx}/{total} | search='{row_data['search_name']}' | supplier='{row_data['supplier']}'",
                silent=settings.SILENT,
            )

            supplier_name = self._resolve_bulk_supplier_key(row_data["supplier"])
            if not supplier_name:
                result["failure"] = (
                    f"Row {row_data['excel_row']}: unknown supplier '{row_data['supplier']}'"
                )
                return result

            supplier_data = inventree_interface.supplier_search(
                supplier=supplier_name,
                part_number=row_data["search_name"],
            )
            if not supplier_data:
                result["failure"] = (
                    f"Row {row_data['excel_row']}: supplier search failed for '{row_data['search_name']}'"
                )
                return result

            part_form = inventree_interface.translate_supplier_to_form(
                supplier=supplier_name,
                part_info=supplier_data,
            )

            if not part_form.get("name"):
                part_form["name"] = row_data["search_name"]
            if not part_form.get("description"):
                part_form["description"] = row_data["search_name"]

            part_form["category_tree"] = [
                segment.strip()
                for segment in inventree_interface.split_category_tree(
                    row_data["inventree_category"]
                )
                if str(segment).strip()
            ]

            if settings.CONFIG_IPN.get("IPN_CATEGORY_CODE", False):
                ipn_code = str(row_data.get("ipn", "") or "").strip()
                if ipn_code:
                    part_form["category_code"] = ipn_code
                    if ipn_code in existing_category_codes:
                        cprint(
                            f"[BULK]\tRow {row_data['excel_row']}: using existing category code '{ipn_code}'",
                            silent=settings.SILENT,
                        )
                    else:
                        cprint(
                            f"[BULK]\tRow {row_data['excel_row']}: using new category code '{ipn_code}'",
                            silent=settings.SILENT,
                        )
                else:
                    if inv_data.get("Create New Code", False):
                        part_form["category_code"] = inv_data.get("New Category Code", "")
                    else:
                        part_form["category_code"] = inv_data.get("IPN: Category Code", "")

            location_text = str(row_data.get("location", "") or "").strip()
            if not location_text:
                location_text = inv_data.get("Stock location", "")

            location_tree = self._normalize_location_tree(location_text)

            create_stock_enabled = bool(inv_data.get("Create stock", False))
            if row_data.get("create_stock") is not None:
                create_stock_enabled = bool(row_data.get("create_stock"))

            stock_quantity = str(row_data.get("stock_quantity", "") or "").strip()
            if not stock_quantity:
                stock_quantity = inv_data.get("Stock quantity", "1")

            make_default = inv_data.get("Make stock location default", False)
            if row_data.get("make_default") is not None:
                make_default = bool(row_data.get("make_default"))

            stock_payload = None
            if create_stock_enabled and not location_tree:
                result["failure"] = (
                    f"Row {row_data['excel_row']}: create_stock is enabled but no stock location provided"
                )
                return result

            if create_stock_enabled and location_tree:
                ts_loc_resolve = time.perf_counter()
                normalized_location = "/".join(
                    str(segment).strip() for segment in location_tree if str(segment).strip()
                )
                location_pk = inventree_interface.resolve_stock_location_pk(
                    normalized_location, location_map
                )
                elapsed_loc_resolve = (time.perf_counter() - ts_loc_resolve) * 1000.0
                cprint(
                    f"[BULK]\tRow {row_data['excel_row']}: location resolve ({elapsed_loc_resolve:.1f} ms)",
                    silent=settings.SILENT,
                )
                if location_pk <= 0:
                    result["failure"] = (
                        f"Row {row_data['excel_row']}: stock location not found "
                        f"'{row_data.get('location') or inv_data.get('Stock location', '')}'"
                    )
                    return result

                stock_payload = {
                    "location": location_pk,
                    "quantity": stock_quantity,
                    "make_default": make_default,
                }

            new_part, part_pk, _ = inventree_interface.inventree_create(
                part_info=part_form,
                kicad=False,
                show_progress=False,
                is_custom=False,
                stock=stock_payload,
            )

            elapsed_row = (time.perf_counter() - row_start_ts) * 1000.0
            cprint(
                f"[BULK]\tRow {row_data['excel_row']} total: {elapsed_row:.1f} ms",
                silent=settings.SILENT,
            )

            if not part_pk:
                result["failure"] = (
                    f"Row {row_data['excel_row']}: failed to create '{row_data['search_name']}' "
                    f"in category '{row_data['inventree_category']}'"
                )
                return result

            barcode_value = str(row_data.get("barcode", "") or "").strip()
            if barcode_value:
                # Explicit barcode column in the Excel sheet — assign directly.
                try:
                    inventree_interface.inventree_link_part_barcode(part_pk, barcode_value)
                except Exception as exc:
                    cprint(
                        f"[BULK]\tBarcode link failed for row {row_data['excel_row']}: {str(exc)[:60]}",
                        silent=False,
                    )

            self._post_process_part(
                part_pk=part_pk,
                location_tree=location_tree,
                create_stock_enabled=create_stock_enabled,
            )

            result["ok"] = True
            result["part_pk"] = int(part_pk)
            return result

        max_workers = min(4, max(1, total))
        cprint(f"[BULK]\tParallel row workers: {max_workers}", silent=settings.SILENT)

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_process_bulk_row, idx, row): (idx, row)
                for idx, row in enumerate(rows, start=1)
            }

            for future in as_completed(future_map):
                if not self.create_continue:
                    self.enable_create(True)
                    return self.process_cancel()

                result = future.result()
                row = result["row"]
                if result["ok"]:
                    success += 1
                    cprint(
                        f"[BULK]\tRow {row['excel_row']} created successfully (part_pk={result['part_pk']})",
                        silent=settings.SILENT,
                    )
                else:
                    if result["failure"] and result["failure"] != "Cancelled":
                        failed += 1
                        failures.append(result["failure"])

                completed += 1
                self.fields["inventree_progress"].value = completed / total
                self.fields[
                    "bulk_status"
                ].value = f"Processing row {completed}/{total} | success={success} failed={failed}"
                self.fields["inventree_progress"].update()
                self.fields["bulk_status"].update()

        self.fields["inventree_progress"].value = 1.0
        if failed == 0:
            self.fields["inventree_progress"].color = "green"
        elif success > 0:
            self.fields["inventree_progress"].color = "amber"
        else:
            self.fields["inventree_progress"].color = "red"
        self.fields[
            "bulk_status"
        ].value = f"Bulk import complete: success={success} failed={failed}"
        self.fields["inventree_progress"].update()
        self.fields["bulk_status"].update()

        cprint(
            f"[BULK]\tCompleted all rows | success={success} failed={failed}",
            silent=settings.SILENT,
        )

        self.enable_create(True)

        all_errors = parse_errors + failures
        if all_errors:
            preview = "\n".join([f"- {err}" for err in all_errors[:5]])
            remaining = max(0, len(all_errors) - 5)
            if remaining:
                preview += f"\n- ... and {remaining} more"
            self.show_dialog(
                DialogType.WARNING,
                f"Bulk import completed. Success: {success}, Failed: {failed}.\nErrors:\n{preview}",
            )
        else:
            self.show_dialog(
                DialogType.VALID,
                f"Bulk import completed. Created {success} parts successfully",
            )

    def show_dialog(self, type: DialogType, message: str):
        if "create" in self.fields:
            self.enable_create(True)
        return super().show_dialog(type, message)

    def enable_create(self, enable=True):
        self.fields["create"].disabled = not enable
        self.fields["create"].update()
        # Invert cancel button
        self.enable_cancel(enable=not enable)

    def enable_cancel(self, enable=True):
        if enable:
            for item in self.fields["cancel"].content.controls:
                item.color = ft.colors.RED_ACCENT_700
        else:
            for item in self.fields["cancel"].content.controls:
                item.color = None

        self.fields["cancel"].disabled = not enable
        self.fields["cancel"].update()

    def cancel(self, e=None):
        self.create_continue = False

    def process_cancel(self):
        # if settings.ENABLE_INVENTREE:
        #     if self.fields['inventree_progress'].value < 1.0:
        #         self.fields['inventree_progress'].color = "red"
        #         self.fields['inventree_progress'].update()
        # if settings.ENABLE_KICAD:
        #     if self.fields['kicad_progress'].value < 1.0:
        #         self.fields['kicad_progress'].color = "red"
        #         self.fields['kicad_progress'].update()
        self.show_dialog(DialogType.ERROR, "Action Cancelled")
        self.create_continue = True
        self.enable_create(True)
        return

    def reset_progress_bars(self):
        # Setup progress bars
        inv_row = self.inventree_progress_row.current if self.inventree_progress_row else None
        if inv_row is not None:
            if not settings.ENABLE_INVENTREE:
                inv_row.visible = False
            else:
                inv_row.visible = True
                # Reset progress bar
                progress.reset_progress_bar(self.fields["inventree_progress"])
            _safe_update(inv_row)

        kicad_row = self.kicad_progress_row.current if self.kicad_progress_row else None
        if kicad_row is not None:
            if not settings.ENABLE_KICAD:
                kicad_row.visible = False
            else:
                kicad_row.visible = True
                # Reset progress bar
                progress.reset_progress_bar(self.fields["kicad_progress"])
            _safe_update(kicad_row)

        if not settings.ENABLE_INVENTREE and not settings.ENABLE_KICAD:
            self.fields["create"].disabled = True
        else:
            self.fields["create"].disabled = False
        _safe_update(self.fields["create"])

    def create_part(self, e=None):
        create_start_ts = time.perf_counter()
        self.reset_progress_bars()

        if not settings.ENABLE_INVENTREE and not settings.ENABLE_KICAD:
            self.show_dialog(
                DialogType.ERROR,
                "Both InvenTree and KiCad are disabled (nothing to create)",
            )

        # print('data_from_views='); cprint(data_from_views)

        # Check data is present
        if not data_from_views.get("Part Search", None):
            self.show_dialog(DialogType.ERROR, "Missing Part Data (nothing to create)")
            return

        # Custom part check
        part_info = copy.deepcopy(data_from_views["Part Search"])
        custom = part_info.pop("custom_part")

        # Part number check
        part_number = data_from_views["Part Search"].get("manufacturer_part_number", None)
        if not custom:
            if not part_number:
                self.show_dialog(DialogType.ERROR, "Missing Manufacturer Part Number")
                return
            else:
                # Update IPN (later overwritten)
                part_info["IPN"] = part_number

        # Button update
        self.enable_create(False)

        # KiCad data gathering
        symbol = None
        template = None
        footprint = None
        if settings.ENABLE_KICAD and not settings.ENABLE_ALTERNATE:
            # Check data is present
            if not data_from_views.get("KiCad", None):
                self.show_dialog(DialogType.ERROR, "Missing KiCad Data")
                return

            # Process symbol
            symbol_lib = data_from_views["KiCad"].get("Symbol Library", None)
            if symbol_lib:
                symbol = f"{symbol_lib}:{part_number}"

            # Process template
            template = data_from_views["KiCad"].get("Symbol Template", None)

            # Process footprint
            footprint_lib = data_from_views["KiCad"].get("Footprint Library", None)
            if footprint_lib:
                if data_from_views["KiCad"].get("New Footprint", False):
                    new_footprint = data_from_views["KiCad"].get("New Footprint Name", "TBD")
                    footprint = f"{footprint_lib}:{new_footprint}"
                elif data_from_views["KiCad"].get("Footprint", None):
                    footprint = f"{footprint_lib}:{data_from_views['KiCad']['Footprint']}"
                else:
                    pass

            # print(symbol, template, footprint)
            if not symbol or not template or not footprint:
                self.show_dialog(DialogType.ERROR, "Missing KiCad Data")
                return

        if not self.create_continue:
            return self.process_cancel()

        # InvenTree data processing
        if settings.ENABLE_INVENTREE:
            # Check data is present
            if not data_from_views.get("InvenTree", None):
                self.show_dialog(DialogType.ERROR, "Missing InvenTree Data")
                return
            # Check connection
            ts_connect = time.perf_counter()
            if not inventree_interface.connect_to_server():
                self.show_dialog(DialogType.ERROR, "ERROR: Failed to connect to InvenTree server")
                return
            elapsed_connect = (time.perf_counter() - ts_connect) * 1000.0
            cprint(
                f"[MAIN]\tCreate InvenTree connect: {elapsed_connect:.1f} ms",
                silent=settings.SILENT,
            )

            if not self._stock_location_id_map:
                ts_map_prefetch = time.perf_counter()
                self._stock_location_id_map = inventree_interface.get_cached_location_id_map()
                elapsed_map_prefetch = (time.perf_counter() - ts_map_prefetch) * 1000.0
                cprint(
                    f"[MAIN]\tCreate stock location map prefetch: {len(self._stock_location_id_map)} entries ({elapsed_map_prefetch:.1f} ms)",
                    silent=settings.SILENT,
                )

            if settings.ENABLE_ALTERNATE:
                # Check mandatory data
                if (
                    not data_from_views["InvenTree"]["Existing Part ID"]
                    and not data_from_views["InvenTree"]["Existing Part IPN"]
                ):
                    self.show_dialog(DialogType.ERROR, "Missing Existing Part ID and Part IPN")
                    return
                # Create alternate
                alt_result = inventree_interface.inventree_create_alternate(
                    part_info=part_info,
                    part_id=data_from_views["InvenTree"]["Existing Part ID"],
                    part_ipn=data_from_views["InvenTree"]["Existing Part IPN"],
                    show_progress=self.fields["inventree_progress"],
                )
            else:
                # Check mandatory data
                if not data_from_views["Part Search"].get("name", None):
                    self.show_dialog(DialogType.ERROR, "Missing Part Name")
                    return
                if len(data_from_views["Part Search"].get("name", None)) > 100:
                    self.show_dialog(DialogType.ERROR, "Part Name too long (>100 characters)")
                    return
                if not data_from_views["Part Search"].get("description", None):
                    self.show_dialog(DialogType.ERROR, "Missing Part Description")
                    return
                # Get relevant data
                category_tree = data_from_views["InvenTree"].get("Category", None)
                if not category_tree:
                    # Check category is present
                    self.show_dialog(DialogType.ERROR, "Missing InvenTree Category")
                    return
                else:
                    part_info["category_tree"] = category_tree
                # Category code
                if settings.CONFIG_IPN.get("IPN_CATEGORY_CODE", False):
                    if data_from_views["InvenTree"].get("Create New Code", False):
                        part_info["category_code"] = data_from_views["InvenTree"].get(
                            "New Category Code", ""
                        )
                    else:
                        part_info["category_code"] = data_from_views["InvenTree"].get(
                            "IPN: Category Code", ""
                        )

                stock = None
                if data_from_views["InvenTree"].get("Create stock"):
                    stock_tree = data_from_views["InvenTree"].get("Stock location", None)
                    if stock_tree:
                        ts_loc_resolve = time.perf_counter()
                        location_pk = self._resolve_stock_location_pk(stock_tree)
                        elapsed_loc_resolve = (time.perf_counter() - ts_loc_resolve) * 1000.0
                        cprint(
                            f"[MAIN]\tCreate stock location resolve: {elapsed_loc_resolve:.1f} ms",
                            silent=settings.SILENT,
                        )
                        stock = {
                            "location": location_pk,
                            "quantity": data_from_views["InvenTree"].get("Stock quantity"),
                            "make_default": data_from_views["InvenTree"].get(
                                "Make stock location default"
                            ),
                        }

                # Create new part
                ts_create = time.perf_counter()
                new_part, part_pk, part_info = inventree_interface.inventree_create(
                    part_info=part_info,
                    kicad=settings.ENABLE_KICAD,
                    symbol=symbol,
                    footprint=footprint,
                    show_progress=self.fields["inventree_progress"],
                    is_custom=custom,
                    stock=stock,
                )
                elapsed_create = (time.perf_counter() - ts_create) * 1000.0
                cprint(
                    f"[MAIN]\tCreate inventree_create: {elapsed_create:.1f} ms",
                    silent=settings.SILENT,
                )
                # print(new_part, part_pk)
                # cprint(part_info)

            if settings.ENABLE_ALTERNATE:
                if alt_result:
                    # Update InvenTree URL
                    if data_from_views["InvenTree"]["Existing Part IPN"]:
                        part_ref = data_from_views["InvenTree"]["Existing Part IPN"]
                    else:
                        part_ref = data_from_views["InvenTree"]["Existing Part ID"]
                    part_info["inventree_url"] = f"{settings.PART_URL_ROOT}{part_ref}/"
                else:
                    self.fields["inventree_progress"].color = "amber"
                # Complete add operation
                self.fields["inventree_progress"].value = progress.MAX_PROGRESS
            else:
                if part_pk:
                    location_tree = data_from_views["InvenTree"].get("Stock location", None)
                    assign_barcode = bool(data_from_views["InvenTree"].get("Part barcode", False))
                    self._post_process_part(
                        part_pk=part_pk,
                        location_tree=location_tree,
                        create_stock_enabled=bool(data_from_views["InvenTree"].get("Create stock")),
                        assign_barcode=assign_barcode,
                        part_info=part_info,
                    )

                    elapsed_total = (time.perf_counter() - create_start_ts) * 1000.0
                    cprint(
                        f"[MAIN]\tCreate total: {elapsed_total:.1f} ms",
                        silent=settings.SILENT,
                    )

                    # Update symbol
                    if symbol:
                        symbol = f"{symbol.split(':')[0]}:{part_info['IPN']}"

                    self.fields["inventree_progress"].color = "green"
                    if not new_part:
                        self.fields["inventree_progress"].color = "amber"
                    # Complete add operation
                    self.fields["inventree_progress"].value = progress.MAX_PROGRESS
                else:
                    self.fields["inventree_progress"].color = "red"

            self.fields["inventree_progress"].update()

        if not self.create_continue:
            return self.process_cancel()

        # KiCad data processing
        if settings.ENABLE_KICAD and not settings.ENABLE_ALTERNATE:
            # Store "pseudo-category" as re-used in multiple places
            pseudo_category = symbol.split(":")[0]
            # Translate part info if InvenTree not enabled
            if not settings.ENABLE_INVENTREE:
                part_info = inventree_interface.translate_form_to_inventree(
                    part_info=part_info,
                    category_tree=[pseudo_category],
                    is_custom=custom,
                )
                # Also add datasheet URL as part page URL
                part_info["inventree_url"] = part_info["datasheet"]
            part_info["Symbol"] = symbol
            part_info["Template"] = template.split("/")
            part_info["Footprint"] = footprint

            symbol_library_path = os.path.join(
                settings.KICAD_SETTINGS["KICAD_SYMBOLS_PATH"],
                f"{pseudo_category}.kicad_sym",
            )

            # Reset progress
            progress.CREATE_PART_PROGRESS = 0
            # Add part symbol to KiCAD
            cprint("\n[MAIN]\tAdding part to KiCad", silent=settings.SILENT)
            kicad_success, kicad_new_part, kicad_part_name = kicad_interface.inventree_to_kicad(
                part_data=part_info,
                library_path=symbol_library_path,
                show_progress=self.fields["kicad_progress"],
            )
            # print(kicad_success, kicad_new_part)
            # Update symbol name in InvenTree
            if settings.ENABLE_INVENTREE and part_pk:
                old_state = settings.UPDATE_INVENTREE
                settings.UPDATE_INVENTREE = True
                inventree_interface.inventree_process_parameters(
                    part_pk,
                    {"Symbol": f"{symbol_lib}:{kicad_part_name}"},
                    show_progress=self.fields["inventree_progress"],
                )
                settings.UPDATE_INVENTREE = old_state

            # Complete add operation
            if kicad_success:
                self.fields["kicad_progress"].color = "green"
                if not kicad_new_part:
                    self.fields["kicad_progress"].color = "amber"
                    self.fields["kicad_progress"].update()
                self.fields["kicad_progress"].value = progress.MAX_PROGRESS
                self.fields["kicad_progress"].update()
            else:
                self.fields["kicad_progress"].color = "red"
                self.fields["kicad_progress"].update()

        if not self.create_continue:
            return self.process_cancel()

        # Final operations
        # Download a local version of the part datasheet
        if settings.DATASHEET_SAVE_ENABLED:
            filename = os.path.join(
                settings.DATASHEET_SAVE_PATH,
                f"{part_info.get('IPN', 'datasheet')}.pdf",
            )
            if settings.DATASHEET_UPLOAD and os.path.isfile(filename):
                # Datasheet was already downloaded
                cprint("\n[MAIN]\tDatasheet")
                cprint(f"[INFO]\tSuccess: Datasheet file exists ({filename})")
            else:
                # Datasheet needs to be downloaded
                datasheet_url = part_info.get("datasheet", None)
                if datasheet_url:
                    cprint("\n[MAIN]\tDownloading Datasheet")
                    if download_with_retry(datasheet_url, filename, filetype="PDF", timeout=10):
                        cprint(f"[INFO]\tSuccess: Datasheet saved to {filename}")
        # Open browser
        if settings.ENABLE_INVENTREE:
            if part_info.get("inventree_url", None):
                if settings.AUTOMATIC_BROWSER_OPEN:
                    # Auto-Open Browser Window
                    cprint(
                        f"\n[MAIN]\tOpening URL {part_info['inventree_url']} in browser",
                        silent=settings.SILENT,
                    )
                    try:
                        self._page.launch_url(part_info["inventree_url"])
                    except TypeError:
                        cprint("[INFO]\tError: Failed to open URL", silent=settings.SILENT)
                else:
                    cprint(
                        f"\n[MAIN]\tPart page URL: {part_info['inventree_url']}",
                        silent=settings.SILENT,
                    )

        # Button update
        self.enable_create(True)

    def build_column(self):
        self.inventree_progress_row = ft.Ref[ft.Row]()
        self.kicad_progress_row = ft.Ref[ft.Row]()

        # Update callbacks
        self.fields["create"].on_click = self.create_part
        self.fields["cancel"].on_click = self.cancel
        self.fields["bulk_excel_pick"].on_click = self._pick_bulk_excel
        self.fields["bulk_import"].on_click = self._bulk_create_from_excel

        self.column = ft.Column(
            controls=[
                ft.Row(),
                ft.Row(
                    controls=[self.fields["bulk_status"]],
                    alignment=ft.MainAxisAlignment.CENTER,
                    width=900,
                ),
                ft.Row(height=10),
                ft.Row(
                    controls=[
                        self.fields["bulk_excel_path"],
                        self.fields["bulk_excel_pick"],
                        self.fields["bulk_import"],
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    width=980,
                ),
                ft.Row(height=10),
                ft.Row(
                    controls=[
                        self.fields["create"],
                        self.fields["cancel"],
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    width=600,
                ),
                ft.Row(height=16),
                ft.Row(
                    ref=self.inventree_progress_row,
                    controls=[
                        ft.Icon(ft.icons.INVENTORY_2, size=32),
                        ft.Text("InvenTree", size=20, weight=ft.FontWeight.BOLD, width=120),
                        self.fields["inventree_progress"],
                    ],
                    width=600,
                    visible=settings.ENABLE_INVENTREE,
                ),
                ft.Row(
                    ref=self.kicad_progress_row,
                    controls=[
                        ft.Icon(ft.icons.SETTINGS_INPUT_COMPONENT, size=32),
                        ft.Text("KiCad", size=20, weight=ft.FontWeight.BOLD, width=120),
                        self.fields["kicad_progress"],
                    ],
                    width=600,
                    visible=settings.ENABLE_KICAD,
                ),
            ],
        )

    def did_mount(self):
        self.reset_progress_bars()
        return super().did_mount()
