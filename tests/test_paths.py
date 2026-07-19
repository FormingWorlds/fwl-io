from pathlib import Path

import pytest

from fwl_io.paths import is_offline, resolve_cache_root, resolve_data_root

pytestmark = pytest.mark.unit


def test_explicit_root_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv('FWL_DATA', str(tmp_path / 'from_env'))
    explicit = tmp_path / 'explicit'
    assert resolve_data_root(explicit) == explicit.absolute()


def test_env_root_used_when_no_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv('FWL_DATA', str(tmp_path / 'from_env'))
    root = resolve_data_root()
    assert root == (tmp_path / 'from_env').absolute()
    assert root.is_dir()


def test_platform_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(
        'platformdirs.user_data_dir', lambda name: str(tmp_path / 'platform' / name)
    )
    assert resolve_data_root() == (tmp_path / 'platform' / 'fwl_data').absolute()


def test_cache_root_requires_existing_directory(tmp_path, monkeypatch):
    assert resolve_cache_root() is None
    monkeypatch.setenv('FWL_DATA_CACHE', str(tmp_path / 'missing'))
    assert resolve_cache_root() is None
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setenv('FWL_DATA_CACHE', str(cache))
    assert resolve_cache_root() == cache.absolute()


@pytest.mark.parametrize(
    ('value', 'expected'),
    [('1', True), ('true', True), ('YES', True), ('on', True), ('0', False), ('', False)],
)
def test_offline_env_parsing(monkeypatch, value, expected):
    monkeypatch.setenv('FWL_IO_OFFLINE', value)
    assert is_offline() is expected


def test_data_root_is_created(tmp_path):
    target = tmp_path / 'fresh' / 'tree'
    root = resolve_data_root(target)
    assert root.is_dir()
    assert isinstance(root, Path)
