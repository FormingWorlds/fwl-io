"""Offline mode refuses every network call: data fetches and the maintainer commands."""

import hashlib
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import fwl_io
from fwl_io import cli, mirror, mirror_status, mirror_sync, paths, pins, sync
from fwl_io.cli import main
from fwl_io.fetch import create_fetcher
from fwl_io.manifest import fetcher_for_key

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

REFUSAL = 'needs the network and offline mode is active; unset FWL_IO_OFFLINE'
ZENODO_DOI = '10.5281/zenodo.15727899'
NETWORK_COMMANDS = (
    'sync',
    'mirror',
    'mirror-publish',
    'mirror-sync',
    'mirror-pin',
    'check-mirrors',
    'mirror-status',
    'latest',
)
TEXTS = (
    'AGENTS.md',
    'README.md',
    'docs/Explanations/design.md',
    'docs/How-to/clusters.md',
    'docs/Reference/cli.md',
    'docs/Reference/environment.md',
    'docs/getting_started.md',
    'src/fwl_io/paths.py',
)
ROOT = Path(__file__).resolve().parents[1]


def _broken():
    raise RuntimeError('provider failed to import')


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
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [])
    return calls


def _manifest(tmp_path):
    path = tmp_path / 'manifest.toml'
    path.write_text(f'[g.demo]\nzenodo = "{ZENODO_DOI}"\n')
    return path


def _assert_refused(call, calls, subject):
    """Call ``call`` and require the offline refusal naming ``subject`` before any socket call."""
    with pytest.raises(Exception) as info:  # another type fails the assert below
        call()
    refusal = f'{subject} {REFUSAL}'
    assert info.type is fwl_io.OfflineModeError and refusal in str(info.value), repr(info.value)
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
        'fetch_zenodo_record': (
            lambda: sync.fetch_zenodo_record(ZENODO_DOI),
            'reading Zenodo record 15727899',
        ),
        'latest_record_id': (
            lambda: mirror_status.latest_record_id('15727899'),
            'reading the newest version of Zenodo record 15727899',
        ),
        'dataverse_server': (
            lambda: pins.dataverse_server('10.9999/not-a-known-prefix'),
            'the doi.org lookup of doi:10.9999/not-a-known-prefix',
        ),
        'zenodo_record_rights': (
            lambda: mirror._zenodo_record_rights('15727899', sync.ZENODO_API),
            'reading the license of Zenodo record 15727899',
        ),
        'dataverse_request': (
            lambda: client._request('GET', '/api/info/version'),
            'Dataverse GET /api/info/version',
        ),
        'dataverse_download': (
            lambda: mirror_sync._download(client, 1),
            'downloading Dataverse file 1',
        ),
        'mirror_status': (lambda: mirror_status.mirror_status(datasets=[]), 'mirror-status'),
        'newer_versions': (lambda: mirror_status.newer_versions(datasets=[]), 'latest'),
        'check_mirrors': (lambda: pins.check_mirrors(datasets=[]), 'check-mirrors'),
    }
    call, subject = calls[name]
    _assert_refused(call, blocked_net, subject)


@pytest.mark.parametrize(
    'argv',
    [
        ['sync', 'MANIFEST'],
        ['mirror', ZENODO_DOI, '--collection', 'c', '--no-publish'],
        ['mirror', ZENODO_DOI, '--collection', 'c', '--dry-run'],
        ['mirror-publish', 'doi:10.34894/ZGZA6I'],
        ['mirror-sync', '--collection', 'c', '--dry-run'],
        ['mirror-pin', 'doi:10.34894/ZGZA6I', '--manifest', 'MANIFEST'],
        ['check-mirrors'],
        ['mirror-status'],
        ['latest'],
    ],
    ids=lambda argv: '-'.join(a for a in argv if not a.startswith(('10.', 'doi:'))),
)
@pytest.mark.parametrize('setup', ['plain', 'no-token', 'broken-provider'])
def test_maintainer_commands_exit_1_with_the_refusal(
    blocked_net, tmp_path, capsys, monkeypatch, argv, setup
):
    """A maintainer command exits 1 with a refusal naming it, before any other check."""
    if setup == 'no-token':
        monkeypatch.delenv('DATAVERSE_TOKEN')
    elif setup == 'broken-provider':
        eps = [SimpleNamespace(name='broken', load=_broken)]
        monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)
    manifest = _manifest(tmp_path)
    before = manifest.read_text()
    code = main([str(manifest) if a == 'MANIFEST' else a for a in argv])
    out = capsys.readouterr()
    assert code == 1
    assert f'the {argv[0]} command {REFUSAL}' in out.err
    assert blocked_net == []
    assert manifest.read_text() == before


def test_sync_of_an_empty_manifest_is_refused(blocked_net, tmp_path, capsys):
    """A sync with no dataset to read is refused too, in the CLI and in Python."""
    empty = tmp_path / 'empty.toml'
    empty.write_text('')
    assert main(['sync', str(empty)]) == 1
    assert f'the sync command {REFUSAL}' in capsys.readouterr().err
    _assert_refused(lambda: sync.sync_manifest(empty), blocked_net, f'syncing {empty}')


def test_library_entry_points_refuse_with_the_error_type(blocked_net, tmp_path, monkeypatch):
    """sync_manifest and mirror_sync raise OfflineModeError, not an aggregate or a FAIL line."""
    manifest = _manifest(tmp_path)
    _assert_refused(lambda: sync.sync_manifest(manifest), blocked_net, f'syncing {manifest}')
    eps = [SimpleNamespace(name='broken', load=_broken)]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)
    options = {'dataverse_url': 'https://dataverse.example', 'token': 't'}
    options.update(contact_name='n', contact_email='e@example.org', dry_run=True)
    _assert_refused(
        lambda: mirror_sync.mirror_sync('c', **options), blocked_net, 'the mirror sync of c'
    )


def _unused(*args):
    raise AssertionError('the client was used in offline mode')


@pytest.mark.parametrize('name', ['mirror_pin', 'mirror_to_dataverse', 'publish_draft'])
def test_mirror_entry_points_refuse_before_their_checks(blocked_net, name):
    """A pin with its own client, a mirror without an email and a bad publish all refuse."""
    client = SimpleNamespace(_retry=_unused, _request=_unused)
    calls = {
        'mirror_pin': (
            lambda: mirror_sync.mirror_pin('doi:10.34894/ZGZA6I', client=client),
            'pinning doi:10.34894/ZGZA6I',
        ),
        'mirror_to_dataverse': (
            lambda: mirror.mirror_to_dataverse(
                ZENODO_DOI,
                dataverse_url='https://dataverse.example',
                collection='c',
                token='t',
                contact_name='n',
                contact_email='',
            ),
            f'mirroring {ZENODO_DOI}',
        ),
        'publish_draft': (
            lambda: mirror.publish_existing_dataverse_draft(
                'doi:10.34894/ZGZA6I',
                dataverse_url='https://dataverse.example',
                token='t',
                version_type='patch',
            ),
            'publishing doi:10.34894/ZGZA6I',
        ),
    }
    call, subject = calls[name]
    _assert_refused(call, blocked_net, subject)


def test_cli_and_texts_list_the_same_network_commands():
    """The CLI guard and every text that documents offline mode name the same 8 commands."""
    assert frozenset(NETWORK_COMMANDS) == cli.NETWORK_COMMANDS
    clause = re.compile(
        r'needs the network \(([^)]*)\)\s+(?:raises (?::class:)?`+OfflineModeError`+'
        r'|exits 1 with the offline message)\s+before\s+it\s+connects'
    )
    for name in TEXTS:
        lists = clause.findall((ROOT / name).read_text())
        assert len(lists) == (2 if name == 'README.md' else 1), name
        for found in lists:
            assert re.findall(r'[a-z-]+', found) == list(NETWORK_COMMANDS), name


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (['list'], 'g.demo'),
        (['path', 'g.demo'], 'r15727899'),
        (['check', 'demo'], 'g.demo: ok'),
        (['cache', 'check', '--key', 'g.demo'], 'g/demo/r15727899: 1/1 files ok'),
        (['relocate', '--dry-run'], 'no dataset declares a legacy location'),
        (['prune'], '1 referenced kept'),
        (['fetch', 'demo'], 'g.demo: 1 file(s)'),
        (['cache', 'fetch', '--key', 'g.demo'], 'g/demo/r15727899: fetched'),
    ],
    ids=lambda v: '-'.join(v) if isinstance(v, list) else None,
)
def test_local_commands_still_run_in_offline_mode(
    blocked_net, tmp_path, capsys, monkeypatch, argv, expected
):
    """Commands that only read a populated tree exit 0 in offline mode and open no socket."""
    body = b'offline payload\n'
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[g.demo]\nzenodo = "{ZENODO_DOI}"\nrequired_by = ["demo"]\n')
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / 'g.demo.registry.txt').write_text(f'a.txt sha256:{digest}\n')
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [SimpleNamespace(name='demo', load=lambda: lambda: manifest)],
    )
    target = tmp_path / 'data' / 'g' / 'demo' / 'r15727899'
    target.mkdir(parents=True)
    (target / 'a.txt').write_bytes(body)
    fetcher_for_key('g.demo', tmp_path / 'data').fetch_all()
    code = main(argv)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert expected in out.out
    assert REFUSAL not in out.err
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
    monkeypatch.setenv('FWL_IO_OFFLINE', value)
    if refused:
        with pytest.raises(fwl_io.OfflineModeError, match='a Zenodo read needs the network'):
            paths.require_online('a Zenodo read')
    else:
        assert paths.require_online('a Zenodo read') is None
