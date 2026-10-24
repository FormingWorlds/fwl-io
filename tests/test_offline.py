"""Offline mode refuses every network call: data fetches and the maintainer commands."""

import hashlib
import socket
import time

import pytest

import fwl_io
from fwl_io import mirror, mirror_status, mirror_sync, paths, pins, sync
from fwl_io.cli import main
from fwl_io.fetch import create_fetcher

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

REFUSAL = 'needs the network and offline mode is active; unset FWL_IO_OFFLINE'
ZENODO_DOI = '10.5281/zenodo.15727899'


@pytest.fixture()
def blocked_net(monkeypatch, tmp_path):
    """Set offline mode, an empty data root, and record every socket call, which fails."""
    calls = []

    def refuse(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise OSError(f'network blocked by the test ({name})')

        return call

    monkeypatch.setattr(socket, 'getaddrinfo', refuse('getaddrinfo'))
    monkeypatch.setattr(socket, 'create_connection', refuse('create_connection'))
    monkeypatch.setattr(socket.socket, 'connect', refuse('connect'))
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse('connect_ex'))
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(mirror, '_sleep', lambda seconds: None)
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')
    (tmp_path / 'data').mkdir()
    monkeypatch.setenv('FWL_DATA', str(tmp_path / 'data'))
    monkeypatch.setenv('DATAVERSE_TOKEN', 'test-token')
    return calls


def _manifest(tmp_path):
    path = tmp_path / 'manifest.toml'
    path.write_text(f'[g.demo]\nzenodo = "{ZENODO_DOI}"\n')
    return path


def _assert_refused(call, calls):
    """Call ``call`` and require the offline refusal before any socket call."""
    with pytest.raises(Exception) as info:
        call()
    assert type(info.value).__name__ == 'OfflineModeError', repr(info.value)
    assert isinstance(info.value, fwl_io.OfflineModeError)
    assert REFUSAL in str(info.value)
    assert calls == []


def test_offline_mode_error_is_exported():
    """The refusal class is part of the public API, next to OfflineDataError."""
    assert 'OfflineModeError' in fwl_io.__all__
    assert issubclass(fwl_io.OfflineModeError, RuntimeError)
    assert not issubclass(fwl_io.OfflineModeError, fwl_io.OfflineDataError)


@pytest.mark.parametrize(
    'name',
    [
        'fetch_zenodo_record',
        'latest_record_id',
        'dataverse_server',
        'zenodo_record_rights',
        'dataverse_request',
        'dataverse_download',
        'mirror_status',
        'newer_versions',
        'check_mirrors',
    ],
)
def test_each_network_call_refuses_in_offline_mode(blocked_net, name):
    """Each function that opens a connection refuses before it, naming the variable."""
    client = mirror.DataverseClient('https://dataverse.example', '')
    calls = {
        'fetch_zenodo_record': lambda: sync.fetch_zenodo_record(ZENODO_DOI),
        'latest_record_id': lambda: mirror_status.latest_record_id('15727899'),
        'dataverse_server': lambda: pins.dataverse_server('10.9999/not-a-known-prefix'),
        'zenodo_record_rights': lambda: mirror._zenodo_record_rights('15727899', sync.ZENODO_API),
        'dataverse_request': lambda: client._request('GET', '/api/info/version'),
        'dataverse_download': lambda: mirror_sync._download(client, 1),
        'mirror_status': lambda: mirror_status.mirror_status(datasets=[]),
        'newer_versions': lambda: mirror_status.newer_versions(datasets=[]),
        'check_mirrors': lambda: pins.check_mirrors(datasets=[]),
    }
    _assert_refused(calls[name], blocked_net)


@pytest.mark.parametrize(
    'argv',
    [
        ['sync', 'MANIFEST'],
        ['mirror', ZENODO_DOI, '--collection', 'c', '--dry-run'],
        ['mirror-publish', 'doi:10.34894/ZGZA6I'],
        ['mirror-sync', '--collection', 'c', '--dry-run'],
        ['mirror-pin', 'doi:10.34894/ZGZA6I', '--manifest', 'MANIFEST'],
        ['check-mirrors'],
        ['mirror-status'],
        ['latest'],
    ],
    ids=lambda argv: argv[0],
)
def test_maintainer_commands_exit_1_with_the_refusal(blocked_net, tmp_path, capsys, argv):
    """A maintainer command exits 1 with the refusal, never 3 or 4, and opens no socket."""
    manifest = _manifest(tmp_path)
    before = manifest.read_text()
    code = main([str(manifest) if a == 'MANIFEST' else a for a in argv])
    out = capsys.readouterr()
    assert code == 1
    assert REFUSAL in out.err
    assert blocked_net == []
    assert manifest.read_text() == before


@pytest.mark.parametrize(
    ('argv', 'code'),
    [
        (['list'], 0),
        (['path', 'interior.mass_radius.zeng_2019'], 1),
        (['check', 'zalmoxis'], 1),
        (['cache', 'check', '--key', 'interior.mass_radius.zeng_2019'], 1),
        (['relocate', '--dry-run'], 0),
        (['prune'], 0),
    ],
    ids=lambda v: v[0] if isinstance(v, list) else str(v),
)
def test_local_commands_still_run_in_offline_mode(blocked_net, capsys, argv, code):
    """Commands that only read the local tree keep their exit code and open no socket."""
    assert main(argv) == code
    assert REFUSAL not in capsys.readouterr().err
    assert blocked_net == []


def test_fetch_from_a_populated_tree_runs_in_offline_mode(blocked_net, tmp_path):
    """A file already in the tree is served in offline mode without a socket call."""
    content = b'offline payload\n'
    registry = {'a.txt': 'sha256:' + hashlib.sha256(content).hexdigest()}
    fetcher = create_fetcher(
        subdir='g/demo', zenodo=ZENODO_DOI, registry=registry, data_root=tmp_path / 'tree'
    )
    fetcher.target_dir.mkdir(parents=True)
    (fetcher.target_dir / 'a.txt').write_bytes(content)
    assert fetcher.fetch('a.txt').read_bytes() == content
    assert blocked_net == []


@pytest.mark.parametrize(
    ('value', 'refused'),
    [
        ('1', True),
        ('true', True),
        (' YES ', True),
        ('On', True),
        ('0', False),
        ('', False),
        ('false', False),
        ('no', False),
        ('2', False),
    ],
)
def test_require_online_follows_the_offline_spellings(monkeypatch, value, refused):
    """The guard refuses for exactly the truthy spellings of FWL_IO_OFFLINE."""
    guard = getattr(paths, 'require_online', None)
    assert guard is not None, 'fwl_io.paths.require_online is missing'
    monkeypatch.setenv('FWL_IO_OFFLINE', value)
    if refused:
        with pytest.raises(fwl_io.OfflineModeError, match='a Zenodo read needs the network'):
            guard('a Zenodo read')
    else:
        assert guard('a Zenodo read') is None
