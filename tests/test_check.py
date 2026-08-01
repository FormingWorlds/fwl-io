"""Tests for :mod:`fwl_io.check`, the validate-only mode.

The contract exercised here is that a check reports the state of a data tree
and changes nothing: no download, no repair, no write. It also has to keep
apart three outcomes a caller acts on differently, an absent file, a corrupt
one, and one that is present but has nothing to be verified against, and it
must never let an unread manifest read as a clean tree.

No test here touches the network. Every fetcher is built against a URL on a
closed port, so a check that tried to download would fail loudly rather than
quietly pass.
"""

from __future__ import annotations

import hashlib
import json
import socket

import pytest

from fwl_io.check import (
    MISMATCH,
    MISSING,
    OK,
    PRESENT,
    UNREADABLE,
    CheckReport,
    DatasetCheck,
    check_dataset,
    check_for,
)
from fwl_io.fetch import create_fetcher

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

SUBDIR = 'interior_lookup_tables/demo'
RECID = '15729114'
ZENODO = f'10.5281/zenodo.{RECID}'
STAMP = '.fwl-io.json'

# Two files with deliberately different contents and lengths, so a check that
# compared the wrong file against the wrong digest cannot pass by coincidence.
CONTENTS = {'alpha.dat': b'0.1 0.2 0.3\n', 'beta.dat': b'9.87\n'}


def _closed_url():
    """A URL on a port bound then released, so any request fails at once."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return f'http://127.0.0.1:{port}/'


def _registry(names=None):
    names = CONTENTS if names is None else {n: CONTENTS[n] for n in names}
    return {n: f'sha256:{hashlib.sha256(b).hexdigest()}' for n, b in names.items()}


def _plain_fetcher(data_root, registry=None):
    return create_fetcher(
        subdir=SUBDIR,
        registry=registry or _registry(),
        base_urls=[_closed_url()],
        data_root=data_root,
    )


def _archive_fetcher(data_root):
    return create_fetcher(
        subdir=SUBDIR,
        registry={'bundle.tar.gz': 'sha256:' + 'a' * 64},
        zenodo=ZENODO,
        base_urls=[_closed_url()],
        data_root=data_root,
        extract='tar',
    )


def _populate(fetcher, names=None, corrupt=()):
    """Write the dataset's files under the fetcher's target directory."""
    fetcher.target_dir.mkdir(parents=True, exist_ok=True)
    for name in CONTENTS if names is None else names:
        body = b'not the recorded contents\n' if name in corrupt else CONTENTS[name]
        (fetcher.target_dir / name).write_bytes(body)


def _write_stamp(fetcher, members, record_id=RECID):
    fetcher.target_dir.mkdir(parents=True, exist_ok=True)
    (fetcher.target_dir / STAMP).write_text(
        json.dumps({'schema': 1, 'extract': 'tar', 'record_id': record_id, 'members': members})
    )


def test_a_complete_tree_is_reported_complete_and_verified(tmp_path):
    """Every declared file present with matching contents reads as ok."""
    fetcher = _plain_fetcher(tmp_path)
    _populate(fetcher)

    result = check_dataset(fetcher, key='demo')

    assert result.complete
    assert result.hashed, 'a plain dataset has digests, so it must not report presence only'
    assert [f.name for f in result.files] == ['alpha.dat', 'beta.dat']
    assert {f.state for f in result.files} == {OK}
    assert result.missing == () and result.mismatched == ()


def test_an_empty_tree_reports_every_file_missing(tmp_path):
    """Nothing on disk names every declared file rather than reporting nothing.

    The edge case that matters: a dataset with no files present must not come
    back with an empty file list, which would satisfy ``complete`` and read as
    a healthy tree.
    """
    fetcher = _plain_fetcher(tmp_path)

    result = check_dataset(fetcher)

    assert not result.complete
    assert len(result.files) == 2, 'an absent tree still reports one entry per declared file'
    assert {f.name for f in result.missing} == {'alpha.dat', 'beta.dat'}
    assert result.mismatched == (), 'an absent file is missing, never a checksum failure'


def test_corrupt_and_absent_are_reported_apart(tmp_path):
    """A corrupt file and an absent one are distinct states, not one fault.

    They are distinguished because the remedies differ: an absent file may
    never have been fetched, while a corrupt one was fetched and then damaged
    or truncated, which points at the tree rather than at the download.
    """
    fetcher = _plain_fetcher(tmp_path)
    _populate(fetcher, names=['alpha.dat'], corrupt=['alpha.dat'])

    result = check_dataset(fetcher)
    states = {f.name: f.state for f in result.files}

    assert states == {'alpha.dat': MISMATCH, 'beta.dat': MISSING}
    assert [f.name for f in result.mismatched] == ['alpha.dat']
    assert [f.name for f in result.missing] == ['beta.dat']
    assert not result.complete


def test_a_check_changes_nothing_on_disk(tmp_path):
    """The tree is byte-identical afterwards, including a corrupt file.

    A diagnostic that repaired what it inspected would report a tree that no
    longer exists, and would do it while another process may be reading.
    """
    fetcher = _plain_fetcher(tmp_path)
    _populate(fetcher, corrupt=['beta.dat'])
    before = {p.name: p.read_bytes() for p in sorted(fetcher.target_dir.iterdir())}

    check_dataset(fetcher)

    after = {p.name: p.read_bytes() for p in sorted(fetcher.target_dir.iterdir())}
    assert after == before
    assert after['beta.dat'] != CONTENTS['beta.dat'], (
        'the corrupt file must still be corrupt, or this asserts nothing'
    )


def test_archive_members_are_present_not_verified(tmp_path):
    """An extracted tree is reported by presence, and says so.

    The registry pins the archive's checksum, not the checksums of what came
    out of it, so there is nothing to hash the members against. Reporting them
    as verified would overstate what was checked.
    """
    fetcher = _archive_fetcher(tmp_path)
    _write_stamp(fetcher, ['inner/one.dat', 'inner/two.dat'])
    for member in ('inner/one.dat', 'inner/two.dat'):
        path = fetcher.target_dir / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')

    result = check_dataset(fetcher)

    assert result.complete
    assert not result.hashed, 'an archive dataset cannot claim its members were verified'
    assert {f.state for f in result.files} == {PRESENT}
    assert 'presence only' in result.summary()


def test_a_deleted_archive_member_is_reported_missing(tmp_path):
    """A member removed after extraction is named, not silently tolerated."""
    fetcher = _archive_fetcher(tmp_path)
    _write_stamp(fetcher, ['inner/one.dat', 'inner/two.dat'])
    kept = fetcher.target_dir / 'inner/one.dat'
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b'x')

    result = check_dataset(fetcher)

    assert not result.complete
    assert [f.name for f in result.missing] == ['inner/two.dat']
    assert [f.name for f in result.files if f.state == PRESENT] == ['inner/one.dat']


@pytest.mark.parametrize(
    'stamp_body',
    ['', 'not json at all', json.dumps({'schema': 1}), json.dumps({'extract': 'zip'})],
    ids=['empty', 'unparseable', 'no-members', 'wrong-kind'],
)
def test_an_archive_without_a_usable_stamp_is_not_complete(tmp_path, stamp_body):
    """No usable stamp means no tree, reported as one missing item.

    The trap this guards is reporting zero files, which would make
    ``complete`` true and hand back a clean bill of health for a dataset that
    was never extracted.
    """
    fetcher = _archive_fetcher(tmp_path)
    fetcher.target_dir.mkdir(parents=True, exist_ok=True)
    (fetcher.target_dir / STAMP).write_text(stamp_body)

    result = check_dataset(fetcher)

    assert result.files, 'an unusable stamp must not produce an empty, complete report'
    assert not result.complete
    assert [f.name for f in result.missing] == ['bundle.tar.gz']


def test_an_unreadable_manifest_fails_the_report(tmp_path):
    """A provider that failed to load keeps the report from reading clean.

    Its datasets were never inspected, so a report that ignored it would be
    indistinguishable from one where everything was checked and passed. The
    dataset here is deliberately complete, so only the manifest error can be
    what fails it.
    """
    fetcher = _plain_fetcher(tmp_path)
    _populate(fetcher)
    healthy = check_dataset(fetcher, key='demo')
    assert healthy.complete, 'the dataset must be sound, or this proves nothing'

    clean = CheckReport(datasets={'demo': healthy}, manifest_errors={})
    broken = CheckReport(datasets={'demo': healthy}, manifest_errors={'other': 'no such file'})

    assert clean.ok
    assert not broken.ok
    assert 'MANIFEST UNREADABLE' in broken.summary()
    assert broken.faults == (), 'the datasets are sound; the fault is the unread manifest'


def test_the_summary_names_the_worst_dataset_first(tmp_path):
    """Datasets with more faults are listed ahead of those with fewer."""
    one_fault = DatasetCheck('one', SUBDIR, tmp_path, (_file('a', MISSING),))
    two_faults = DatasetCheck('two', SUBDIR, tmp_path, (_file('a', MISSING), _file('b', MISMATCH)))
    report = CheckReport(datasets={'one': one_fault, 'two': two_faults}, manifest_errors={})

    assert not report.ok
    assert [d.key for d in report.faults] == ['two', 'one']
    assert 'data check FAILED' in report.summary()
    assert '1 missing, 1 corrupt' in two_faults.summary()


def _file(name, state):
    from pathlib import Path

    from fwl_io.check import FileCheck

    return FileCheck(name, Path(name), state)


def test_an_unreadable_file_is_a_fault_not_a_pass(tmp_path):
    """A file that cannot be read is reported, not assumed to be fine.

    Its contents are unknown, which is not the same as correct, and the
    diagnostic must not resolve that doubt in the tree's favour.
    """
    fetcher = _plain_fetcher(tmp_path)
    _populate(fetcher)
    unreadable = fetcher.target_dir / 'alpha.dat'
    unreadable.chmod(0o000)
    try:
        result = check_dataset(fetcher)
    finally:
        unreadable.chmod(0o644)

    states = {f.name: f.state for f in result.files}
    assert states['alpha.dat'] == UNREADABLE
    assert states['beta.dat'] == OK, 'one unreadable file must not spoil the others'
    assert not result.complete
    assert [f.name for f in result.unreadable] == ['alpha.dat']


# ---------------------------------------------------------------------------
# check_for, the entry point a diagnostic calls
# ---------------------------------------------------------------------------


def _install_manifest(monkeypatch, manifest_path, name='demoprovider'):
    class _EP:
        def __init__(self):
            self.name = name

        def load(self):
            return lambda: manifest_path

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])


def _demo_manifest(tmp_path, *, with_registry=True, required_by='"demo"'):
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        f'[g.demo]\nzenodo = "10.5281/zenodo.1234567"\nrequired_by = [{required_by}]\n'
    )
    if with_registry:
        digests = _registry()
        lines = ''.join(f'{n} {d}\n' for n, d in sorted(digests.items()))
        (tmp_path / 'g.demo.registry.txt').write_text(lines)
    return manifest


def test_check_for_reports_a_complete_tree_through_the_real_path(tmp_path, monkeypatch):
    """A populated tree read through the manifest comes back ok.

    This goes through discovery, the registry file and the fetcher, unlike the
    tests above which hand a fetcher straight to check_dataset.
    """
    _install_manifest(monkeypatch, _demo_manifest(tmp_path))
    data_root = tmp_path / 'data'
    target = data_root / 'g' / 'demo' / 'r1234567'
    target.mkdir(parents=True)
    for name, body in CONTENTS.items():
        (target / name).write_bytes(body)

    report = check_for('demo', data_root=data_root)

    assert report.ok
    assert list(report.datasets) == ['g.demo']
    assert report.datasets['g.demo'].complete
    assert report.manifest_errors == {} and report.dataset_errors == {}


def test_check_for_matches_the_model_case_insensitively(tmp_path, monkeypatch):
    """``required_by`` matching ignores case, and a different model matches nothing."""
    _install_manifest(monkeypatch, _demo_manifest(tmp_path, required_by='"Demo"'))
    data_root = tmp_path / 'data'

    matched = check_for('dEmO', data_root=data_root)
    unmatched = check_for('othermodel', data_root=data_root)

    assert list(matched.datasets) == ['g.demo']
    assert unmatched.datasets == {}
    assert not unmatched.ok, 'a model that matches nothing must never read as a clean tree'


def test_check_for_carries_a_manifest_failure_into_the_report(tmp_path, monkeypatch):
    """An unreadable manifest fails the report through the real discovery path.

    The report is otherwise empty, so nothing else can be what fails it.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('this is not valid toml [[[\n')
    _install_manifest(monkeypatch, manifest)

    report = check_for('demo', data_root=tmp_path / 'data')

    assert not report.ok
    assert list(report.manifest_errors) == ['demoprovider']
    assert report.dataset_errors == {}
    assert 'MANIFEST UNREADABLE' in report.summary()


def test_check_for_separates_a_missing_registry_from_a_bad_manifest(tmp_path, monkeypatch):
    """A dataset with no registry is reported as unchecked, not as a bad manifest.

    They tell the user to fix different things: one is a broken file, the other
    is a registry that has simply never been generated.
    """
    _install_manifest(monkeypatch, _demo_manifest(tmp_path, with_registry=False))

    report = check_for('demo', data_root=tmp_path / 'data')
    summary = report.summary()

    assert not report.ok
    assert list(report.dataset_errors) == ['g.demo']
    assert report.manifest_errors == {}
    assert 'NOT CHECKED' in summary
    assert 'MANIFEST UNREADABLE' not in summary
    assert 'fwl-io sync' in summary, 'the message has to name the remedy'


def test_check_for_reports_missing_files_without_creating_them(tmp_path, monkeypatch):
    """Absent data is reported per file, and the check populates nothing."""
    _install_manifest(monkeypatch, _demo_manifest(tmp_path))
    data_root = tmp_path / 'data'

    report = check_for('demo', data_root=data_root)

    assert not report.ok
    dataset = report.datasets['g.demo']
    assert {f.name for f in dataset.missing} == set(CONTENTS)
    assert not (data_root / 'g').exists(), 'a check must not create the dataset directory'


def test_an_empty_report_is_not_ok():
    """Nothing checked is not the same as nothing wrong."""
    assert not CheckReport().ok
    assert CheckReport().summary() == 'nothing was checked'


def test_a_stamp_from_another_record_does_not_describe_this_tree(tmp_path):
    """A stamp naming a different deposit is not evidence about this one.

    The version directory is named for the record, so a stamp carrying another
    record id was left by a different fetch and its member list says nothing
    about what should be here.
    """
    fetcher = _archive_fetcher(tmp_path)
    _write_stamp(fetcher, ['inner/one.dat'], record_id='99999999')
    member = fetcher.target_dir / 'inner/one.dat'
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_bytes(b'x')

    assert fetcher.recorded_members() is None
    result = check_dataset(fetcher)

    assert not result.complete, 'a foreign stamp must not certify this tree'
    assert [f.name for f in result.missing] == ['bundle.tar.gz']


def test_a_stamp_member_escaping_the_dataset_is_refused(tmp_path):
    """A member name pointing outside the dataset directory is dropped.

    A stamp is an ordinary file on disk and can be edited or replaced, so a
    name inside it gets the same suspicion as a name inside an archive rather
    than being joined onto the tree and reported on.
    """
    fetcher = _archive_fetcher(tmp_path)
    outside = tmp_path / 'outside.dat'
    outside.write_bytes(b'not part of the dataset\n')
    _write_stamp(fetcher, ['../../../outside.dat', 'inner/one.dat'])
    member = fetcher.target_dir / 'inner/one.dat'
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_bytes(b'x')

    members = fetcher.recorded_members()

    assert members == ['inner/one.dat'], 'the escaping name must not survive'
    reported = {f.name for f in check_dataset(fetcher).files}
    assert reported == {'inner/one.dat'}
    assert outside.is_file(), 'the check reads only; it never touches what it refused'


def test_one_unresolvable_dataset_fails_a_report_of_sound_ones(tmp_path, monkeypatch):
    """A dataset that could not be checked fails the report beside healthy ones.

    The discriminating case for the rule: with a complete dataset present, an
    empty result cannot be what fails the report, so only the unchecked
    dataset can be. Without this, a report holding nothing but errors already
    fails for having checked nothing, and the rule would go untested.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        '[g.demo]\nzenodo = "10.5281/zenodo.1234567"\nrequired_by = ["demo"]\n\n'
        '[g.other]\nzenodo = "10.5281/zenodo.7654321"\nrequired_by = ["demo"]\n'
    )
    lines = ''.join(f'{n} {d}\n' for n, d in sorted(_registry().items()))
    (tmp_path / 'g.demo.registry.txt').write_text(lines)
    # g.other deliberately has no registry file, so it cannot be resolved.
    _install_manifest(monkeypatch, manifest)

    data_root = tmp_path / 'data'
    target = data_root / 'g' / 'demo' / 'r1234567'
    target.mkdir(parents=True)
    for name, body in CONTENTS.items():
        (target / name).write_bytes(body)

    report = check_for('demo', data_root=data_root)

    assert report.datasets['g.demo'].complete, 'the sound dataset must really be sound'
    assert list(report.dataset_errors) == ['g.other']
    assert not report.ok, 'an unchecked dataset fails the report even when the rest is sound'
    assert report.faults == (), 'no dataset is broken; the fault is the one never checked'
