"""Root conftest — runs before any test module is collected.

Sets ENABLE_TEST early so that kintree.config.settings skips heavy
filesystem / network initialisation that would fail outside a real install.
"""

import os
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Make the project root importable regardless of how pytest is invoked.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Lightweight stub of kintree.config.settings so that importing any kintree
# module doesn't trigger real config-file loading or os.makedirs calls.
# ---------------------------------------------------------------------------
def pytest_configure(config):
    """Install a minimal settings stub *before* any kintree code is imported."""

    _settings_defaults = {
        "ENABLE_TEST": True,
        "SILENT": True,
        "HIDE_DEBUG": True,
        "CONFIG_IPN": {},
        "CONFIG_CATEGORIES": "",
        "CONFIG_STOCK_LOCATIONS": "",
        "CONFIG_SUPPLIER_PARAMETERS": "",
        "CONFIG_DIGIKEY_CATEGORIES": "",
        "CONFIG_SUPPLIERS": {},
        "CONFIG_DIGIKEY": {},
        "CONFIG_MOUSER": {},
        "CONFIG_ELEMENT14": {},
        "CONFIG_LCSC": {},
        "CONFIG_JAMECO": {},
        "CONFIG_TME": {},
        "CONFIG_AUTOMATIONDIRECT": {},
        "CATEGORY_MATCH_RATIO_LIMIT": 0.85,
        "inventree_part_template": {"parameters": {}},
        "search_results": {"directory": "/tmp/", "extension": ".yaml"},
        "SERVER_ADDRESS": "",
        "USERNAME": "",
        "PASSWORD": "",
        "PROXIES": {},
        "load_inventree_settings": lambda: None,
    }

    # If the real settings module was already loaded (e.g. by a conftest in a
    # sub-directory), just flip the test flag and move on.
    if "kintree.config.settings" in sys.modules:
        mod = sys.modules["kintree.config.settings"]
        for attr, val in _settings_defaults.items():
            if not hasattr(mod, attr):
                setattr(mod, attr, val)
        mod.ENABLE_TEST = True
        mod.SILENT = True
        return

    # ── Build a fake ``kintree.config`` package so relative imports resolve. ──
    kintree_pkg = types.ModuleType("kintree")
    kintree_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree")]
    kintree_pkg.__package__ = "kintree"

    config_pkg = types.ModuleType("kintree.config")
    config_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "config")]
    config_pkg.__package__ = "kintree.config"

    # ── Stub config_interface (needed by settings on import) ──
    ci_stub = types.ModuleType("kintree.config.config_interface")

    def _stub_load_file(*a, **kw):
        return {
            "KICAD_SYMBOLS_PATH": "",
            "KICAD_TEMPLATES_PATH": "",
            "KICAD_FOOTPRINTS_PATH": "",
            "KICAD_LIBRARIES": {},
            "KICAD_TEMPLATES": {},
            "KICAD_FOOTPRINTS": {},
        }

    ci_stub.load_file = _stub_load_file
    ci_stub.dump_file = lambda *a, **kw: True
    ci_stub.load_user_paths = lambda home_dir="": {
        "USER_FILES": os.path.join(home_dir or "/tmp", "user", ""),
        "USER_CACHE": os.path.join(home_dir or "/tmp", "cache", ""),
    }
    ci_stub.load_user_config_files = lambda **kw: True
    ci_stub.load_inventree_user_settings = lambda p: {}
    ci_stub.load_libraries_paths = lambda *a, **kw: {}
    ci_stub.load_templates_paths = lambda *a, **kw: {}
    ci_stub.load_footprint_paths = lambda *a, **kw: {}

    # ── Stub common.tools ──
    common_pkg = types.ModuleType("kintree.common")
    common_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "common")]
    common_pkg.__package__ = "kintree.common"

    tools_stub = types.ModuleType("kintree.common.tools")
    tools_stub.cprint = lambda *a, **kw: None
    tools_stub.create_library = lambda *a, **kw: None
    tools_stub.download = lambda *a, **kw: None
    tools_stub.download_with_retry = lambda *a, **kw: None
    tools_stub.validate_downloaded_file = lambda *a, **kw: False

    # ── Stub common.progress ──
    progress_stub = types.ModuleType("kintree.common.progress")
    progress_stub.update_progress = lambda *a, **kw: None

    # ── Stub database package ──
    database_pkg = types.ModuleType("kintree.database")
    database_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "database")]
    database_pkg.__package__ = "kintree.database"

    # ── Stub search package + supplier APIs ──
    search_pkg = types.ModuleType("kintree.search")
    search_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "search")]
    search_pkg.__package__ = "kintree.search"

    _supplier_api_names = [
        "search_api",
        "digikey_api",
        "mouser_api",
        "element14_api",
        "lcsc_api",
        "jameco_api",
        "tme_api",
        "automationdirect_api",
    ]
    supplier_stubs = {}
    for api_name in _supplier_api_names:
        stub = types.ModuleType(f"kintree.search.{api_name}")
        stub.get_default_search_keys = lambda: [""] * 12
        stub.load_from_file = lambda *a, **kw: None
        supplier_stubs[api_name] = stub

    # ── Stub GUI packages (for BarcodeScannedRow import chain) ──
    # ── Add __version__ to the kintree stub ──
    kintree_pkg.__version__ = "0.0.0-test"

    gui_pkg = types.ModuleType("kintree.gui")
    gui_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "gui")]
    gui_pkg.__package__ = "kintree.gui"

    gui_views_pkg = types.ModuleType("kintree.gui.views")
    gui_views_pkg.__path__ = [os.path.join(_PROJECT_ROOT, "kintree", "gui", "views")]
    gui_views_pkg.__package__ = "kintree.gui.views"

    all_stubs = [
        ("kintree", kintree_pkg),
        ("kintree.common", common_pkg),
        ("kintree.common.tools", tools_stub),
        ("kintree.common.progress", progress_stub),
        ("kintree.config", config_pkg),
        ("kintree.config.config_interface", ci_stub),
        ("kintree.database", database_pkg),
        ("kintree.search", search_pkg),
        ("kintree.gui", gui_pkg),
        ("kintree.gui.views", gui_views_pkg),
    ]
    for api_name, stub in supplier_stubs.items():
        all_stubs.append((f"kintree.search.{api_name}", stub))

    for name, mod in all_stubs:
        sys.modules.setdefault(name, mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_yaml(tmp_path):
    """Return a helper that writes a YAML file in tmp_path and returns its path."""
    import yaml

    def _write(name, data):
        p = tmp_path / name
        with open(p, "w") as fh:
            yaml.safe_dump(data, fh)
        return str(p)

    return _write
