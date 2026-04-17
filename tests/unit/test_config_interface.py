"""Unit tests for kintree.config.config_interface.

The conftest installs a stub for this module so that settings.py can import
without a real install. We force-reload the real module here by temporarily
removing the stub from sys.modules.
"""

import base64
import os
import sys
import unittest

# Force-import the real config_interface (not the conftest stub).
# The pop must happen BEFORE the import so the real module is loaded.
_stub = sys.modules.pop("kintree.config.config_interface", None)
from kintree.config import config_interface as _ci  # noqa: E402

# Restore the stub so other modules still see it.
if _stub is not None:
    sys.modules["kintree.config.config_interface"] = _stub

load_file = _ci.load_file
dump_file = _ci.dump_file
load_user_paths = _ci.load_user_paths
load_inventree_user_settings = _ci.load_inventree_user_settings
load_supplier_categories = _ci.load_supplier_categories
load_supplier_categories_inversed = _ci.load_supplier_categories_inversed
load_category_parameters = _ci.load_category_parameters
FUNCTION_FILTER_KEY = _ci.FUNCTION_FILTER_KEY


# ---------------------------------------------------------------------------
# load_file / dump_file
# ---------------------------------------------------------------------------


class TestLoadDumpFile(unittest.TestCase):
    def test_round_trip(self):
        import tempfile

        data = {"key": "value", "nested": {"a": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test.yaml")
            self.assertTrue(dump_file(data, p))
            self.assertEqual(load_file(p), data)

    def test_load_missing_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_file(os.path.join(tmp, "nonexistent.yaml")))

    def test_load_invalid_yaml(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "bad.yaml")
            with open(p, "w") as f:
                f.write("key: [invalid\n  yaml: {")
            self.assertIsNone(load_file(p))

    def test_dump_overwrites(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "test.yaml")
            dump_file({"a": 1}, p)
            dump_file({"b": 2}, p)
            self.assertEqual(load_file(p), {"b": 2})


# ---------------------------------------------------------------------------
# load_user_paths
# ---------------------------------------------------------------------------


class TestLoadUserPaths(unittest.TestCase):
    def test_creates_default_when_missing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = load_user_paths(tmp)
            self.assertIn("USER_FILES", result)
            self.assertIn("USER_CACHE", result)
            self.assertTrue(os.path.exists(os.path.join(tmp, "settings.yaml")))

    def test_reads_existing(self):
        import tempfile

        custom = {"USER_FILES": "/custom/files/", "USER_CACHE": "/custom/cache/"}
        with tempfile.TemporaryDirectory() as tmp:
            dump_file(custom, os.path.join(tmp, "settings.yaml"))
            result = load_user_paths(tmp)
            self.assertEqual(result["USER_FILES"], "/custom/files/")


# ---------------------------------------------------------------------------
# load_inventree_user_settings
# ---------------------------------------------------------------------------


class TestLoadInventreeUserSettings(unittest.TestCase):
    def test_decodes_password(self):
        import tempfile

        pw = base64.b64encode(b"secret").decode()
        data = {"PASSWORD": pw, "SERVER_ADDRESS": "http://localhost", "USERNAME": "admin"}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "inventree.yaml")
            dump_file(data, p)
            result = load_inventree_user_settings(p)
            self.assertEqual(result["PASSWORD"], "secret")

    def test_defaults_added(self):
        import tempfile

        pw = base64.b64encode(b"pw").decode()
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "inventree.yaml")
            dump_file({"PASSWORD": pw}, p)
            result = load_inventree_user_settings(p)
            self.assertFalse(result["ENABLE_PROXY"])
            self.assertFalse(result["DATASHEET_UPLOAD"])
            self.assertFalse(result["PRICING_UPLOAD"])

    def test_proxy_extracted(self):
        import tempfile

        pw = base64.b64encode(b"pw").decode()
        data = {"PASSWORD": pw, "ENABLE_PROXY": True, "PROXIES": {"https": "http://proxy:8080"}}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "inventree.yaml")
            dump_file(data, p)
            result = load_inventree_user_settings(p)
            self.assertEqual(result["PROXY"], "http://proxy:8080")

    def test_missing_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = load_inventree_user_settings(os.path.join(tmp, "nope.yaml"))
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# load_supplier_categories
# ---------------------------------------------------------------------------


class TestLoadSupplierCategories(unittest.TestCase):
    def _write(self, tmp, data):
        p = os.path.join(tmp, "supplier.yaml")
        dump_file(data, p)
        return p

    def test_basic_load(self):
        import tempfile

        data = {"Capacitors": {"Ceramic": ["MLCC"]}}
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, data)
            self.assertEqual(load_supplier_categories(p), data)

    def test_clean_removes_filter_prefix(self):
        import tempfile

        data = {"Capacitors": {f"{FUNCTION_FILTER_KEY}Ceramic": ["MLCC"]}}
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, data)
            result = load_supplier_categories(p, clean=True)
            self.assertIn("Ceramic", result["Capacitors"])
            self.assertNotIn(f"{FUNCTION_FILTER_KEY}Ceramic", result["Capacitors"])


# ---------------------------------------------------------------------------
# load_supplier_categories_inversed
# ---------------------------------------------------------------------------


class TestLoadSupplierCategoriesInversed(unittest.TestCase):
    def test_inversion(self):
        import tempfile

        data = {"Capacitors": {"Ceramic": ["MLCC", "Disc"]}}
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "supplier.yaml")
            dump_file(data, p)
            result = load_supplier_categories_inversed(p)
            self.assertEqual(result["Capacitors"]["MLCC"], "Ceramic")
            self.assertEqual(result["Capacitors"]["Disc"], "Ceramic")

    def test_missing_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = load_supplier_categories_inversed(os.path.join(tmp, "nope.yaml"))
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# load_category_parameters
# ---------------------------------------------------------------------------


class TestLoadCategoryParameters(unittest.TestCase):
    def test_basic_mapping(self):
        import tempfile

        data = {
            "Capacitors": {
                "Capacitance": ["Cap", "Capacitance Value"],
                "Voltage": ["Rated Voltage"],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "params.yaml")
            dump_file(data, p)
            result = load_category_parameters(["Capacitors"], p)
            self.assertEqual(result["Cap"], "Capacitance")
            self.assertEqual(result["Capacitance Value"], "Capacitance")
            self.assertEqual(result["Rated Voltage"], "Voltage")

    def test_parent_inheritance(self):
        import tempfile

        data = {
            "Passives": {"Package": ["Package Type"]},
            "Capacitors": {"parent": ["Passives"], "Capacitance": ["Cap"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "params.yaml")
            dump_file(data, p)
            result = load_category_parameters(["Capacitors"], p)
            self.assertEqual(result["Package Type"], "Package")
            self.assertEqual(result["Cap"], "Capacitance")
