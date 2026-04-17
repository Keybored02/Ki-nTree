import sys
import unittest
from kintree.common.part_tools import clean_parameter_value
from kintree.common.part_tools import compare
from kintree.common.part_tools import generate_part_number
import kintree.config.settings as settings

"""Unit tests for kintree.common.part_tools."""


# ---------------------------------------------------------------------------
# Lightweight settings / config_interface stubs so the module can import.
# The conftest.py hook normally handles this, but we ensure the attrs exist.
# ---------------------------------------------------------------------------
def _ensure_settings_attrs():
    mod = sys.modules.get("kintree.config.settings")
    if mod is None:
        return
    if not hasattr(mod, "CONFIG_IPN"):
        mod.CONFIG_IPN = {}
    if not hasattr(mod, "CONFIG_CATEGORIES"):
        mod.CONFIG_CATEGORIES = ""
    if not hasattr(mod, "HIDE_DEBUG"):
        mod.HIDE_DEBUG = True


_ensure_settings_attrs()


# ---------------------------------------------------------------------------
# generate_part_number
# ---------------------------------------------------------------------------


class TestGeneratePartNumber(unittest.TestCase):
    def setUp(self):
        settings.CONFIG_IPN = {
            "IPN_ENABLE_PREFIX": False,
            "IPN_PREFIX": "",
            "IPN_CATEGORY_CODE": False,
            "IPN_ENABLE_SUFFIX": False,
            "IPN_SUFFIX": "",
            "IPN_UNIQUE_ID_LENGTH": "6",
        }

    def test_basic(self):
        ipn = generate_part_number("Capacitors", 42)
        self.assertEqual(ipn, "000042")

    def test_with_prefix(self):
        settings.CONFIG_IPN["IPN_ENABLE_PREFIX"] = True
        settings.CONFIG_IPN["IPN_PREFIX"] = "KT"
        ipn = generate_part_number("Capacitors", 1)
        self.assertEqual(ipn, "KT-000001")

    def test_with_suffix(self):
        settings.CONFIG_IPN["IPN_ENABLE_SUFFIX"] = True
        settings.CONFIG_IPN["IPN_SUFFIX"] = "R1"
        ipn = generate_part_number("Capacitors", 5)
        self.assertEqual(ipn, "000005-R1")

    def test_with_category_code(self):
        settings.CONFIG_IPN["IPN_CATEGORY_CODE"] = True
        ipn = generate_part_number("Capacitors", 7, category_code="CAP")
        self.assertEqual(ipn, "CAP-000007")

    def test_custom_id_length(self):
        settings.CONFIG_IPN["IPN_UNIQUE_ID_LENGTH"] = "3"
        ipn = generate_part_number("Resistors", 99)
        self.assertEqual(ipn, "099")

    def test_full_ipn(self):
        settings.CONFIG_IPN["IPN_ENABLE_PREFIX"] = True
        settings.CONFIG_IPN["IPN_PREFIX"] = "KT"
        settings.CONFIG_IPN["IPN_CATEGORY_CODE"] = True
        settings.CONFIG_IPN["IPN_ENABLE_SUFFIX"] = True
        settings.CONFIG_IPN["IPN_SUFFIX"] = "R0"
        ipn = generate_part_number("Capacitors", 10, category_code="CAP")
        self.assertEqual(ipn, "KT-CAP-000010-R0")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
class TestCompare(unittest.TestCase):
    def test_equal_params(self):
        new = {"R": "10k", "Tol": "1%"}
        db = {"R": "10k", "Tol": "1%"}
        self.assertTrue(compare(new, db, []))

    def test_different_params(self):
        new = {"R": "10k"}
        db = {"R": "4.7k"}
        self.assertFalse(compare(new, db, []))

    def test_filter_match(self):
        new = {"R": "10k", "Package": "0402"}
        db = {"R": "10k", "Package": "0603"}
        self.assertTrue(compare(new, db, ["R"]))

    def test_filter_mismatch(self):
        new = {"R": "10k", "Package": "0402"}
        db = {"R": "4.7k", "Package": "0402"}
        self.assertFalse(compare(new, db, ["R"]))

    def test_missing_key_returns_false(self):
        new = {"R": "10k", "Extra": "x"}
        db = {"R": "10k"}
        self.assertFalse(compare(new, db, []))


# ---------------------------------------------------------------------------
# clean_parameter_value
# ---------------------------------------------------------------------------
class TestCleanParameterValue(unittest.TestCase):
    def test_package_first_token(self):
        val = clean_parameter_value("Capacitors", "Package", "0402 (1005 Metric)")
        self.assertEqual(val, "0402")

    def test_size_metric_single(self):
        val = clean_parameter_value("Resistors", "Size", "3.20mm x 1.60mm")
        self.assertEqual(val, "3.20x1.60mm")

    def test_size_metric_diameter(self):
        val = clean_parameter_value("Capacitors", "Size", "dia 5.00mm")
        self.assertEqual(val, "⌀5.00mm")

    def test_size_three_dims(self):
        val = clean_parameter_value("Connectors", "Outline", "10.00mm x 5.00mm x 2.00mm")
        self.assertEqual(val, "10.00x5.00x2.00mm")

    def test_power_ratio(self):
        val = clean_parameter_value("Resistors", "Power", "1/4W")
        self.assertEqual(val, "1/4W")

    def test_esr_cleanup(self):
        val = clean_parameter_value("Capacitors", "ESR", "200Max Ohm")
        self.assertEqual(val, "200R")

    def test_resistance_kohms(self):
        val = clean_parameter_value("Resistors", "Resistance", "10 kOhms")
        self.assertEqual(val, "10K")

    def test_resistance_mohms(self):
        val = clean_parameter_value("Resistors", "Resistance", "2.2 MOhms")
        self.assertEqual(val, "2.2M")

    def test_resistance_ohms(self):
        val = clean_parameter_value("Resistors", "Resistance", "100 Ohms")
        self.assertEqual(val, "100R")

    def test_parenthesis_removal(self):
        val = clean_parameter_value("Capacitors", "Voltage", "50V(DC)")
        self.assertEqual(val, "50V")

    def test_at_sign_space_removal(self):
        val = clean_parameter_value("Capacitors", "Capacitance", "10uF @ 25V")
        self.assertEqual(val, "10uF@25V")

    def test_double_quote_escape(self):
        val = clean_parameter_value("Connectors", "Pitch", '0.1"')
        self.assertEqual(val, '0.1\\"')

    def test_range_cleanup(self):
        val = clean_parameter_value("Capacitors", "Temperature", "-40°C ~ 85°C")
        self.assertEqual(val, "-40~85°C")
