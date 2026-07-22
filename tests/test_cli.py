import json

import pytest

from fwl_io.cli import main

pytestmark = pytest.mark.integration


def _serve_record(root, recid, payload):
    api_dir = root / 'api' / 'records'
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / str(recid)).write_text(json.dumps(payload))


def test_sync_command_writes_registries(http_server, tmp_path, capsys):
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
    code = main(['sync', str(tmp_path / 'missing_manifest.toml')])
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith('fwl-io: ')
    assert 'Traceback' not in err


@pytest.mark.unit
def test_list_reports_broken_provider_and_missing_registry(tmp_path, capsys, monkeypatch):
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
    assert 'badmodel' in captured.err and 'FAILED TO LOAD' in captured.err


@pytest.mark.unit
def test_fetch_unknown_module_exits_nonzero(capsys, monkeypatch):
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [])
    code = main(['fetch', 'nomodule'])
    assert code == 1
    assert 'no datasets' in capsys.readouterr().err


@pytest.mark.unit
def test_fetch_missing_registry_is_aggregated_error(tmp_path, capsys, monkeypatch):
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
