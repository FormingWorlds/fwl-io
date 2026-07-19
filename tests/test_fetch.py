import socket
from pathlib import Path

import pytest

from fwl_io.fetch import DownloadError, OfflineDataError, create_fetcher

pytestmark = pytest.mark.integration

SUBDIR = 'interior_lookup_tables/demo'


@pytest.fixture()
def dead_url():
    """A URL on a port that is guaranteed closed (bound, then released)."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return f'http://127.0.0.1:{port}/'


def _fetcher(base_url, registry, data_root, **kwargs):
    return create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=[base_url], data_root=data_root, **kwargs
    )


def test_download_verify_and_place(sample_files, tmp_path):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    path = fetcher.fetch('alpha.dat')
    assert path == tmp_path / SUBDIR / 'alpha.dat'
    assert path.read_bytes() == b'0.1 0.2 0.3\n'
    assert not list(path.parent.glob('tmp*')), 'no temporary files in the dataset dir'
    staging = tmp_path / '.fwl-io-staging'
    assert not any(staging.iterdir()), 'staging is empty after a clean fetch'


def test_existing_valid_file_is_not_refetched(sample_files, tmp_path):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    first = fetcher.fetch('alpha.dat')
    stamp = first.stat().st_mtime_ns
    assert fetcher.fetch('alpha.dat').stat().st_mtime_ns == stamp


def test_corrupt_file_is_refetched(sample_files, tmp_path):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    target = fetcher.fetch('alpha.dat')
    target.write_bytes(b'corrupted')
    assert fetcher.fetch('alpha.dat').read_bytes() == b'0.1 0.2 0.3\n'


def test_nested_registry_name_is_placed_below_dataset_dir(http_server, tmp_path):
    import pooch

    base_url, root = http_server
    nested = Path(root) / 'sub' / 'nested.dat'
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b'nested payload\n')
    registry = {'sub/nested.dat': 'sha256:' + pooch.file_hash(str(nested), alg='sha256')}
    fetcher = _fetcher(base_url, registry, tmp_path)
    path = fetcher.fetch('sub/nested.dat')
    assert path == tmp_path / SUBDIR / 'sub' / 'nested.dat'
    assert path.read_bytes() == b'nested payload\n'


def test_traversal_registry_name_rejected(tmp_path):
    with pytest.raises(ValueError, match=r'\.\.'):
        create_fetcher(
            subdir=SUBDIR,
            registry={'../escape.dat': 'sha256:aaa'},
            base_urls=['http://unused/'],
            data_root=tmp_path,
        )


def test_traversal_subdir_rejected(tmp_path):
    with pytest.raises(ValueError, match='escapes the data root'):
        create_fetcher(
            subdir='../outside',
            registry={'a.dat': 'sha256:aaa'},
            base_urls=['http://unused/'],
            data_root=tmp_path,
        )


def test_mirror_fallback_when_first_mirror_dead(sample_files, tmp_path, dead_url):
    base_url, registry = sample_files
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=[dead_url, base_url],
        data_root=tmp_path,
    )
    assert fetcher.fetch('beta.dat').is_file()


def test_all_mirrors_dead_raises_download_error(sample_files, tmp_path, dead_url):
    _, registry = sample_files
    fetcher = create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=[dead_url], data_root=tmp_path
    )
    with pytest.raises(DownloadError, match='alpha.dat'):
        fetcher.fetch('alpha.dat')


def test_local_placement_failure_is_not_a_mirror_failure(sample_files, tmp_path):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    blocker = tmp_path / 'interior_lookup_tables'
    blocker.mkdir(parents=True)
    blocker.chmod(0o500)  # download succeeds, placement cannot create the dataset dir
    try:
        with pytest.raises(PermissionError):
            fetcher.fetch('alpha.dat')
    finally:
        blocker.chmod(0o755)


def test_offline_mode_blocks_download_but_serves_local(sample_files, tmp_path, monkeypatch):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    fetched = fetcher.fetch('alpha.dat')

    monkeypatch.setenv('FWL_IO_OFFLINE', '1')
    assert fetcher.fetch('alpha.dat') == fetched
    with pytest.raises(OfflineDataError, match=r'beta\.dat'):
        fetcher.fetch('beta.dat')


def test_shared_cache_is_used_before_download(sample_files, tmp_path, monkeypatch):
    base_url, registry = sample_files

    cache_root = tmp_path / 'shared_cache'
    populate = _fetcher(base_url, registry, cache_root)
    populate.fetch_all()

    data_root = tmp_path / 'private'
    monkeypatch.setenv('FWL_DATA_CACHE', str(cache_root))
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')  # proves no network is needed
    fetcher = create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=['http://unused/'], data_root=data_root
    )
    path = fetcher.fetch('alpha.dat')
    assert path == data_root / SUBDIR / 'alpha.dat'
    assert path.read_bytes() == b'0.1 0.2 0.3\n'


def test_unknown_file_rejected(sample_files, tmp_path):
    base_url, registry = sample_files
    with pytest.raises(KeyError, match='gamma.dat'):
        _fetcher(base_url, registry, tmp_path).fetch('gamma.dat')


@pytest.mark.unit
def test_empty_registry_rejected(tmp_path):
    with pytest.raises(ValueError, match='fwl-io sync'):
        create_fetcher(subdir=SUBDIR, registry={}, base_urls=['http://x/'], data_root=tmp_path)


@pytest.mark.unit
def test_no_source_rejected(tmp_path):
    with pytest.raises(ValueError, match='no data source'):
        create_fetcher(subdir=SUBDIR, registry={'a': 'sha256:x'}, data_root=tmp_path)


def test_provenance_reports_actual_sources(sample_files, tmp_path, monkeypatch):
    base_url, registry = sample_files

    cache_root = tmp_path / 'shared_cache'
    _fetcher(base_url, registry, cache_root).fetch('beta.dat')

    monkeypatch.setenv('FWL_DATA_CACHE', str(cache_root))
    fetcher = _fetcher(base_url, registry, tmp_path / 'private')
    fetcher.fetch('alpha.dat')  # served by the mirror
    fetcher.fetch('beta.dat')  # served by the shared cache

    records = {r['file']: r for r in fetcher.provenance()}
    assert records[f'{SUBDIR}/alpha.dat']['source'] == base_url
    assert records[f'{SUBDIR}/beta.dat']['source'] == f'cache:{cache_root}'


def test_provenance_marks_unfetched_files_as_declared(sample_files, tmp_path):
    base_url, registry = sample_files
    records = {r['file']: r for r in _fetcher(base_url, registry, tmp_path).provenance()}
    assert all(r['source'].startswith('declared:') for r in records.values())
    assert all(r['checksum'].startswith('sha256:') for r in records.values())
