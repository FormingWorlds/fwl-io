import pytest

from fwl_io.fetch import DownloadError, OfflineDataError, create_fetcher

pytestmark = pytest.mark.integration

SUBDIR = 'interior_lookup_tables/demo'


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
    assert not list(path.parent.glob('tmp*')), 'no temporary files left behind'


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


def test_mirror_fallback_when_first_mirror_dead(sample_files, tmp_path):
    base_url, registry = sample_files
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=['http://127.0.0.1:9/', base_url],  # port 9: connection refused
        data_root=tmp_path,
    )
    assert fetcher.fetch('beta.dat').is_file()


def test_all_mirrors_dead_raises_download_error(sample_files, tmp_path):
    _, registry = sample_files
    fetcher = create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=['http://127.0.0.1:9/'], data_root=tmp_path
    )
    with pytest.raises(DownloadError, match='alpha.dat'):
        fetcher.fetch('alpha.dat')


def test_offline_mode_blocks_download_but_serves_local(sample_files, tmp_path, monkeypatch):
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    fetched = fetcher.fetch('alpha.dat')

    monkeypatch.setenv('FWL_IO_OFFLINE', '1')
    assert fetcher.fetch('alpha.dat') == fetched
    with pytest.raises(OfflineDataError, match='beta.dat|FWL_IO_OFFLINE'):
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
        subdir=SUBDIR, registry=registry, base_urls=['http://127.0.0.1:9/'], data_root=data_root
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


@pytest.mark.unit
def test_provenance_lists_every_file(sample_files, tmp_path):
    base_url, registry = sample_files
    records = _fetcher(base_url, registry, tmp_path).provenance()
    assert {r['file'] for r in records} == {f'{SUBDIR}/alpha.dat', f'{SUBDIR}/beta.dat'}
    assert all(r['checksum'].startswith('sha256:') for r in records)
