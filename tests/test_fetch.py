import io
import json
import socket
import tarfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pooch
import pytest
import requests

from fwl_io.fetch import DownloadError, OfflineDataError, _is_transient, create_fetcher

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


def test_all_mirrors_dead_raises_download_error(sample_files, tmp_path, dead_url, monkeypatch):
    # A refused connection is transient, so the sole mirror would otherwise be
    # retried on the full production schedule; blank it to keep the test fast.
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', ())
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


def test_doi_whitespace_never_reaches_a_mirror_url(sample_files, tmp_path):
    """A DOI carrying a line break resolves to the same clean mirror and path."""
    base_url, registry = sample_files
    fetcher = _fetcher(
        base_url, registry, tmp_path, zenodo=f'{ZENODO}\n', dataverse=' 10.34894/ABCDEF '
    )
    assert all('\n' not in mirror and ' ' not in mirror for mirror in fetcher.mirrors)
    assert f'doi:{ZENODO}/' in fetcher.mirrors
    # Discrimination: the version directory is the record id alone, so the
    # whitespace cannot leak into the on-disk layout either.
    assert fetcher.rel_dir == VERSIONED


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


# --- archive extraction -------------------------------------------------------

ARCHIVE_MEMBERS = [('m0p1.txt', b'0.1\n'), ('nested/m1p0.txt', b'1.0\n')]


def _serve_archive(root, name, members, kind, *, compression='gz'):
    """Build a tar/zip archive in the served root; return its {name: sha256} registry."""
    path = Path(root) / name
    if kind == 'tar':
        mode = f'w:{compression}' if compression else 'w'
        with tarfile.open(path, mode) as tf:
            for member, data in members:
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    else:
        with zipfile.ZipFile(path, 'w') as zf:
            for member, data in members:
                zf.writestr(member, data)
    return {name: 'sha256:' + pooch.file_hash(str(path), alg='sha256')}


def _archive_fetcher(base_url, registry, data_root, kind):
    return create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=[base_url],
        zenodo=ZENODO,
        data_root=data_root,
        extract=kind,
    )


@pytest.mark.parametrize('kind', ['tar', 'zip'])
def test_fetch_extracts_archive_into_version_dir_and_drops_it(http_server, tmp_path, kind):
    """An archive dataset is extracted into the version dir; the archive is not kept."""
    base_url, root = http_server
    name = f'tracks.{kind}'
    registry = _serve_archive(root, name, ARCHIVE_MEMBERS, kind)
    fetcher = _archive_fetcher(base_url, registry, tmp_path, kind)

    paths = fetcher.fetch_all()

    version_dir = tmp_path / VERSIONED
    got = sorted(p.relative_to(version_dir).as_posix() for p in paths)
    assert got == ['m0p1.txt', 'nested/m1p0.txt']
    assert (version_dir / 'm0p1.txt').read_bytes() == b'0.1\n'
    # A nested member keeps its subdirectory rather than being flattened.
    assert (version_dir / 'nested' / 'm1p0.txt').read_bytes() == b'1.0\n'
    # The archive itself is discarded; only the extracted tree and the stamp remain.
    assert not (version_dir / name).exists()
    stamp = json.loads((version_dir / '.fwl-io.json').read_text())
    assert stamp['extract'] == kind
    assert not any((tmp_path / '.fwl-io-staging').iterdir()), 'staging clean after extraction'


def test_archive_refetch_uses_the_stamp_and_does_not_redownload(http_server, tmp_path):
    """A current stamp short-circuits re-download and re-extraction.

    After a successful fetch the served archive is removed, so any re-download
    would 404; a second fetch_all must still return the extracted tree, proving
    it did not touch the network.
    """
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    first = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    (Path(root) / 'tracks.tar').unlink()

    second = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    assert sorted(first) == sorted(second)
    assert (tmp_path / VERSIONED / 'm0p1.txt').read_bytes() == b'0.1\n'


def test_archive_offline_serves_extracted_tree_and_errors_when_absent(http_server, tmp_path):
    """Offline mode returns an already-extracted dataset and errors when it is missing."""
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    # Not yet extracted and offline -> a clear OfflineDataError, no download attempt.
    with pytest.raises(OfflineDataError):
        _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all(offline=True)
    # Extract online, then a subsequent offline call returns the tree.
    _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all(offline=True)
    assert (tmp_path / VERSIONED / 'm0p1.txt') in paths


def test_corrupt_archive_fails_and_extracts_nothing(http_server, tmp_path, monkeypatch):
    """When every mirror fails the archive download, nothing is extracted.

    pooch.retrieve is stubbed to fail (as a checksum mismatch would) for every
    mirror, so the test is hermetic (no fall-through to the real doi.org
    resolver) and exercises the exhausted-mirrors path for an archive.
    """
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')

    def boom(*args, **kwargs):
        raise ValueError('hash of downloaded file does not match the known hash')

    monkeypatch.setattr('pooch.retrieve', boom)
    fetcher = _archive_fetcher(base_url, registry, tmp_path, 'tar')
    with pytest.raises(DownloadError):
        fetcher.fetch_all()
    assert not (tmp_path / VERSIONED).exists()


def test_archive_heals_a_deleted_member_on_refetch(http_server, tmp_path):
    """A member deleted from the extracted tree is restored on the next fetch.

    The stamp records the member names, so a missing member fails the intact
    check and the archive is re-extracted rather than served incomplete.
    """
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    victim = tmp_path / VERSIONED / 'nested' / 'm1p0.txt'
    victim.unlink()
    assert not victim.exists()

    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    # The deleted member is back, restored from a re-extraction (not served short).
    assert victim.read_bytes() == b'1.0\n'
    assert victim in paths


def test_a_plain_stamp_does_not_pass_for_an_extracted_tree(http_server, tmp_path):
    """A deposit fetched whole, then declared an archive, is extracted.

    The stamp a per-file fetch leaves behind describes the same record id, so
    only the recorded archive kind tells the two fetches apart.
    """
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    plain = create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=[base_url], zenodo=ZENODO, data_root=tmp_path
    )
    plain.fetch_all()
    version_dir = tmp_path / VERSIONED
    assert (version_dir / 'tracks.tar').is_file(), 'the plain fetch keeps the archive'

    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()

    assert sorted(p.relative_to(version_dir).as_posix() for p in paths) == [
        'm0p1.txt',
        'nested/m1p0.txt',
    ]
    # The archive is gone and the tree is in its place, so the second fetch did
    # the extraction rather than trusting the stamp of the first.
    assert not (version_dir / 'tracks.tar').exists()


def test_a_stamp_from_a_different_archive_kind_is_not_intact(http_server, tmp_path):
    """Switching the declared archive kind re-extracts rather than serving the old tree.

    Both spellings of the deposit carry the same member names, so only the kind
    recorded in the stamp distinguishes the tree already on disk from the one
    the manifest now asks for.
    """
    base_url, root = http_server
    zip_members = (('m0p1.txt', b'from the zip\n'),)
    tar_members = (('m0p1.txt', b'from the tar\n'),)
    zip_registry = _serve_archive(root, 'tracks.zip', zip_members, 'zip')
    tar_registry = _serve_archive(root, 'tracks.tar', tar_members, 'tar')
    _archive_fetcher(base_url, zip_registry, tmp_path, 'zip').fetch_all()
    member = tmp_path / VERSIONED / 'm0p1.txt'
    assert member.read_bytes() == b'from the zip\n'

    paths = _archive_fetcher(base_url, tar_registry, tmp_path, 'tar').fetch_all()

    assert member.read_bytes() == b'from the tar\n'
    assert paths == [member]


def test_a_stamp_recording_no_members_is_not_intact(http_server, tmp_path):
    """An empty member list describes no tree, so it cannot mark one complete."""
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    version_dir = tmp_path / VERSIONED
    stamp_path = version_dir / '.fwl-io.json'
    record = json.loads(stamp_path.read_text())
    record['members'] = []
    stamp_path.write_text(json.dumps(record))
    for member in ('m0p1.txt', 'nested/m1p0.txt'):
        (version_dir / member).unlink()

    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()

    assert len(paths) == 2, 'the emptied tree is re-extracted, not reported complete'
    assert (version_dir / 'm0p1.txt').read_bytes() == b'0.1\n'


def test_archive_member_named_like_the_stamp_belongs_to_the_dataset(http_server, tmp_path):
    """Only the stamp at the top of the version directory is fwl-io's own."""
    base_url, root = http_server
    members = (('m0p1.txt', b'0.1\n'), ('nested/.fwl-io.json', b'{"deposit": true}\n'))
    registry = _serve_archive(root, 'tracks.tar', members, 'tar')

    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()

    version_dir = tmp_path / VERSIONED
    assert sorted(p.relative_to(version_dir).as_posix() for p in paths) == [
        'm0p1.txt',
        'nested/.fwl-io.json',
    ]
    # The member keeps the deposit's content, and the stamp is written beside it.
    assert (version_dir / 'nested' / '.fwl-io.json').read_bytes() == b'{"deposit": true}\n'
    assert 'record_id' in json.loads((version_dir / '.fwl-io.json').read_text())


def test_shared_cache_serves_an_archive_dataset_offline(http_server, tmp_path, monkeypatch):
    """A cluster node with a populated cache needs no network for an archive."""
    base_url, root = http_server
    registry = _serve_archive(root, 'tracks.tar', ARCHIVE_MEMBERS, 'tar')
    cache_root = tmp_path / 'shared_cache'
    _archive_fetcher(base_url, registry, cache_root, 'tar').fetch_all()
    assert not (cache_root / VERSIONED / 'tracks.tar').exists(), 'the cache holds the tree'

    data_root = tmp_path / 'private'
    monkeypatch.setenv('FWL_DATA_CACHE', str(cache_root))
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=['http://127.0.0.1:1/'],
        zenodo=ZENODO,
        data_root=data_root,
        extract='tar',
    )

    paths = fetcher.fetch_all(offline=True)

    version_dir = data_root / VERSIONED
    assert sorted(p.relative_to(version_dir).as_posix() for p in paths) == [
        'm0p1.txt',
        'nested/m1p0.txt',
    ]
    # Discrimination: the copy is attributed to the cache, not to a mirror, and
    # the same call without the cache is refused offline.
    assert fetcher.provenance()[0]['source'] == f'cache:{cache_root}'
    monkeypatch.delenv('FWL_DATA_CACHE')
    with pytest.raises(OfflineDataError):
        _archive_fetcher(base_url, registry, tmp_path / 'other', 'tar').fetch_all(offline=True)


def test_archive_extracts_a_top_level_directory_member(http_server, tmp_path):
    """A tar whose members sit under a top-level directory keeps that structure.

    This is the common real-Zenodo layout (files inside one wrapping folder).
    """
    base_url, root = http_server
    members = [('grid/t0.txt', b'0\n'), ('grid/sub/t1.txt', b'1\n')]
    registry = _serve_archive(root, 'grid.tar', members, 'tar')
    paths = _archive_fetcher(base_url, registry, tmp_path, 'tar').fetch_all()
    version_dir = tmp_path / VERSIONED
    got = sorted(p.relative_to(version_dir).as_posix() for p in paths)
    assert got == ['grid/sub/t1.txt', 'grid/t0.txt']
    assert (version_dir / 'grid' / 't0.txt').read_bytes() == b'0\n'


@pytest.mark.unit
def test_extract_requires_a_zenodo_pin(tmp_path):
    """An archive dataset without a Zenodo pin is refused at construction."""
    with pytest.raises(ValueError, match='requires a Zenodo version DOI'):
        create_fetcher(
            subdir=SUBDIR,
            registry={'a.tar': 'sha256:aaa'},
            base_urls=['http://unused/'],
            data_root=tmp_path,
            extract='tar',
        )


def test_malicious_archive_aborts_with_no_dataset_and_clean_staging(http_server, tmp_path):
    """A traversing member aborts extraction, leaving no dataset dir and clean staging."""
    from fwl_io.archive import ArchiveError

    base_url, root = http_server
    registry = _serve_archive(
        root, 'evil.tar', [('ok.txt', b'ok\n'), ('../evil.txt', b'PWNED\n')], 'tar'
    )
    fetcher = _archive_fetcher(base_url, registry, tmp_path, 'tar')
    with pytest.raises(ArchiveError, match='escapes the destination'):
        fetcher.fetch_all()
    # Aborted before placement: the version dir was never created and the staged
    # work directory was cleaned up, so no half-populated tree is left behind.
    assert not (tmp_path / VERSIONED).exists()
    assert not any((tmp_path / '.fwl-io-staging').iterdir())


@pytest.mark.unit
def test_extract_with_multi_file_registry_rejected(tmp_path):
    """An archive dataset must list exactly one archive; two entries is a config error."""
    with pytest.raises(ValueError, match='exactly one archive'):
        create_fetcher(
            subdir=SUBDIR,
            registry={'a.tar': 'sha256:aaa', 'b.tar': 'sha256:bbb'},
            base_urls=['http://unused/'],
            zenodo=ZENODO,
            data_root=tmp_path,
            extract='tar',
        )


@pytest.mark.unit
def test_extract_unknown_kind_rejected(tmp_path):
    """An unknown extract kind fails at construction, listing the valid kinds."""
    with pytest.raises(ValueError, match='unknown extract kind'):
        create_fetcher(
            subdir=SUBDIR,
            registry={'a.rar': 'sha256:aaa'},
            base_urls=['http://unused/'],
            zenodo=ZENODO,
            data_root=tmp_path,
            extract='rar',
        )


@pytest.mark.unit
def test_transient_failure_is_retried_then_succeeds(tmp_path, monkeypatch):
    """A read timeout on the first attempt is retried and the fetch then succeeds.

    The stubbed download fails once with a transient timeout, so the retry loop
    must call pooch a second time (where it serves the file) rather than failing
    the fetch, sleeping once on the backoff schedule between the two attempts.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01, 0.01))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise requests.exceptions.ReadTimeout('read timed out')
        served = Path(kwargs['path']) / kwargs['fname']
        served.write_bytes(b'payload\n')
        return str(served)

    monkeypatch.setattr('pooch.retrieve', flaky)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://unused/'],
        data_root=tmp_path,
    )
    path = fetcher.fetch('alpha.dat')
    assert path.read_bytes() == b'payload\n'
    assert calls['n'] == 2, 'exactly one retry after the transient failure'
    # One wait, not zero (no retry) and not two (an extra attempt); the second
    # attempt succeeds so the schedule is not consumed further.
    assert sleeps == [0.01]


@pytest.mark.unit
def test_permanent_failure_is_not_retried(tmp_path, monkeypatch):
    """A checksum mismatch fails at once without consuming the retry budget.

    A permanent failure re-fails identically on a retry, so the loop must not
    sleep or re-attempt; with a non-empty schedule available it still surfaces
    as a DownloadError after a single call.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01, 0.01, 0.01))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def permanent(*args, **kwargs):
        calls['n'] += 1
        raise ValueError('hash of downloaded file does not match the known hash')

    monkeypatch.setattr('pooch.retrieve', permanent)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://unused/'],
        data_root=tmp_path,
    )
    with pytest.raises(DownloadError, match='alpha.dat'):
        fetcher.fetch('alpha.dat')
    assert calls['n'] == 1, 'a checksum mismatch is not retried'
    assert sleeps == [], 'no backoff wait for a permanent failure'


@pytest.mark.unit
def test_transient_failure_exhausts_schedule_then_raises(tmp_path, monkeypatch):
    """A mirror that times out on every attempt fails after the whole schedule.

    With two backoff entries the loop makes three attempts and sleeps twice,
    then raises DownloadError; a persistently unreachable mirror is retried a
    bounded number of times, never forever.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01, 0.02))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def always_timeout(*args, **kwargs):
        calls['n'] += 1
        raise requests.exceptions.ConnectTimeout('connect timed out')

    monkeypatch.setattr('pooch.retrieve', always_timeout)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://unused/'],
        data_root=tmp_path,
    )
    with pytest.raises(DownloadError):
        fetcher.fetch('alpha.dat')
    assert calls['n'] == 3, 'two backoff entries means three attempts'
    assert sleeps == [0.01, 0.02], 'one wait per retry, in schedule order'


@pytest.mark.unit
def test_downloader_carries_explicit_timeout(tmp_path):
    """Both mirror kinds build a downloader with the bounded request timeout.

    A stalled socket must fail in bounded time on the DOI and the direct-URL
    path alike, so neither may inherit pooch's downloader-specific default, and
    the two mirror kinds must resolve to their distinct downloader classes.
    """
    from fwl_io.fetch import _DOWNLOAD_TIMEOUT_S

    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'a.dat': 'sha256:aaa'},
        base_urls=['http://example.invalid/'],
        zenodo=ZENODO,
        data_root=tmp_path,
    )
    http_dl = fetcher._downloader('http://example.invalid/')
    doi_dl = fetcher._downloader(f'doi:{ZENODO}/')
    assert http_dl.kwargs.get('timeout') == _DOWNLOAD_TIMEOUT_S
    assert doi_dl.timeout == _DOWNLOAD_TIMEOUT_S
    assert type(http_dl).__name__ == 'HTTPDownloader'
    assert type(doi_dl).__name__ == 'DOIDownloader'


def test_real_server_503_then_200_is_retried_and_served(tmp_path, monkeypatch):
    """A mirror answering 503 once, then 200, is retried through the real stack.

    This exercises pooch and requests rather than a stubbed retrieve, proving a
    5xx from a real socket is classified transient and the second attempt is
    served with the correct bytes.
    """
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = b'0.5 0.6 0.7\n'
    state = {'hits': 0}

    class _FlakyHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 -- silence request logging
            pass

        def do_GET(self):  # noqa: N802 -- http.server dispatch name
            state['hits'] += 1
            if state['hits'] == 1:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'busy')
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), _FlakyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f'http://{host}:{port}/'
    try:
        monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01,))
        monkeypatch.setattr('time.sleep', lambda s: None)
        registry = {'gamma.dat': 'sha256:' + hashlib.sha256(payload).hexdigest()}
        fetcher = _fetcher(base_url, registry, tmp_path)
        path = fetcher.fetch('gamma.dat')
        assert path.read_bytes() == payload
        assert state['hits'] == 2, 'one 503 then one served response'
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.unit
def test_mid_stream_drop_is_retried_then_succeeds(tmp_path, monkeypatch):
    """A connection dropped mid-download is transient and is retried.

    requests surfaces a stream truncated part-way through as ChunkedEncodingError,
    which is not a Timeout or ConnectionError subclass and carries no response; it
    must still be retried, matching the documented "dropped connection" contract,
    rather than treated as a permanent failure.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01,))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise requests.exceptions.ChunkedEncodingError('Connection broken: IncompleteRead')
        served = Path(kwargs['path']) / kwargs['fname']
        served.write_bytes(b'payload\n')
        return str(served)

    monkeypatch.setattr('pooch.retrieve', flaky)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://unused/'],
        data_root=tmp_path,
    )
    assert fetcher.fetch('alpha.dat').read_bytes() == b'payload\n'
    assert calls['n'] == 2, 'the mid-stream drop is retried, not abandoned'
    assert sleeps == [0.01]


@pytest.mark.unit
def test_http_404_is_permanent_and_not_retried(tmp_path, monkeypatch):
    """A 404 is a permanent HTTP error and consumes no retry budget.

    This exercises the status branch of the classifier (a real HTTPError carrying
    a 404 response), distinct from the checksum-mismatch ValueError path, so a
    regression that widened retry to 4xx would be caught here.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01, 0.01, 0.01))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def not_found(*args, **kwargs):
        calls['n'] += 1
        response = requests.Response()
        response.status_code = 404
        raise requests.exceptions.HTTPError('404 Client Error', response=response)

    monkeypatch.setattr('pooch.retrieve', not_found)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://unused/'],
        data_root=tmp_path,
    )
    with pytest.raises(DownloadError, match='alpha.dat'):
        fetcher.fetch('alpha.dat')
    assert calls['n'] == 1, 'a 404 is not retried'
    assert sleeps == [], 'no backoff wait for a 404'


def test_healthy_mirror_used_without_waiting_out_backoff(
    sample_files, tmp_path, dead_url, monkeypatch
):
    """A transient failure on the first mirror falls over at once, not after backoff.

    With the full production schedule installed and time.sleep recorded, a dead
    first mirror must hand off to the healthy second mirror within the first
    round, so no backoff wait is spent; per-mirror retry would instead burn the
    whole 100 s schedule on the dead mirror before trying the second.
    """
    base_url, registry = sample_files
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (10.0, 30.0, 60.0))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry=registry,
        base_urls=[dead_url, base_url],
        data_root=tmp_path,
    )
    assert fetcher.fetch('beta.dat').read_bytes() == b'columns\n1 2\n3 4\n'
    assert sleeps == [], 'the healthy mirror is reached without waiting out the backoff'


def test_incomplete_read_from_real_server_is_retried_and_served(tmp_path, monkeypatch):
    """A real truncated response is classified transient through the pooch stack.

    The mirror declares more bytes than it sends on the first request, then closes
    the socket, forcing a real ChunkedEncodingError out of requests rather than a
    fabricated one; the retry then downloads the full, checksum-matching file.
    """
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = b'1.0 2.0 3.0 4.0\n'
    state = {'hits': 0}

    class _TruncatingHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 -- silence request logging
            pass

        def do_GET(self):  # noqa: N802 -- http.server dispatch name
            state['hits'] += 1
            self.send_response(200)
            if state['hits'] == 1:
                # Promise more than we deliver, then drop the connection.
                self.send_header('Content-Length', str(len(payload) + 64))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), _TruncatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f'http://{host}:{port}/'
    try:
        monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01,))
        monkeypatch.setattr('time.sleep', lambda s: None)
        registry = {'delta.dat': 'sha256:' + hashlib.sha256(payload).hexdigest()}
        fetcher = _fetcher(base_url, registry, tmp_path)
        assert fetcher.fetch('delta.dat').read_bytes() == payload
        assert state['hits'] == 2, 'one truncated response then one complete one'
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.unit
def test_production_retry_and_timeout_constants():
    """Pin the shipped retry schedule and timeout independent of test overrides.

    Every other retry test monkeypatches ``_RETRY_BACKOFF_S`` to a fast local
    value that reverts on teardown, so nothing else asserts the production
    values; without this pin an edit that disabled retry or changed the timeout
    would pass the whole suite unnoticed.
    """
    from fwl_io.fetch import _DOWNLOAD_TIMEOUT_S, _RETRY_BACKOFF_S

    assert _RETRY_BACKOFF_S == (10.0, 30.0, 60.0)
    # (connect, read): a short connect budget fails a dead mirror fast, a longer
    # read budget tolerates a slow but live transfer.
    assert _DOWNLOAD_TIMEOUT_S == (10.0, 60.0)
    assert _DOWNLOAD_TIMEOUT_S[0] < _DOWNLOAD_TIMEOUT_S[1]


def _http_error(status: int) -> requests.exceptions.HTTPError:
    """An HTTPError carrying a response with the given status, as raise_for_status builds it."""
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(str(status), response=response)


@pytest.mark.unit
@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_retryable_http_status_is_transient(status):
    """Every status in the retryable set is classified transient.

    Pinning the whole set (not just the 503 the real-server test exercises) means
    dropping 429 rate-limiting or a 5xx gateway error from the policy is caught
    here rather than silently making those failures permanent.
    """
    assert _is_transient(_http_error(status)) is True


@pytest.mark.unit
@pytest.mark.parametrize('status', [400, 403, 404, 410, 501, 505])
def test_non_retryable_http_status_is_permanent(status):
    """A 4xx other than 408/429, and the permanent 5xx codes, are not retried.

    A regression widening the policy to blanket 4xx (retrying a 404) or to every
    5xx (retrying 501/505) would flip one of these and fail here.
    """
    assert _is_transient(_http_error(status)) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    'exc',
    [
        requests.exceptions.ReadTimeout('read timed out'),
        requests.exceptions.ConnectTimeout('connect timed out'),
        requests.exceptions.ConnectionError('connection refused'),
        requests.exceptions.SSLError('ssl read error'),
        requests.exceptions.ChunkedEncodingError('IncompleteRead'),
        requests.exceptions.ContentDecodingError('corrupt gzip body'),
        requests.exceptions.JSONDecodeError('metadata', '', 0),
    ],
)
def test_transport_and_metadata_errors_are_transient(exc):
    """Transport-level failures and a malformed DOI-metadata response are retried.

    The last case guards the DOI path: pooch resolves a Zenodo/Dataverse DOI
    through an API call that does not raise for status, so a 5xx there surfaces as
    a JSONDecodeError rather than an HTTPError and must still be treated transient.
    """
    assert _is_transient(exc) is True


@pytest.mark.unit
def test_checksum_mismatch_and_responseless_http_error_are_permanent():
    """A checksum ValueError and a response-less HTTPError are not retried.

    pooch signals a hash mismatch with a plain ValueError, which must not be
    confused with the DOI-metadata JSONDecodeError (a ValueError subclass); and an
    HTTPError with no response carries no status to trust, so neither is retried.
    """
    assert _is_transient(ValueError('hash of downloaded file does not match')) is False
    assert isinstance(requests.exceptions.JSONDecodeError('m', '', 0), ValueError)  # the trap
    assert _is_transient(requests.exceptions.HTTPError('no response attached')) is False


@pytest.mark.unit
def test_round_with_a_transient_mirror_retries_despite_a_permanent_one(tmp_path, monkeypatch):
    """A round mixing one transient and one permanent mirror still retries.

    The retriable decision is OR-ed over every mirror tried in the round, so a
    transient failure on one mirror keeps the schedule alive even when another
    mirror in the same round fails permanently, and both mirrors are tried every
    round rather than the round being abandoned on the permanent one.
    """
    monkeypatch.setattr('fwl_io.fetch._RETRY_BACKOFF_S', (0.01, 0.02))
    sleeps: list[float] = []
    monkeypatch.setattr('time.sleep', lambda s: sleeps.append(s))

    calls = {'n': 0}

    def mixed(*args, **kwargs):
        calls['n'] += 1
        if 'transient/' in kwargs['url']:
            raise requests.exceptions.ConnectTimeout('connect timed out')
        raise ValueError('hash of downloaded file does not match the known hash')

    monkeypatch.setattr('pooch.retrieve', mixed)
    fetcher = create_fetcher(
        subdir=SUBDIR,
        registry={'alpha.dat': 'sha256:' + '0' * 64},
        base_urls=['http://transient/', 'http://permanent/'],
        data_root=tmp_path,
    )
    with pytest.raises(DownloadError):
        fetcher.fetch('alpha.dat')
    # Two mirrors across three rounds; a per-mirror-overwrite of retriable would
    # abandon after the permanent mirror and give calls == 2, sleeps == [].
    assert calls['n'] == 6, 'two mirrors tried across three rounds'
    assert sleeps == [0.01, 0.02], 'the transient mirror keeps the round retriable'
