"""Unit tests for kintree.config.config_interface.

The conftest installs a stub for this module so that settings.py can import
without a real install. We force-reload the real module here by temporarily
removing the stub from sys.modules.
"""

import os
import sys
import base64
import pytest
import yaml

# Force-import the real config_interface (not the conftest stub).
_stub = sys.modules.pop('kintree.config.config_interface', None)
from kintree.config import config_interface as _ci
# Restore the stub so other modules still see it.
if _stub is not None:
    sys.modules['kintree.config.config_interface'] = _stub

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
class TestLoadDumpFile:
    def test_round_trip(self, tmp_path):
        data = {'key': 'value', 'nested': {'a': 1}}
        p = str(tmp_path / 'test.yaml')
        assert dump_file(data, p) is True
        loaded = load_file(p)
        assert loaded == data

    def test_load_missing_file(self, tmp_path):
        assert load_file(str(tmp_path / 'nonexistent.yaml')) is None

    def test_load_invalid_yaml(self, tmp_path):
        p = tmp_path / 'bad.yaml'
        p.write_text("key: [invalid\n  yaml: {")
        assert load_file(str(p)) is None

    def test_dump_overwrites(self, tmp_path):
        p = str(tmp_path / 'test.yaml')
        dump_file({'a': 1}, p)
        dump_file({'b': 2}, p)
        assert load_file(p) == {'b': 2}


# ---------------------------------------------------------------------------
# load_user_paths
# ---------------------------------------------------------------------------
class TestLoadUserPaths:
    def test_creates_default_when_missing(self, tmp_path):
        result = load_user_paths(str(tmp_path))
        assert 'USER_FILES' in result
        assert 'USER_CACHE' in result
        assert os.path.exists(tmp_path / 'settings.yaml')

    def test_reads_existing(self, tmp_path):
        custom = {'USER_FILES': '/custom/files/', 'USER_CACHE': '/custom/cache/'}
        dump_file(custom, str(tmp_path / 'settings.yaml'))
        result = load_user_paths(str(tmp_path))
        assert result['USER_FILES'] == '/custom/files/'


# ---------------------------------------------------------------------------
# load_inventree_user_settings
# ---------------------------------------------------------------------------
class TestLoadInventreeUserSettings:
    def test_decodes_password(self, tmp_path):
        pw = base64.b64encode(b'secret').decode()
        data = {
            'PASSWORD': pw,
            'SERVER_ADDRESS': 'http://localhost',
            'USERNAME': 'admin',
        }
        p = str(tmp_path / 'inventree.yaml')
        dump_file(data, p)
        result = load_inventree_user_settings(p)
        assert result['PASSWORD'] == 'secret'

    def test_defaults_added(self, tmp_path):
        pw = base64.b64encode(b'pw').decode()
        data = {'PASSWORD': pw}
        p = str(tmp_path / 'inventree.yaml')
        dump_file(data, p)
        result = load_inventree_user_settings(p)
        assert result['ENABLE_PROXY'] is False
        assert result['DATASHEET_UPLOAD'] is False
        assert result['PRICING_UPLOAD'] is False

    def test_proxy_extracted(self, tmp_path):
        pw = base64.b64encode(b'pw').decode()
        data = {
            'PASSWORD': pw,
            'ENABLE_PROXY': True,
            'PROXIES': {'https': 'http://proxy:8080'},
        }
        p = str(tmp_path / 'inventree.yaml')
        dump_file(data, p)
        result = load_inventree_user_settings(p)
        assert result['PROXY'] == 'http://proxy:8080'

    def test_missing_file(self, tmp_path):
        result = load_inventree_user_settings(str(tmp_path / 'nope.yaml'))
        assert result is None


# ---------------------------------------------------------------------------
# load_supplier_categories
# ---------------------------------------------------------------------------
class TestLoadSupplierCategories:
    def _write(self, tmp_path, data):
        p = str(tmp_path / 'supplier.yaml')
        dump_file(data, p)
        return p

    def test_basic_load(self, tmp_path):
        data = {'Capacitors': {'Ceramic': ['MLCC']}}
        p = self._write(tmp_path, data)
        assert load_supplier_categories(p) == data

    def test_clean_removes_filter_prefix(self, tmp_path):
        data = {'Capacitors': {f'{FUNCTION_FILTER_KEY}Ceramic': ['MLCC']}}
        p = self._write(tmp_path, data)
        result = load_supplier_categories(p, clean=True)
        assert 'Ceramic' in result['Capacitors']
        assert f'{FUNCTION_FILTER_KEY}Ceramic' not in result['Capacitors']


# ---------------------------------------------------------------------------
# load_supplier_categories_inversed
# ---------------------------------------------------------------------------
class TestLoadSupplierCategoriesInversed:
    def test_inversion(self, tmp_path):
        data = {'Capacitors': {'Ceramic': ['MLCC', 'Disc']}}
        p = str(tmp_path / 'supplier.yaml')
        dump_file(data, p)
        result = load_supplier_categories_inversed(p)
        assert result['Capacitors']['MLCC'] == 'Ceramic'
        assert result['Capacitors']['Disc'] == 'Ceramic'

    def test_missing_file(self, tmp_path):
        result = load_supplier_categories_inversed(str(tmp_path / 'nope.yaml'))
        assert result is None


# ---------------------------------------------------------------------------
# load_category_parameters
# ---------------------------------------------------------------------------
class TestLoadCategoryParameters:
    def test_basic_mapping(self, tmp_path):
        data = {
            'Capacitors': {
                'Capacitance': ['Cap', 'Capacitance Value'],
                'Voltage': ['Rated Voltage'],
            }
        }
        p = str(tmp_path / 'params.yaml')
        dump_file(data, p)
        result = load_category_parameters(['Capacitors'], p)
        assert result['Cap'] == 'Capacitance'
        assert result['Capacitance Value'] == 'Capacitance'
        assert result['Rated Voltage'] == 'Voltage'

    def test_parent_inheritance(self, tmp_path):
        data = {
            'Passives': {
                'Package': ['Package Type'],
            },
            'Capacitors': {
                'parent': ['Passives'],
                'Capacitance': ['Cap'],
            },
        }
        p = str(tmp_path / 'params.yaml')
        dump_file(data, p)
        result = load_category_parameters(['Capacitors'], p)
        assert result['Package Type'] == 'Package'
        assert result['Cap'] == 'Capacitance'
