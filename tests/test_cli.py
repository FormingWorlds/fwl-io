import json

import pytest

from fwl_io.cli import main

pytestmark = pytest.mark.integration


def _serve_record(root, recid, payload):
    api_dir = root / 'api' / 'records'
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / str(recid)).write_text(json.dumps(payload))


def test_sync_command_writes_registries(http_server, tmp_path, capsys):
    """The sync command writes the registry file the Zenodo record describes."""
    base_url, root = http_server
    _serve_record(
        root,
        1234567,
        {
            'id': 1234567,
            'conceptrecid': '1234566',
            'files': [{'key': 'alpha.dat', 'checksum': 'md5:aaa111'}],
        },
    )
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[g.demo]\nzenodo = "10.5281/zenodo.1234567"\n')
    code = main(['sync', str(manifest), '--api-base', f'{base_url}api/records'])
    assert code == 0
    assert 'wrote' in capsys.readouterr().out
    assert (tmp_path / 'g.demo.registry.txt').is_file()


def test_sync_command_failure_is_message_not_traceback(tmp_path, capsys):
    """A missing manifest is reported as a message, not as a stack trace."""
    code = main(['sync', str(tmp_path / 'missing_manifest.toml')])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith('fwl-io: ')
    assert 'Traceback' not in err


@pytest.mark.unit
def test_list_reports_broken_provider_and_missing_registry(tmp_path, capsys, monkeypatch):
    """Listing shows what is installed and names what failed to load."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[g.demo]\nzenodo = "10.5281/zenodo.1"\n')

    class _EP:
        def __init__(self, name, target):
            self.name = name
            self._target = target

        def load(self):
            return self._target

    def broken():
        raise ValueError('bad manifest')

    eps = [_EP('okmodel', lambda: manifest), _EP('badmodel', broken)]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)

    code = main(['list'])
    captured = capsys.readouterr()
    assert code == 1
    assert 'g.demo' in captured.out
    assert 'NO REGISTRY' in captured.out
    # The key is the location, so the listing carries it once: a second column
    # repeating it as a path would contradict the command reference.
    assert 'g/demo' not in captured.out
    assert 'badmodel' in captured.err and 'FAILED TO LOAD' in captured.err


@pytest.mark.unit
def test_fetch_unknown_module_exits_nonzero(capsys, monkeypatch):
    """Asking for a model no manifest declares is an error, not an empty success."""
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [])
    code = main(['fetch', 'nomodule'])
    assert code == 1
    assert 'no datasets' in capsys.readouterr().err


@pytest.mark.unit
def test_fetch_missing_registry_is_aggregated_error(tmp_path, capsys, monkeypatch):
    """A dataset that cannot be fetched is named, without a stack trace."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[g.demo]\nzenodo = "10.5281/zenodo.1"\nrequired_by = ["demo"]\n')

    class _EP:
        name = 'okmodel'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    code = main(['fetch', 'demo', '--data-root', str(tmp_path / 'data')])
    err = capsys.readouterr().err
    assert code == 1
    assert 'g.demo' in err and 'Traceback' not in err


@pytest.mark.unit
def test_check_unknown_module_exits_nonzero(capsys, monkeypatch):
    """Asking about a model no manifest declares is an error, not a clean tree."""
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [])
    code = main(['check', 'nomodule'])
    assert code == 1
    err = capsys.readouterr()
    assert 'no datasets' in err.err
    assert 'all data present' not in err.out, 'nothing was checked, so nothing may be declared ok'


@pytest.mark.unit
def test_check_reports_missing_data_and_exits_nonzero(tmp_path, capsys, monkeypatch):
    """Absent data exits 1 and names the dataset, without downloading it."""
    import hashlib

    manifest = tmp_path / 'manifest.toml'
    # The dataset location comes from the table key, so this one lands under
    # "g/demo"; a manifest does not name its own subdirectory.
    manifest.write_text('[g.demo]\nzenodo = "10.5281/zenodo.1234567"\nrequired_by = ["demo"]\n')
    registry = tmp_path / 'g.demo.registry.txt'
    digest = hashlib.sha256(b'contents\n').hexdigest()
    registry.write_text(f'alpha.dat sha256:{digest}\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    data_root = tmp_path / 'data'
    code = main(['check', 'demo', '--data-root', str(data_root)])
    out = capsys.readouterr().out

    assert code == 1
    assert 'FAILED' in out
    assert 'g.demo' in out
    # The dataset has to be reported as data that is absent, not as a manifest
    # this could not read. Both exit 1 and both name the dataset, so without
    # this the test would pass just as well against a misplaced registry file
    # and would be proving nothing about the check itself.
    assert '1 missing' in out
    assert 'MANIFEST UNREADABLE' not in out
    # Resolving a path creates the data root, as it does for every entry point.
    # What a check must not do is populate it: no dataset directory, no file.
    assert list(data_root.iterdir()) == [], 'a check must not create the tree it inspects'
