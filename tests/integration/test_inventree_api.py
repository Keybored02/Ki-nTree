import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
import kintree.config.settings as settings
from kintree.database import inventree_api

"""Integration tests for kintree.database.inventree_api (mocked HTTP)."""

# Ensure settings attrs needed by inventree_api at import time.
for attr, default in [
    ("SILENT", True),
    ("HIDE_DEBUG", True),
    ("CONFIG_IPN", {}),
    ("CONFIG_CATEGORIES", ""),
    ("inventree_part_template", {}),
]:
    if not hasattr(settings, attr):
        setattr(settings, attr, default)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_fake_api(token="tok123", base_url="http://inv.local"):
    """Inject a fake inventree_api global."""
    fake = MagicMock()
    fake.token = token
    fake.base_url = base_url
    inventree_api.inventree_api = fake
    return fake


# ---------------------------------------------------------------------------
# get_supplier_part_pk
# ---------------------------------------------------------------------------


class TestGetSupplierPartPk(unittest.TestCase):
    def setUp(self):
        _set_fake_api()

    def test_returns_pk_on_exact_match(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "results": [
                {"pk": 42, "SKU": "ABC-123"},
                {"pk": 99, "SKU": "OTHER"},
            ]
        }
        with patch("kintree.database.inventree_api.requests.get", return_value=resp):
            self.assertEqual(inventree_api.get_supplier_part_pk(1, "ABC-123"), 42)

    def test_case_insensitive(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [{"pk": 7, "SKU": "abc-123"}]}
        with patch("kintree.database.inventree_api.requests.get", return_value=resp):
            self.assertEqual(inventree_api.get_supplier_part_pk(1, "ABC-123"), 7)

    def test_no_match_returns_zero(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [{"pk": 7, "SKU": "DIFFERENT"}]}
        with patch("kintree.database.inventree_api.requests.get", return_value=resp):
            self.assertEqual(inventree_api.get_supplier_part_pk(1, "ABC-123"), 0)

    def test_empty_args(self):
        self.assertEqual(inventree_api.get_supplier_part_pk(0, ""), 0)
        self.assertEqual(inventree_api.get_supplier_part_pk(None, "SKU"), 0)

    def test_http_error_returns_zero(self):
        resp = MagicMock()
        resp.status_code = 500
        with patch("kintree.database.inventree_api.requests.get", return_value=resp):
            self.assertEqual(inventree_api.get_supplier_part_pk(1, "SKU"), 0)

    def test_no_token_returns_zero(self):
        inventree_api.inventree_api = MagicMock(token=None, base_url="http://x")
        self.assertEqual(inventree_api.get_supplier_part_pk(1, "SKU"), 0)


# ---------------------------------------------------------------------------
# link_barcode
# ---------------------------------------------------------------------------


class TestLinkBarcode(unittest.TestCase):
    def setUp(self):
        _set_fake_api()

    def test_success(self):
        resp = MagicMock()
        resp.status_code = 200
        with patch("kintree.database.inventree_api.requests.post", return_value=resp) as mock_post:
            self.assertTrue(inventree_api.link_barcode("BC001", part_pk=10))
            call_kwargs = mock_post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            self.assertEqual(payload["barcode"], "BC001")
            self.assertEqual(payload["part"], 10)

    def test_empty_barcode(self):
        self.assertFalse(inventree_api.link_barcode("", part_pk=10))

    def test_no_target(self):
        self.assertFalse(inventree_api.link_barcode("BC001"))

    def test_failure_status(self):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "bad request"
        with patch("kintree.database.inventree_api.requests.post", return_value=resp):
            self.assertFalse(inventree_api.link_barcode("BC001", part_pk=10))

    def test_supplier_part_target(self):
        resp = MagicMock()
        resp.status_code = 201
        with patch("kintree.database.inventree_api.requests.post", return_value=resp) as mock_post:
            self.assertTrue(inventree_api.link_barcode("BC002", supplierpart_pk=5))
            payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
            self.assertEqual(payload["supplierpart"], 5)


# ---------------------------------------------------------------------------
# _to_parent_id
# ---------------------------------------------------------------------------


class TestToParentId(unittest.TestCase):
    def test_int(self):
        self.assertEqual(inventree_api._to_parent_id(5), 5)

    def test_string_int(self):
        self.assertEqual(inventree_api._to_parent_id("42"), 42)

    def test_dict_pk(self):
        self.assertEqual(inventree_api._to_parent_id({"pk": 3}), 3)

    def test_dict_id(self):
        self.assertEqual(inventree_api._to_parent_id({"id": 7}), 7)

    def test_none(self):
        self.assertIsNone(inventree_api._to_parent_id(None))

    def test_zero(self):
        self.assertIsNone(inventree_api._to_parent_id(0))

    def test_string_none(self):
        self.assertIsNone(inventree_api._to_parent_id("None"))
