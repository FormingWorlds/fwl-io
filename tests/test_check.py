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
    CheckReport,
    DatasetCheck,
    check_dataset,
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


def _write_stamp(fetcher, members):
    fetcher.target_dir.mkdir(parents=True, exist_ok=True)
    (fetcher.target_dir / STAMP).write_text(
        json.dumps({'schema': 1, 'extract': 'tar', 'record_id': RECID, 'members': members})
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
