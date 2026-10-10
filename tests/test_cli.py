import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

from fwl_io.cli import main

pytestmark = pytest.mark.integration


class _FakeStderr(io.StringIO):
    """A stderr stand-in with a settable ``isatty`` for progress auto-detect."""

    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def _capture_fetch_progress(monkeypatch):
    """Replace ``fetch_for`` with a stub that records the ``progress`` it got."""
    seen = {}

    def fake_fetch_for(model, data_root=None, progress=False):
        seen['progress'] = progress
        return {'g.demo': [Path('a')]}

    monkeypatch.setattr('fwl_io.manifest.fetch_for', fake_fetch_for)
    return seen


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
def test_list_shows_declared_name_but_not_the_key_fallback(tmp_path, capsys, monkeypatch):
    """A declared human-readable name is printed; an undeclared one is not repeated."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        '[labelled]\n'
        'name = "Wolf & Bower (2018) MgSiO3 equation of state"\n'
        'zenodo = "10.5281/zenodo.1"\n'
        '[unlabelled]\n'
        'zenodo = "10.5281/zenodo.2"\n'
    )

    class _EP:
        def __init__(self, name, target):
            self.name = name
            self._target = target

        def load(self):
            return self._target

    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_EP('demo', lambda: manifest)],
    )

    code = main(['list'])
    out = capsys.readouterr().out
    lines = out.splitlines()
    name = 'Wolf & Bower (2018) MgSiO3 equation of state'
    assert code == 0
    # The declared name prints on the line immediately below its key, not merely
    # somewhere in the output: a print-order swap has to fail this.
    key_line = next(i for i, line in enumerate(lines) if line.strip().startswith('labelled'))
    assert lines[key_line + 1].strip() == name
    # The undeclared dataset falls back to its key, so its key line is not
    # followed by a repeat of the key.
    fallback_line = next(i for i, line in enumerate(lines) if line.strip().startswith('unlabelled'))
    following = lines[fallback_line + 1] if fallback_line + 1 < len(lines) else ''
    assert following.strip() != 'unlabelled'
    assert out.count('unlabelled') == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    'escape',
    ['\\n', '\\t', '\\r', '\\u2028'],
    ids=['newline', 'tab', 'cr', 'line-separator'],
)
def test_list_strips_control_characters_from_a_declared_name(escape, tmp_path, capsys, monkeypatch):
    """A name carrying a control character cannot inject an extra line into the listing."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        '[evil]\n'
        f'name = "harmless{escape}  forged.key    required_by: victim    [NO REGISTRY]"\n'
        'zenodo = "10.5281/zenodo.1"\n'
    )

    class _EP:
        def __init__(self, name, target):
            self.name = name
            self._target = target

        def load(self):
            return self._target

    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_EP('demo', lambda: manifest)],
    )

    code = main(['list'])
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert code == 0
    # The embedded newline must not produce a fourth line that reads like a
    # second dataset: provider header, key line, one name line, nothing else.
    assert len(lines) == 3
    assert lines[0] == '[demo]'
    assert lines[1].startswith('  evil')
    assert lines[2].startswith('    harmless')
    assert not any(line.startswith('  forged') for line in lines)


@pytest.mark.unit
@pytest.mark.parametrize(
    'toml_escaped_name',
    ['\\u200b\\u200b', '\\u0001   \\u0001'],
    ids=['all-non-printable', 'non-printable-padding-around-spaces'],
)
def test_list_omits_the_label_line_for_an_all_non_printable_name(
    toml_escaped_name, tmp_path, capsys, monkeypatch
):
    """A name that strips to nothing after filtering must not print a bare indented line."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[evil]\nname = "{toml_escaped_name}"\nzenodo = "10.5281/zenodo.1"\n')

    class _EP:
        def __init__(self, name, target):
            self.name = name
            self._target = target

        def load(self):
            return self._target

    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_EP('demo', lambda: manifest)],
    )

    code = main(['list'])
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert code == 0
    assert len(lines) == 2
    assert lines[0] == '[demo]'
    assert lines[1].startswith('  evil')


@pytest.mark.unit
@pytest.mark.parametrize(
    'toml_escaped_name',
    ['evil ', 'evil\\u200b'],
    ids=['trailing-space', 'trailing-invisible-char'],
)
def test_list_omits_the_label_line_when_it_collapses_to_the_key(
    toml_escaped_name, tmp_path, capsys, monkeypatch
):
    """A name that differs from the key raw but not after filtering must not repeat it."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[evil]\nname = "{toml_escaped_name}"\nzenodo = "10.5281/zenodo.1"\n')

    class _EP:
        def __init__(self, name, target):
            self.name = name
            self._target = target

        def load(self):
            return self._target

    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_EP('demo', lambda: manifest)],
    )

    code = main(['list'])
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert code == 0
    assert len(lines) == 2
    assert lines[0] == '[demo]'
    assert lines[1].startswith('  evil')


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
    assert 'MANIFEST NOT USED' not in out
    # Resolving a path creates the data root, as it does for every entry point.
    # What a check must not do is populate it: no dataset directory, no file.
    assert list(data_root.iterdir()) == [], 'a check must not create the tree it inspects'


@pytest.mark.unit
def test_check_exits_zero_and_says_which_verdict_it_reached(tmp_path, capsys, monkeypatch):
    """A sound tree exits 0, and the wording separates hashed from presence-only.

    Both trees here are sound, so the exit code cannot tell them apart, which is
    the intended contract: presence is all an archive dataset makes checkable and
    it is not a fault. What must differ is the claim. Without the second half a
    change tying the exit code to verification instead of soundness would go
    unnoticed, and every archive dataset would start failing.
    """
    import hashlib

    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        '[g.plain]\nzenodo = "10.5281/zenodo.1234567"\nrequired_by = ["demo"]\n\n'
        '[g.arc]\nzenodo = "10.5281/zenodo.7654321"\nrequired_by = ["demo"]\n'
        'extract = "tar"\n'
    )
    body = b'contents\n'
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / 'g.plain.registry.txt').write_text(f'alpha.dat sha256:{digest}\n')
    (tmp_path / 'g.arc.registry.txt').write_text('bundle.tar sha256:' + 'a' * 64 + '\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    data_root = tmp_path / 'data'
    plain_dir = data_root / 'g' / 'plain' / 'r1234567'
    plain_dir.mkdir(parents=True)
    (plain_dir / 'alpha.dat').write_bytes(body)
    arc_dir = data_root / 'g' / 'arc' / 'r7654321'
    arc_dir.mkdir(parents=True)
    (arc_dir / 'inner.dat').write_bytes(b'x')
    (arc_dir / '.fwl-io.json').write_text(
        json.dumps(
            {
                'schema': 1,
                'extract': 'tar',
                'record_id': '7654321',
                'zenodo': '10.5281/zenodo.7654321',
                'members': ['inner.dat'],
            }
        )
    )

    code = main(['check', 'demo', '--data-root', str(data_root)])
    out = capsys.readouterr().out

    assert code == 0, 'a sound tree exits 0 even where only presence was checkable'
    assert 'FAILED' not in out
    assert 'g.plain: ok' in out and 'g.arc: ok' in out
    assert 'presence only' in out, 'the archive dataset has to say what it could not check'
    assert 'all data present, 1 dataset(s) by presence only' in out
    assert 'and verified' not in out, 'one presence-only dataset forfeits the stronger claim'


@pytest.mark.unit
def test_relocate_exits_nonzero_when_a_manifest_could_not_be_read(tmp_path, capsys, monkeypatch):
    """A run that could not read a manifest is not a clean run.

    Nothing moved and nothing was found, which on its own is what a finished
    tree looks like. The manifest that failed may be the one declaring the
    dataset whose old directory is still sitting there, so automation reading
    only the exit code must not be told this pass was complete.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('this is not valid toml [[[\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    data_root = tmp_path / 'data'

    code = main(['relocate', '--data-root', str(data_root)])
    out = capsys.readouterr().out

    assert code == 1, 'an unread manifest cannot exit as success'
    assert 'MANIFEST FAILED TO LOAD' in out
    assert 'may be partial' in out


@pytest.mark.unit
def test_relocate_exits_zero_on_a_tree_with_nothing_to_move(tmp_path, capsys, monkeypatch):
    """A machine that never had the old layout is a success, not a fault.

    The discriminating half of the case above: both runs move nothing and
    report no faults, so only the manifest error separates them, and the exit
    code has to follow that rather than the move count.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[star.tracks.baraffe_2015]\nzenodo = "10.5281/zenodo.15729114"\n')
    (tmp_path / 'star.tracks.baraffe_2015.registry.txt').write_text(
        'a.dat sha256:' + 'a' * 64 + '\n'
    )

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])

    code = main(['relocate', '--data-root', str(tmp_path / 'data')])
    out = capsys.readouterr().out

    assert code == 0
    assert 'MANIFEST NOT USED' not in out
    assert 'absent' in out


def test_mirror_command_forwards_repeated_file_options(monkeypatch):
    """Each ``--file`` reaches the mirror as one entry of the ``files`` list."""
    seen = {}

    def fake_mirror(doi, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr('fwl_io.mirror.mirror_to_dataverse', fake_mirror)
    monkeypatch.setenv('DATAVERSE_TOKEN', 't')
    argv = ['mirror', '10.5281/zenodo.5', '--collection', 'c', '--dry-run']
    argv += ['--contact-name', 'n', '--contact-email', 'e@x.org']
    assert main(argv + ['--file', 'a.dat', '--file', 'b.dat']) == 0
    assert seen['files'] == ['a.dat', 'b.dat']
    assert main(argv) == 0
    assert seen['files'] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    'flags, tty, expected',
    [
        (['--no-progress'], True, False),
        (['--progress'], False, True),
        ([], True, True),
        ([], False, False),
    ],
)
def test_fetch_progress_resolution(flags, tty, expected, monkeypatch):
    """An explicit flag wins; absent it, the bar follows whether stderr is a TTY."""
    monkeypatch.setattr('pooch.downloaders.tqdm', object())
    monkeypatch.setattr('sys.stderr', _FakeStderr(tty))
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', *flags]) == 0
    assert seen['progress'] is expected


@pytest.mark.unit
def test_fetch_progress_soft_degrades_without_tqdm(monkeypatch):
    """With tqdm absent, the bar is dropped with a note and the fetch still runs."""
    monkeypatch.setattr('pooch.downloaders.tqdm', None)
    fake_err = _FakeStderr(tty=True)
    monkeypatch.setattr('sys.stderr', fake_err)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', '--progress']) == 0
    assert seen['progress'] is False
    assert 'pip install fwl-io[progress]' in fake_err.getvalue()


@pytest.mark.unit
def test_fetch_progress_auto_degrades_silently_without_tqdm(monkeypatch):
    """Auto mode on a TTY drops the bar when tqdm is absent, printing no note."""
    monkeypatch.setattr('pooch.downloaders.tqdm', None)
    fake_err = _FakeStderr(tty=True)
    monkeypatch.setattr('sys.stderr', fake_err)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo']) == 0
    assert seen['progress'] is False
    assert 'pip install fwl-io[progress]' not in fake_err.getvalue()


@pytest.mark.unit
def test_fetch_progress_hint_follows_pooch_binding_not_import(monkeypatch):
    """The hint and the bar follow pooch's tqdm binding, not whether tqdm imports.

    pooch binds tqdm once at its own import. A tqdm that is importable now but
    was absent then leaves the binding ``None``, so the bar cannot be drawn:
    an explicit ``--progress`` must print the hint and the fetch must still run.
    """
    monkeypatch.setitem(sys.modules, 'tqdm', types.ModuleType('tqdm'))
    monkeypatch.setattr('pooch.downloaders.tqdm', None)
    fake_err = _FakeStderr(tty=True)
    monkeypatch.setattr('sys.stderr', fake_err)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', '--progress']) == 0
    assert seen['progress'] is False
    assert 'pip install fwl-io[progress]' in fake_err.getvalue()


@pytest.mark.unit
def test_fetch_progress_hint_is_not_written_to_stdout_without_stderr(monkeypatch, capsys):
    """An explicit ``--progress`` with no tqdm and no stderr must not print the hint to stdout."""
    monkeypatch.setattr('pooch.downloaders.tqdm', None)
    monkeypatch.setattr('sys.stderr', None)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', '--progress']) == 0
    assert seen['progress'] is False
    assert 'pip install' not in capsys.readouterr().out


@pytest.mark.unit
def test_fetch_progress_dropped_without_stderr_even_with_tqdm(monkeypatch, capsys):
    """An explicit ``--progress`` with tqdm present but no stderr runs without a bar."""
    monkeypatch.setattr('pooch.downloaders.tqdm', object())
    monkeypatch.setattr('sys.stderr', None)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', '--progress']) == 0
    assert seen['progress'] is False
    assert 'pip install' not in capsys.readouterr().out


@pytest.mark.unit
def test_fetch_progress_hint_not_printed_when_pooch_binding_missing(monkeypatch):
    """Without pooch's private tqdm binding, ``--progress`` drops the bar without blaming tqdm."""
    monkeypatch.delattr('pooch.downloaders.tqdm', raising=False)
    fake_err = _FakeStderr(True)
    monkeypatch.setattr('sys.stderr', fake_err)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo', '--progress']) == 0
    assert seen['progress'] is False
    assert 'pip install' not in fake_err.getvalue()


@pytest.mark.unit
def test_fetch_progress_auto_survives_stderr_with_raising_isatty(monkeypatch):
    """Auto mode resolves to no bar when ``stderr.isatty`` raises any exception.

    The value only picks a cosmetic default, so a stream whose ``isatty`` raises
    something other than ``ValueError`` or ``OSError`` must not abort the fetch.
    """

    class _RaisingStderr(io.StringIO):
        def isatty(self):
            raise RuntimeError('broken stream')

    monkeypatch.setattr('pooch.downloaders.tqdm', object())
    monkeypatch.setattr('sys.stderr', _RaisingStderr())
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo']) == 0
    assert seen['progress'] is False


@pytest.mark.unit
def test_fetch_progress_auto_survives_stderr_without_isatty(monkeypatch):
    """Auto mode resolves to no bar, not a crash, when stderr has no ``isatty``.

    A redirected or replaced stream can be ``None`` or lack ``isatty``; auto mode
    must read that as "not a terminal" and let the fetch run, rather than aborting
    it at the CLI boundary.
    """
    monkeypatch.setattr('sys.stderr', None)
    seen = _capture_fetch_progress(monkeypatch)

    assert main(['fetch', 'demo']) == 0
    assert seen['progress'] is False


@pytest.mark.unit
def test_list_prints_conflicting_providers_as_failed_without_a_traceback(
    tmp_path, capsys, monkeypatch
):
    """Two providers claiming one location are listed as failed, exit 1, no traceback."""
    text = '[interior_lookup_tables.demo_eos]\nzenodo = "10.5281/zenodo.1234567"\n'

    class _EP:
        def __init__(self, name, sub):
            self.name = name
            self.sub = sub

        def load(self):
            def path():
                target = tmp_path / self.sub / 'manifest.toml'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
                return target

            return path

    eps = [_EP('package-a', 'a'), _EP('package-b', 'b')]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)

    code = main(['list'])
    captured = capsys.readouterr()
    assert code == 1
    assert '[package-a] NOT USED' in captured.err
    assert '[package-b] NOT USED' in captured.err
    assert 'interior_lookup_tables/demo_eos' in captured.err
    assert 'Traceback' not in captured.err
    assert captured.out == '', 'neither conflicting provider is listed as loaded'


def _conflicting_providers(tmp_path, model):
    """Two entry points whose manifests claim the same dataset location.

    Both read fine; the fault is that they collide, so a caller must not
    confuse this with a provider whose manifest could not be read at all.
    """
    text = (
        '[interior_lookup_tables.demo_eos]\n'
        'zenodo = "10.5281/zenodo.1234567"\n'
        f'required_by = ["{model}"]\n'
    )

    class _EP:
        def __init__(self, name, sub):
            self.name = name
            self.sub = sub

        def load(self):
            def path():
                target = tmp_path / self.sub / 'manifest.toml'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
                return target

            return path

    return [_EP('package-a', 'a'), _EP('package-b', 'b')]


@pytest.mark.unit
def test_check_reports_a_conflict_as_not_used_not_failed_to_load(tmp_path, capsys, monkeypatch):
    """A real cross-provider conflict, driven through main(), reads as NOT USED."""
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points', lambda group: _conflicting_providers(tmp_path, 'demo')
    )

    code = main(['check', 'demo', '--data-root', str(tmp_path / 'data')])
    out = capsys.readouterr().out

    assert code == 1
    assert 'package-a: MANIFEST NOT USED' in out
    assert 'package-b: MANIFEST NOT USED' in out
    assert 'FAILED TO LOAD' not in out


@pytest.mark.unit
def test_fetch_reports_both_conflicting_providers_as_not_used(tmp_path, capsys, monkeypatch):
    """A real cross-provider conflict is aggregated into fetch's failure report."""
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points', lambda group: _conflicting_providers(tmp_path, 'demo')
    )

    code = main(['fetch', 'demo', '--data-root', str(tmp_path / 'data')])
    err = capsys.readouterr().err

    assert code == 1
    assert 'manifest(s) not used' in err
    assert 'package-a' in err and 'package-b' in err
    assert 'Traceback' not in err


@pytest.mark.unit
def test_relocate_reports_a_conflict_as_not_used_not_failed_to_load(tmp_path, capsys, monkeypatch):
    """A real cross-provider conflict, driven through main(), reads as NOT USED."""
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points', lambda group: _conflicting_providers(tmp_path, 'demo')
    )

    code = main(['relocate', '--data-root', str(tmp_path / 'data')])
    out = capsys.readouterr().out

    assert code == 1
    assert 'package-a: MANIFEST NOT USED' in out
    assert 'package-b: MANIFEST NOT USED' in out
    assert 'FAILED TO LOAD' not in out


@pytest.mark.unit
@pytest.mark.parametrize('command', [['prune'], ['prune', '--delete', '--yes'], ['check-mirrors']])
def test_prune_and_check_mirrors_fail_on_a_conflict_and_name_it(
    tmp_path, capsys, monkeypatch, command
):
    """A real cross-provider conflict, driven through main(), exits 1 and reads as NOT USED
    for a prune dry run, a prune deletion and check-mirrors."""
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points', lambda group: _conflicting_providers(tmp_path, 'demo')
    )
    (tmp_path / 'data').mkdir()
    root = ['--data-root', str(tmp_path / 'data')] if command[0] == 'prune' else []

    code = main([*command, *root])
    out = capsys.readouterr().out

    lead = 'FAIL ' if command == ['check-mirrors'] else ''
    assert code == 1
    assert f'{lead}package-a: MANIFEST NOT USED' in out
    assert f'{lead}package-b: MANIFEST NOT USED' in out
    assert 'FAILED TO LOAD' not in out


def _one_dataset(tmp_path, monkeypatch, extra=''):
    """Install one manifest declaring g.demo (one file, a.dat) with a DataverseNL pin."""
    import hashlib

    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        f'[g.demo]\nzenodo = "10.5281/zenodo.1"\ndataverse = "10.34894/ABCDEF"\n{extra}'
    )
    digest = hashlib.md5(b'data').hexdigest()
    (tmp_path / 'g.demo.registry.txt').write_text(f'a.dat md5:{digest}\n')

    class _EP:
        name = 'demo'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    return tmp_path / 'data' / 'g' / 'demo' / 'r1'


@pytest.mark.unit
def test_fetch_by_key_then_path_prints_the_version_dir(tmp_path, capsys, monkeypatch):
    """fwl-io path refuses a dataset no completed fetch left in place, and prints its version
    directory once fwl-io fetch --key has verified and stamped it."""
    target = _one_dataset(tmp_path, monkeypatch)
    root = ['--data-root', str(tmp_path / 'data')]
    (tmp_path / 'data').mkdir()
    assert main(['path', 'g.demo', *root]) == 1
    hint = f'run: fwl-io fetch --key g.demo --data-root {tmp_path / "data"}\n'
    assert capsys.readouterr().err.endswith(hint), 'the hint keeps the data root'
    monkeypatch.setenv('FWL_DATA', str(tmp_path / 'data'))
    assert main(['path', 'g.demo']) == 1
    assert capsys.readouterr().err.endswith('run: fwl-io fetch --key g.demo\n')
    target.mkdir(parents=True)
    (target / 'a.dat').write_bytes(b'data')
    assert main(['path', 'g.demo', *root]) == 1, 'a file without a completed fetch is not enough'
    capsys.readouterr()
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')  # the file is pre-seeded; no network
    assert main(['fetch', '--key', 'g.demo', *root]) == 0
    assert capsys.readouterr().out == 'g.demo: 1 file(s)\n'
    assert main(['path', 'g.demo', *root]) == 0
    assert capsys.readouterr().out == f'{target}\n'
    (target / 'a.dat').unlink()
    assert main(['path', 'g.demo', *root]) == 1, 'a file deleted after the fetch'


@pytest.mark.unit
def test_fetch_by_key_uses_the_dataset_mirrors(tmp_path, monkeypatch):
    """The key fetcher carries the Zenodo and DataverseNL mirrors of the dataset."""
    from fwl_io.manifest import fetcher_for_key

    _one_dataset(tmp_path, monkeypatch)
    fetcher = fetcher_for_key('g.demo', tmp_path / 'data')
    assert fetcher.mirrors == ['doi:10.5281/zenodo.1/', 'doi:10.34894/ABCDEF/']


@pytest.mark.unit
@pytest.mark.parametrize(
    ('argv', 'key'),
    [
        (['path', 'g.other'], 'g.other'),
        (['fetch', '--key', 'g.other'], 'g.other'),
        (['fetch', '--key', ''], ''),
    ],
)
def test_an_unknown_key_is_named(tmp_path, capsys, monkeypatch, argv, key):
    """A key no installed manifest declares, an empty one included, is a message and exit 1."""
    _one_dataset(tmp_path, monkeypatch)
    (tmp_path / 'data').mkdir()
    assert main([*argv, '--data-root', str(tmp_path / 'data')]) == 1
    assert f'no installed manifest declares the dataset {key!r}' in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize('via_env', [False, True])
def test_path_does_not_create_a_missing_data_root(tmp_path, capsys, monkeypatch, via_env):
    """fwl-io path only reads: a data root that does not exist, given by --data-root or by
    FWL_DATA, or that is a file, is named and left as it is."""
    _one_dataset(tmp_path, monkeypatch)
    typo, file_root = tmp_path / 'typo', tmp_path / 'file'
    file_root.write_text('x')
    for root in (typo, file_root):
        if via_env:
            monkeypatch.setenv('FWL_DATA', str(root))
        argv = ['path', 'g.demo'] if via_env else ['path', 'g.demo', '--data-root', str(root)]
        assert main(argv) == 1
        assert (
            f'the data root {root} does not exist or is not a directory' in capsys.readouterr().err
        )
    assert not typo.exists() and file_root.read_text() == 'x'


@pytest.mark.unit
@pytest.mark.parametrize('argv', [['fetch'], ['fetch', 'demo', '--key', 'g.demo']])
def test_fetch_takes_a_model_or_a_key(argv):
    """fetch needs exactly one of a model and --key."""
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2


@pytest.mark.unit
def test_an_archive_dataset_is_fetched_when_its_tree_is_intact(tmp_path, monkeypatch):
    """For an archive dataset, is_fetched is the stamp-recorded tree check."""
    from fwl_io.manifest import fetcher_for_key

    _one_dataset(tmp_path, monkeypatch, extra='extract = "zip"\n')
    fetcher = fetcher_for_key('g.demo', tmp_path / 'data')
    for intact in (True, False):
        monkeypatch.setattr(type(fetcher), '_archive_tree_intact', lambda self, v=intact: v)
        assert fetcher.is_fetched() is intact


@pytest.mark.unit
def test_a_missing_key_names_the_unused_manifests(tmp_path, monkeypatch):
    """With a provider left out of discovery, a key no usable manifest declares is not found
    and the error says how many manifests were not used; the usable keys still resolve."""
    from fwl_io import manifest
    from fwl_io.manifest import fetcher_for_key

    _one_dataset(tmp_path, monkeypatch)

    class _Broken:
        name = 'broken'

        def load(self):
            raise ImportError('provider package is broken')

    eps = [*manifest.entry_points(group='fwl_io.manifests'), _Broken()]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)
    with pytest.raises(LookupError, match=r"'g.other'; 1 manifest\(s\) not used, see fwl-io"):
        fetcher_for_key('g.other', tmp_path / 'data')
    assert fetcher_for_key('g.demo', tmp_path / 'data').target_dir.name == 'r1'


@pytest.mark.unit
def test_the_fetch_hint_quotes_a_data_root_with_a_space(tmp_path, capsys, monkeypatch):
    """A data root that needs shell quoting is quoted in the suggested fetch command."""
    import shlex

    _one_dataset(tmp_path, monkeypatch)
    root = tmp_path / 'my data'
    root.mkdir()
    assert main(['path', 'g.demo', '--data-root', str(root)]) == 1
    hint = f'run: fwl-io fetch --key g.demo --data-root {shlex.quote(str(root))}\n'
    assert "'" in hint and capsys.readouterr().err.endswith(hint)


@pytest.mark.unit
def test_fetch_by_key_fails_cleanly_when_a_file_cannot_be_fetched(tmp_path, capsys, monkeypatch):
    """Offline with an empty tree, fetch --key exits 1 with no file count and no stamp, and
    path still exits 1."""
    target = _one_dataset(tmp_path, monkeypatch)
    root = ['--data-root', str(tmp_path / 'data')]
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')
    assert main(['fetch', '--key', 'g.demo', *root]) == 1
    out, err = capsys.readouterr()
    assert 'file(s)' not in out and 'offline mode is active' in err
    assert not (target / '.fwl-io.json').exists()
    assert main(['path', 'g.demo', *root]) == 1


@pytest.mark.unit
@pytest.mark.skipif(os.geteuid() == 0, reason='root writes into a read-only directory')
def test_fetch_by_key_fails_when_the_stamp_cannot_be_written(tmp_path, capsys, monkeypatch):
    """When the version directory cannot take the stamp, fetch --key exits 1 and says so,
    instead of a success that fwl-io path would then refuse."""
    target = _one_dataset(tmp_path, monkeypatch)
    target.mkdir(parents=True)
    (target / 'a.dat').write_bytes(b'data')
    target.chmod(0o555)
    try:
        monkeypatch.setenv('FWL_IO_OFFLINE', '1')
        assert main(['fetch', '--key', 'g.demo', '--data-root', str(tmp_path / 'data')]) == 1
        out, err = capsys.readouterr()
        assert 'file(s)' not in out and 'its stamp could not be written' in err
    finally:
        target.chmod(0o755)


def test_mirror_into_passes_the_draft_and_never_publishes(monkeypatch, capsys):
    """--into reaches the mirror with publish off, and needs no contact email."""
    seen = {}

    def fake(doi, **kwargs):
        seen.update(kwargs)
        return 'doi:10.34894/DRAFT1'

    monkeypatch.setattr('fwl_io.mirror.mirror_to_dataverse', fake)
    monkeypatch.setenv('DATAVERSE_TOKEN', 't')
    argv = ['mirror', '10.5281/zenodo.55', '--collection', 'C', '--into', 'doi:10.34894/DRAFT1']
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert 'draft doi:10.34894/DRAFT1 completed, not published' in out
    assert 'add this to the manifest' not in out
    assert seen['into'] == 'doi:10.34894/DRAFT1' and seen['publish'] is False
    assert seen['contact_email'] == ''
    assert seen['licence'] is None


def test_mirror_licence_reaches_the_mirror_and_needs_no_publish(monkeypatch, capsys):
    """--licence is passed through with --no-publish; without it the run is refused before
    any request, and the message names the option."""
    seen = {}
    monkeypatch.setenv('DATAVERSE_TOKEN', 't')
    argv = ['mirror', '10.5281/zenodo.21390786', '--collection', 'C', '--licence', 'CC-BY-4.0']
    argv += ['--contact-email', 'c@x']
    monkeypatch.setattr('requests.Session.request', lambda *a, **k: pytest.fail('a request'))
    monkeypatch.setattr('requests.get', lambda *a, **k: pytest.fail('a request'))
    assert main(argv) == 1
    assert 'pass --no-publish' in capsys.readouterr().err
    monkeypatch.setattr('fwl_io.mirror.mirror_to_dataverse', lambda doi, **kw: seen.update(kw))
    main([*argv, '--no-publish'])
    assert seen['licence'] == 'CC-BY-4.0' and seen['into'] is None and seen['publish'] is False


def test_mirror_no_publish_reports_a_created_draft(monkeypatch, capsys):
    """--no-publish without --into reports the draft as created and gives no manifest line."""
    monkeypatch.setattr('fwl_io.mirror.mirror_to_dataverse', lambda doi, **kw: 'doi:10.34894/D1')
    monkeypatch.setenv('DATAVERSE_TOKEN', 't')
    assert main(['mirror', '10.5281/zenodo.55', '--collection', 'C', '--no-publish']) == 0
    out = capsys.readouterr().out
    assert 'draft doi:10.34894/D1 created, not published' in out
    assert 'add this to the manifest' not in out
