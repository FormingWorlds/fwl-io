import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fwl_io.fetch import DownloadError, OfflineDataError, create_fetcher

pytestmark = pytest.mark.integration

SUBDIR = 'interior_lookup_tables/demo'
RECID = '15729114'
ZENODO = f'10.5281/zenodo.{RECID}'
VERSIONED = f'{SUBDIR}/r{RECID}'


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


def test_zenodo_pin_lands_in_version_directory(sample_files, tmp_path):
    """A dataset with a Zenodo pin resolves into <subdir>/r<record-id>."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    path = fetcher.fetch('alpha.dat')
    assert path == tmp_path / VERSIONED / 'alpha.dat'
    assert path.read_bytes() == b'0.1 0.2 0.3\n'
    # Discrimination: the bare, unversioned path must never be created, or two
    # deposits of the same dataset would collide in one directory.
    assert not (tmp_path / SUBDIR / 'alpha.dat').exists()


def test_no_zenodo_pin_keeps_bare_subdir(sample_files, tmp_path):
    """Without a Zenodo pin the legacy bare-subdir layout is preserved."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path)
    path = fetcher.fetch('alpha.dat')
    assert path == tmp_path / SUBDIR / 'alpha.dat'
    # No version directory is invented for an unpinned (base_urls) source.
    assert not list((tmp_path / SUBDIR).glob('r*'))


def test_two_pinned_versions_coexist(sample_files, tmp_path):
    """Two record ids of one dataset occupy separate directories side by side."""
    base_url, registry = sample_files
    other_recid = '15729115'
    first = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO).fetch('alpha.dat')
    second = _fetcher(base_url, registry, tmp_path, zenodo=f'10.5281/zenodo.{other_recid}').fetch(
        'alpha.dat'
    )
    assert first.parent == tmp_path / SUBDIR / f'r{RECID}'
    assert second.parent == tmp_path / SUBDIR / f'r{other_recid}'
    assert first != second
    assert first.read_bytes() == second.read_bytes() == b'0.1 0.2 0.3\n'


def test_stamp_records_doi_checksums_and_date(sample_files, tmp_path):
    """fetch_all writes a .fwl-io.json stamp describing the version directory."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    fetcher.fetch_all()

    stamp_path = tmp_path / VERSIONED / '.fwl-io.json'
    assert stamp_path.is_file()
    stamp = json.loads(stamp_path.read_text())
    assert stamp['schema'] == 1
    assert stamp['zenodo'] == ZENODO
    assert stamp['record_id'] == RECID
    assert stamp['subdir'] == SUBDIR
    # The recorded checksums are the full registry, not an empty placeholder.
    assert stamp['files'] == registry
    # The fetch date is an ISO-8601 instant recorded in UTC, not merely aware.
    fetched = datetime.fromisoformat(stamp['fetched'])
    assert fetched.utcoffset() == timedelta(0)


def test_valid_stamp_is_not_churned_on_refetch(sample_files, tmp_path):
    """A valid stamp for the same record id survives a repeat fetch untouched."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    fetcher.fetch_all()
    stamp_path = tmp_path / VERSIONED / '.fwl-io.json'

    # Add a marker to the (still valid) stamp; a write-once-on-valid policy must
    # preserve it, proving no churn independently of the one-second clock tick.
    stamp = json.loads(stamp_path.read_text())
    stamp['marker'] = 'preserve-me'
    stamp_path.write_text(json.dumps(stamp))
    fetcher.fetch_all()
    assert json.loads(stamp_path.read_text())['marker'] == 'preserve-me'
    # No staging or temp residue is left beside the data.
    assert not list((tmp_path / VERSIONED).glob('.fwl-io-stamp-*'))


def test_single_fetch_does_not_stamp(sample_files, tmp_path):
    """The stamp is a fetch_all side effect; a lone fetch() leaves none."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    path = fetcher.fetch('alpha.dat')
    assert path.is_file()
    assert not (tmp_path / VERSIONED / '.fwl-io.json').exists()


def test_corrupt_stamp_is_healed(sample_files, tmp_path):
    """An unparseable existing stamp is rewritten, not trusted forever."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    fetcher.fetch_all()
    stamp_path = tmp_path / VERSIONED / '.fwl-io.json'
    stamp_path.write_text('not json at all {{{')

    fetcher.fetch_all()
    healed = json.loads(stamp_path.read_text())
    assert healed['record_id'] == RECID
    assert healed['zenodo'] == ZENODO


def test_mismatched_stamp_is_healed(sample_files, tmp_path):
    """A stamp whose record id disagrees with the pin is rewritten."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    fetcher.fetch_all()
    stamp_path = tmp_path / VERSIONED / '.fwl-io.json'
    wrong = json.loads(stamp_path.read_text())
    wrong['record_id'] = '99999999'
    wrong['zenodo'] = '10.5281/zenodo.99999999'
    stamp_path.write_text(json.dumps(wrong))

    fetcher.fetch_all()
    healed = json.loads(stamp_path.read_text())
    assert healed['record_id'] == RECID
    assert healed['zenodo'] == ZENODO


def test_stamp_write_failure_does_not_break_fetch(sample_files, tmp_path):
    """A read-only version dir cannot fail an otherwise complete fetch_all."""
    base_url, registry = sample_files
    # Populate and verify the dataset, then remove the stamp and lock the dir so
    # the stamp write is the only remaining write, and it must fail.
    _fetcher(base_url, registry, tmp_path, zenodo=ZENODO).fetch_all()
    version_dir = tmp_path / VERSIONED
    (version_dir / '.fwl-io.json').unlink()
    version_dir.chmod(0o500)
    try:
        fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
        paths = fetcher.fetch_all(offline=True)  # all files present; no download
        assert sorted(p.name for p in paths) == ['alpha.dat', 'beta.dat']
        assert not (version_dir / '.fwl-io.json').exists()  # write failed, but silently
        assert not list(version_dir.glob('.fwl-io-stamp-*'))  # no temp residue
    finally:
        version_dir.chmod(0o755)


def test_unpinned_dataset_is_not_stamped(sample_files, tmp_path):
    """A source without a Zenodo pin gets no stamp (nothing to describe by DOI)."""
    base_url, registry = sample_files
    _fetcher(base_url, registry, tmp_path).fetch_all()
    assert not list(tmp_path.rglob('.fwl-io.json'))


def test_shared_cache_lookup_uses_version_directory(sample_files, tmp_path, monkeypatch):
    """A pinned dataset is served from the cache at its versioned path."""
    base_url, registry = sample_files
    cache_root = tmp_path / 'shared_cache'
    _fetcher(base_url, registry, cache_root, zenodo=ZENODO).fetch_all()
    assert (cache_root / VERSIONED / 'alpha.dat').is_file()

    data_root = tmp_path / 'private'
    monkeypatch.setenv('FWL_DATA_CACHE', str(cache_root))
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')  # proves the cache, not the network, serves it
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=['http://unused/'],
        zenodo=ZENODO,
        data_root=data_root,
    )
    path = fetcher.fetch('alpha.dat')
    assert path == data_root / VERSIONED / 'alpha.dat'
    assert path.read_bytes() == b'0.1 0.2 0.3\n'


def test_cache_at_bare_subdir_is_ignored_for_pinned_dataset(sample_files, tmp_path, monkeypatch):
    """A cache populated at the legacy bare path is not used by a pinned fetch."""
    base_url, registry = sample_files
    cache_root = tmp_path / 'shared_cache'
    # Populate the cache at the unversioned path only.
    _fetcher(base_url, registry, cache_root).fetch_all()
    assert (cache_root / SUBDIR / 'alpha.dat').is_file()

    monkeypatch.setenv('FWL_DATA_CACHE', str(cache_root))
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=['http://unused/'],
        zenodo=ZENODO,
        data_root=tmp_path / 'private',
    )
    with pytest.raises(OfflineDataError, match=r'alpha\.dat'):
        fetcher.fetch('alpha.dat')


def test_provenance_reports_version_path(sample_files, tmp_path):
    """Provenance records point at the versioned on-disk location."""
    base_url, registry = sample_files
    fetcher = _fetcher(base_url, registry, tmp_path, zenodo=ZENODO)
    fetcher.fetch('alpha.dat')
    record = {r['file']: r for r in fetcher.provenance()}[f'{VERSIONED}/alpha.dat']
    assert record['source'] == base_url
    # Discrimination: the bare-subdir key is absent, so consumers cannot record
    # a path that does not exist on disk.
    assert f'{SUBDIR}/alpha.dat' not in {r['file'] for r in fetcher.provenance()}


@pytest.mark.unit
def test_malformed_zenodo_pin_rejected(tmp_path):
    """A non-Zenodo DOI passed as the pin fails loudly at construction."""
    with pytest.raises(ValueError, match='Zenodo DOI'):
        create_fetcher(
            subdir=SUBDIR,
            registry={'a.dat': 'sha256:aaa'},
            base_urls=['http://unused/'],
            zenodo='10.34894/ABCDEF',
            data_root=tmp_path,
        )
