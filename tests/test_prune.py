"""Tests for :mod:`fwl_io.prune`, removing unreferenced dataset versions.

The contract exercised here is that a version directory is deleted only when it
is provably an older pin of a dataset a currently installed manifest declares,
that an incomplete reference set stops every deletion, and that a command whose
whole job is deleting data cannot follow a symlink out of the tree, leave the
root, or remove a version a current pin still uses.

No test here touches the network or a real FWL_DATA tree. Every dataset lives
under a pytest ``tmp_path``, and a fake manifest is installed so the real
manifest loader runs against it.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from filelock import FileLock

from fwl_io.fetch import _LOCK_DIRNAME, _STAGING_DIRNAME, _STAMP_FILENAME
from fwl_io.prune import (
    GONE,
    ORPHANED,
    REFERENCED,
    REFUSED,
    REMOVE_FAILED,
    SHARED_TREE_WARNING,
    SUPERSEDED,
    UNRECOGNISED,
    PruneCandidate,
    _fs_is_case_insensitive,
    _prune_empty_parents,
    _remove_one,
    _shadows_known_subdir,
    apply_prune,
    plan_prune,
    prune_versions,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

# A dataset that really ships, so the version-dir shape under test is the one
# the record-id derivation produces rather than one invented for the test.
KEY = 'star.tracks.baraffe_2015'
SUBDIR = 'star/tracks/baraffe_2015'
RECID = '15729114'
ZENODO = f'10.5281/zenodo.{RECID}'
OLD_RECID = '15000000'

# Files of a few different lengths, so a per-category byte total is a real sum
# and not a figure that any arrangement would produce.
_REFERENCED_BYTES = b'current pin contents\n'
_SUPERSEDED_BYTES = b'older pin\n'
_ORPHAN_BYTES = b'unknown provider payload here\n'


def _install_manifest(monkeypatch, manifest_path, *, extra_eps=()):
    """Install one manifest entry point, plus any extra (name, loader) points.

    ``extra_eps`` lets a test add a provider whose ``load`` raises or returns a
    broken manifest, to drive the incomplete-reference-set path.
    """

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest_path

    eps = [_EP()]
    for name, loader in extra_eps:
        eps.append(type('_EP', (), {'name': name, 'load': lambda self, ldr=loader: ldr}))
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)


def _write_manifest(tmp_path):
    """Write a manifest declaring the one pinned dataset and return its path."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{KEY}]\nzenodo = "{ZENODO}"\nrequired_by = ["mors"]\n')
    return manifest


def _stamp_text(record_id, subdir):
    """The body of a minimal valid fetcher stamp for ``subdir``/r``record_id``."""
    return json.dumps({'schema': 1, 'record_id': record_id, 'subdir': subdir})


def _write_stamp(directory, record_id, subdir):
    """Write a minimal valid fetcher stamp into ``directory``, as a fetch of ``subdir`` does."""
    (directory / _STAMP_FILENAME).write_text(_stamp_text(record_id, subdir))


def _make_tree(root):
    """Build a data root with one dir of each state, plus a reserved-dir version.

    ``superseded`` and ``orphaned`` carry the fetcher's own stamp, matching what
    a real fetch writes, so they classify past the stamp guard. ``reserved`` is
    filtered out before classification ever reaches it and stays unstamped.

    Returns
    -------
    dict
        ``referenced``, ``superseded``, ``orphaned`` and ``reserved`` mapped to
        the version directories created on disk.
    """
    referenced = root / SUBDIR / f'r{RECID}'
    superseded = root / SUBDIR / f'r{OLD_RECID}'
    orphaned = root / 'atmos' / 'unknown_set' / 'r99999999'
    reserved = root / _STAGING_DIRNAME / 'r12345678'
    for directory, body in (
        (referenced, _REFERENCED_BYTES),
        (superseded, _SUPERSEDED_BYTES),
        (orphaned, _ORPHAN_BYTES),
        (reserved, b'staging leftover\n'),
    ):
        directory.mkdir(parents=True)
        (directory / 'data.dat').write_bytes(body)
    _write_stamp(superseded, OLD_RECID, SUBDIR)
    _write_stamp(orphaned, '99999999', 'atmos/unknown_set')
    return {
        'referenced': referenced,
        'superseded': superseded,
        'orphaned': orphaned,
        'reserved': reserved,
    }


def _states(report):
    """Map each candidate's posix rel path to its classified state."""
    return {c.rel: c.state for c in report.candidates}


def test_a_dry_run_classifies_every_version_and_deletes_nothing(tmp_path, monkeypatch):
    """The default reports each directory's state and leaves the tree intact.

    A dry run is the one a user gets without opting into deletion, so it must
    both classify correctly and be provably harmless.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = plan_prune(data_root=root)
    states = _states(report)

    assert states[f'{SUBDIR}/r{RECID}'] == REFERENCED
    assert states[f'{SUBDIR}/r{OLD_RECID}'] == SUPERSEDED
    assert states['atmos/unknown_set/r99999999'] == ORPHANED
    assert f'{_STAGING_DIRNAME}/r12345678' not in states, 'a reserved dir must not be scanned'
    for directory in dirs.values():
        assert directory.is_dir(), 'a plan touches nothing on disk'


def test_the_summary_reports_reclaimable_bytes_per_category(tmp_path, monkeypatch):
    """Superseded and orphaned bytes are reported apart, not as one figure.

    A user deciding whether to prune needs to see what each opt-in reclaims, so
    the per-category totals are part of the report contract.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)

    report = plan_prune(data_root=root)
    superseded_stamp = len(_stamp_text(OLD_RECID, SUBDIR))
    orphaned_stamp = len(_stamp_text('99999999', 'atmos/unknown_set'))

    assert report.reclaimable(SUPERSEDED) == len(_SUPERSEDED_BYTES) + superseded_stamp
    assert report.reclaimable(ORPHANED) == len(_ORPHAN_BYTES) + orphaned_stamp
    summary = report.summary()
    assert 'superseded (' in summary and 'orphaned (' in summary


def test_delete_removes_the_superseded_pin_and_keeps_the_rest(tmp_path, monkeypatch):
    """A superseded version goes; the current pin and the orphan stay.

    Superseded is the only default target, so this is the line between what the
    command removes without a further opt-in and what it never removes by default.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = prune_versions(data_root=root, delete=True)

    assert not dirs['superseded'].exists(), 'the superseded pin is removed'
    assert dirs['referenced'].is_dir(), 'the current pin is kept'
    assert dirs['orphaned'].is_dir(), 'an orphan is not a default target'
    assert {c.rel for c in report.removed} == {f'{SUBDIR}/r{OLD_RECID}'}
    assert report.ok


def test_an_orphan_is_removed_only_with_the_explicit_opt_in(tmp_path, monkeypatch):
    """An orphan needs ``include_orphans``; the current pin is still kept.

    An orphan may be another environment's pin, so its deletion is a separate,
    louder choice than pruning a superseded version of a known dataset.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert not dirs['orphaned'].exists(), 'the orphan is removed under the opt-in'
    assert not dirs['superseded'].exists(), 'the superseded pin is removed too'
    assert dirs['referenced'].is_dir(), 'the current pin is never a target'
    assert {c.rel for c in report.removed} == {
        f'{SUBDIR}/r{OLD_RECID}',
        'atmos/unknown_set/r99999999',
    }


def test_a_manifest_that_did_not_load_blocks_every_deletion(tmp_path, monkeypatch):
    """One unreadable manifest makes nothing provably unreferenced.

    A provider whose manifest fails to load may be the one pinning the version
    that otherwise looks superseded, so the reference set is a subset of the
    truth and no deletion is allowed, even under the orphan opt-in.
    """
    broken = tmp_path / 'broken.toml'
    broken.write_text('this is not valid toml [[[\n')
    _install_manifest(
        monkeypatch,
        _write_manifest(tmp_path),
        extra_eps=[('brokenprovider', lambda: broken)],
    )
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert report.blocked
    assert report.superseded == (), 'nothing may be classed superseded off a partial set'
    assert not report.ok
    assert 'MANIFEST UNREADABLE' in report.summary()
    for directory in dirs.values():
        assert directory.exists(), 'a blocked run deletes nothing'


def test_an_unresolvable_version_dir_blocks_every_deletion(tmp_path, monkeypatch):
    """A pin whose version dir cannot be computed blocks the run.

    The manifest loader rejects a malformed pin before this code sees it, so the
    resolve-error path is driven by making the version-dir helper raise. The
    guarantee it protects is that one uncomputable reference stops all deletion.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    def _raise(_ds):
        raise ValueError('record id unavailable')

    monkeypatch.setattr('fwl_io.prune._version_dir', _raise)

    report = prune_versions(data_root=root, delete=True)

    assert report.blocked
    assert report.resolve_error is not None
    assert 'VERSION DIR UNRESOLVABLE' in report.summary()
    for directory in dirs.values():
        assert directory.exists(), 'an unresolvable reference stops every deletion'


def test_a_reserved_directory_is_never_scanned_or_removed(tmp_path, monkeypatch):
    """A version dir inside a reserved fwl-io directory is left untouched.

    The lock and staging directories are fwl-io's own; a name inside them that
    looks like a version dir is never a prune target.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    (root / _LOCK_DIRNAME / 'r55555555').mkdir(parents=True)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert dirs['reserved'].is_dir(), 'a version dir under staging survives'
    assert (root / _LOCK_DIRNAME / 'r55555555').is_dir(), 'a version dir under locks survives'
    rels = {c.rel for c in report.candidates}
    assert not any(_STAGING_DIRNAME in r or _LOCK_DIRNAME in r for r in rels)


def test_a_symlinked_version_dir_is_not_followed_or_removed(tmp_path, monkeypatch):
    """A symlink whose name matches the version shape is skipped, not deleted.

    Following it would let a deletion reach outside the tree; the scan drops it
    and its target is left in place.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    outside = tmp_path / 'outside_dataset'
    outside.mkdir()
    (outside / 'keep.dat').write_bytes(b'do not touch\n')
    link = root / SUBDIR / 'r22222222'
    link.symlink_to(outside, target_is_directory=True)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert link.is_symlink(), 'the symlink itself is left alone'
    assert (outside / 'keep.dat').is_file(), 'the target outside the tree is untouched'
    assert f'{SUBDIR}/r22222222' not in _states(report)


def test_remove_one_refuses_a_symlink(tmp_path):
    """The delete step re-checks for a symlink rather than trusting the plan."""
    root = tmp_path / 'data'
    root.mkdir()
    outside = tmp_path / 'target'
    outside.mkdir()
    link = root / SUBDIR / 'r33333333'
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    candidate = PruneCandidate(path=link, rel=f'{SUBDIR}/r33333333', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (REFUSED, 'is a symlink; not removed')
    assert link.is_symlink() and outside.is_dir()


def test_remove_one_refuses_a_symlink_to_a_stamped_version_inside_the_root(tmp_path):
    """The symlink guard alone stops a link whose target passes every other check.

    The target sits inside the root and carries a stamp naming the link's own
    record id and subdir, so containment, stamp and leaf checks all pass
    through the link. Only the symlink check keeps the link from being acted on.
    """
    root = tmp_path / 'data'
    target = _stamped_version(root, 'elsewhere', '33333333', stamp_subdir=SUBDIR)
    link = root / SUBDIR / 'r33333333'
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    candidate = PruneCandidate(path=link, rel=f'{SUBDIR}/r33333333', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (REFUSED, 'is a symlink; not removed')
    assert link.is_symlink() and (target / 'data.dat').is_file()


def test_remove_one_refuses_a_path_escaping_the_root(tmp_path):
    """A candidate resolving outside the data root is refused, not deleted.

    The plan's containment is re-established at act time, so a candidate that
    somehow named a path outside the root cannot be removed.
    """
    root = tmp_path / 'data'
    root.mkdir()
    outside = tmp_path / 'outside_dataset' / 'r44444444'
    outside.mkdir(parents=True)
    (outside / 'keep.dat').write_bytes(b'safe\n')
    candidate = PruneCandidate(path=outside, rel='../outside_dataset/r44444444', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert result.state == REFUSED
    assert 'outside' in result.detail
    assert (outside / 'keep.dat').is_file()


def test_remove_one_refuses_a_referenced_version(tmp_path):
    """A directory in the referenced set is never removed, even if handed in.

    Defence in depth: the current pin is checked again against the reference set
    at the moment of deletion, so a misclassified candidate cannot delete it.
    """
    root = tmp_path / 'data'
    version = root / SUBDIR / f'r{RECID}'
    version.mkdir(parents=True)
    (version / 'data.dat').write_bytes(_REFERENCED_BYTES)
    _write_stamp(version, RECID, SUBDIR)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced={version.resolve()})

    assert (result.state, result.detail) == (REFUSED, 'is a referenced version; not removed')
    assert version.is_dir()


def test_the_cli_confirm_shows_the_shared_tree_warning_and_an_abort_keeps_the_dir(
    tmp_path, monkeypatch, capsys
):
    """The interactive confirm names the danger and a declined prompt deletes nothing.

    On a shared FWL_DATA a superseded version may be another environment's pin,
    so the confirm must state that before any deletion and a non-"yes" reply must
    stop the run.
    """
    from fwl_io.cli import main

    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    monkeypatch.setattr(builtins, 'input', lambda _prompt='': 'no')

    exit_code = main(['prune', '--data-root', str(root), '--delete'])
    out = capsys.readouterr().out

    assert SHARED_TREE_WARNING in out, 'the confirm must warn about a shared tree'
    assert exit_code == 1, 'a declined confirm is a non-zero, non-deleting exit'
    assert dirs['superseded'].is_dir(), 'nothing is deleted without a "yes"'


def test_the_cli_yes_flag_deletes_the_superseded_pin_without_a_prompt(
    tmp_path, monkeypatch, capsys
):
    """``--yes`` removes the superseded pin non-interactively and keeps the pin.

    Scripts need a non-interactive path, so ``--yes`` skips the prompt while the
    same targets and guards apply.
    """
    from fwl_io.cli import main

    def _no_input(_prompt=''):
        raise AssertionError('--yes must not prompt')

    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    monkeypatch.setattr(builtins, 'input', _no_input)

    exit_code = main(['prune', '--data-root', str(root), '--delete', '--yes'])
    capsys.readouterr()

    assert exit_code == 0
    assert not dirs['superseded'].exists(), 'the superseded pin is removed'
    assert dirs['referenced'].is_dir(), 'the current pin is kept'
    assert dirs['orphaned'].is_dir(), 'an orphan is not removed without the opt-in'


# A dataset whose key's last segment matches the version shape (a spectral
# resolving power like r1000), so the subdir contains a directory that looks
# like a version dir but is a subdir segment, with the real version dir below.
_MASK_KEY = 'opacity.petitradtrans.r1000'
_MASK_SUBDIR = 'opacity/petitradtrans/r1000'
_MASK_RECID = '12345678'


def _write_masking_manifest(tmp_path):
    """Write a manifest whose subdir ends in a version-shaped segment."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        f'[{_MASK_KEY}]\nzenodo = "10.5281/zenodo.{_MASK_RECID}"\nrequired_by = ["mors"]\n'
    )
    return manifest


def test_a_subdir_named_like_a_version_dir_does_not_mask_the_referenced_version(
    tmp_path, monkeypatch
):
    """A dataset subdir ending in r<digits> is descended into, not matched as a version.

    A manifest key may end in a segment like ``r1000``. The referenced version
    dir sits below that subdir, so the subdir itself must be classified as part
    of the dataset and descended into, not matched as a version directory whose
    deletion would take the live pin nested inside it.
    """
    _install_manifest(monkeypatch, _write_masking_manifest(tmp_path))
    root = tmp_path / 'data'
    version = root / _MASK_SUBDIR / f'r{_MASK_RECID}'
    version.mkdir(parents=True)
    (version / 'opacities.h5').write_bytes(b'live referenced data\n')

    report = plan_prune(data_root=root)
    states = _states(report)

    assert states.get(f'{_MASK_SUBDIR}/r{_MASK_RECID}') == REFERENCED
    assert _MASK_SUBDIR not in states, 'the subdir segment is not itself a version dir'


def test_delete_with_orphans_never_removes_the_masked_referenced_version(tmp_path, monkeypatch):
    """A --include-orphans prune leaves a referenced version under an r-named subdir intact.

    This is the deletion side of the masking case: even under the loudest
    opt-in, the live pin nested below a version-shaped subdir survives.
    """
    _install_manifest(monkeypatch, _write_masking_manifest(tmp_path))
    root = tmp_path / 'data'
    version = root / _MASK_SUBDIR / f'r{_MASK_RECID}'
    version.mkdir(parents=True)
    live = version / 'opacities.h5'
    live.write_bytes(b'live referenced data\n')

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert live.is_file(), 'the referenced version under an r-named subdir must survive'
    assert report.removed == (), 'nothing is removed when the only version is referenced'


def test_remove_one_refuses_a_candidate_that_contains_a_referenced_version(tmp_path):
    """The delete step refuses a directory with a referenced version nested inside it.

    Defence in depth against a misclassified candidate: the not-referenced check
    covers nested referenced paths, so removing a parent can never take live data.
    """
    root = tmp_path / 'data'
    parent = root / 'opacity' / 'petitradtrans' / 'r1000'
    referenced = parent / f'r{_MASK_RECID}'
    referenced.mkdir(parents=True)
    (referenced / 'opacities.h5').write_bytes(b'live\n')
    candidate = PruneCandidate(path=parent, rel='opacity/petitradtrans/r1000', state=ORPHANED)

    result = _remove_one(candidate, root, referenced={referenced.resolve()})

    assert result.state == REFUSED
    assert 'contains a referenced version' in result.detail
    assert referenced.is_dir(), 'the nested referenced version is untouched'


def _skip_if_root():
    """Skip a test that relies on an unreadable directory when running as root."""
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('an unreadable directory does not stop root')


def test_an_unreadable_subtree_is_reported_as_a_scan_error_not_a_clean_run(tmp_path, monkeypatch):
    """A directory the scan cannot read makes the plan report a scan error, not ok.

    A permission error on a shared tree must not read as a clean tree with
    nothing to remove, so the scan records it and the report is not ok.
    """
    _skip_if_root()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    unreadable = root / 'atmos' / 'locked'
    (unreadable / 'r99999999').mkdir(parents=True)
    os.chmod(unreadable, 0)
    try:
        report = plan_prune(data_root=root)
        assert report.scan_error is not None, 'an unreadable subtree is a scan error'
        assert not report.ok, 'a partial scan is not a clean run'
        assert 'DATA ROOT UNREADABLE' in report.summary()
    finally:
        os.chmod(unreadable, stat.S_IRWXU)


def test_the_cli_refuses_to_delete_when_the_tree_cannot_be_fully_read(
    tmp_path, monkeypatch, capsys
):
    """A scan error stops the delete path, so a partial view never drives a prune.

    If part of the tree is unreadable the reference-to-directory match is
    unreliable, so the command must refuse rather than delete off a partial scan.
    """
    _skip_if_root()
    from fwl_io.cli import main

    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    unreadable = root / 'atmos' / 'locked'
    (unreadable / 'r99999999').mkdir(parents=True)
    os.chmod(unreadable, 0)
    try:
        exit_code = main(['prune', '--data-root', str(root), '--delete', '--yes'])
        err = capsys.readouterr().err
        assert exit_code == 1, 'a scan error is a non-zero, non-deleting exit'
        assert 'cannot be fully read' in err
        assert dirs['superseded'].is_dir(), 'nothing is deleted off a partial scan'
    finally:
        os.chmod(unreadable, stat.S_IRWXU)


def test_apply_prune_removes_exactly_the_planned_superseded_set(tmp_path, monkeypatch):
    """apply_prune deletes the plan it was given: the shown superseded pin, no more.

    The apply step acts on the confirmed plan rather than a fresh scan, so the
    set deleted is the set the user saw.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    plan = plan_prune(data_root=root)
    result = apply_prune(plan, data_root=root)

    assert not dirs['superseded'].exists(), 'the planned superseded pin is removed'
    assert dirs['referenced'].is_dir(), 'the current pin is kept'
    assert dirs['orphaned'].is_dir(), 'an orphan is not a default target'
    assert {c.rel for c in result.removed} == {f'{SUBDIR}/r{OLD_RECID}'}


def test_apply_prune_ignores_a_version_dir_that_appeared_after_the_plan(tmp_path, monkeypatch):
    """A version dir created after the plan is not deleted, because it was never shown.

    apply_prune acts on the plan's candidate set, so a directory the user never
    saw and never confirmed cannot be removed by the apply step.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    plan = plan_prune(data_root=root)
    late = root / SUBDIR / 'r14000000'
    late.mkdir(parents=True)
    (late / 'data.dat').write_bytes(b'appeared after the plan\n')

    apply_prune(plan, data_root=root)

    assert not dirs['superseded'].exists(), 'the planned superseded pin is still removed'
    assert late.is_dir(), 'a version dir not in the plan is never deleted by apply'


# A manifest key cased differently from the directory names actually written to
# disk, so classification must match it through filesystem identity and a
# case-insensitive subdir comparison rather than a literal string comparison.
_CI_KEY = 'Star.Tracks.baraffe_2015'
_CI_SUBDIR = 'Star/Tracks/baraffe_2015'


def _write_case_mismatched_manifest(tmp_path):
    """Write a manifest whose declared subdir differs in case from the on-disk one."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{_CI_KEY}]\nzenodo = "{ZENODO}"\nrequired_by = ["mors"]\n')
    return manifest


def test_a_case_mismatched_manifest_subdir_still_matches_the_on_disk_version(tmp_path, monkeypatch):
    """A manifest key cased differently from the on-disk subdir is still matched.

    APFS is case-insensitive but case-preserving, so a manifest may spell a
    dataset's subdir with different casing than the directory actually carries
    on disk. Both the referenced-version identity check and the known-subdir
    match must still recognise the two as the same dataset.
    """
    root = tmp_path / 'data'
    root.mkdir(parents=True)
    if not _fs_is_case_insensitive(root):
        pytest.skip('this filesystem is case-sensitive')
    _install_manifest(monkeypatch, _write_case_mismatched_manifest(tmp_path))
    lower_subdir = _CI_SUBDIR.lower()
    referenced = root / lower_subdir / f'r{RECID}'
    superseded = root / lower_subdir / f'r{OLD_RECID}'
    referenced.mkdir(parents=True)
    (referenced / 'data.dat').write_bytes(_REFERENCED_BYTES)
    superseded.mkdir(parents=True)
    (superseded / 'data.dat').write_bytes(_SUPERSEDED_BYTES)
    _write_stamp(superseded, OLD_RECID, _CI_SUBDIR)

    report = plan_prune(data_root=root)
    states = _states(report)

    assert states[f'{lower_subdir}/r{RECID}'] == REFERENCED
    assert states[f'{lower_subdir}/r{OLD_RECID}'] == SUPERSEDED


def test_orphans_are_blocked_when_no_manifest_declares_any_dataset(tmp_path, monkeypatch):
    """An empty reference set stops an orphan-including deletion without the override.

    Nothing declared means nothing can be told apart from an environment this
    one simply cannot see, so the loudest opt-in still needs the explicit
    override before it will touch a directory.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('')
    _install_manifest(monkeypatch, manifest)
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert report.empty_reference_set
    refusal = report.deletion_refusal(include_orphans=True)
    assert refusal is not None
    assert 'no installed manifest declares any dataset' in refusal
    for directory in dirs.values():
        assert directory.exists(), 'an empty reference set blocks every deletion under orphans'


def test_the_allow_empty_reference_set_override_permits_orphan_deletion(tmp_path, monkeypatch):
    """The override lets an orphan-including run proceed with no manifest declared.

    An unstamped directory still survives even under the override: the stamp
    guard is independent of the reference-set checks.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('')
    _install_manifest(monkeypatch, manifest)
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    report = prune_versions(
        data_root=root, delete=True, include_orphans=True, allow_empty_reference_set=True
    )

    assert report.empty_reference_set
    assert not dirs['superseded'].exists(), 'a stamped directory is removed under the override'
    assert not dirs['orphaned'].exists(), 'a stamped directory is removed under the override'
    assert dirs['referenced'].is_dir(), 'an unstamped directory is never removed'


def test_the_cli_refuses_to_delete_orphans_when_the_reference_set_is_empty(
    tmp_path, monkeypatch, capsys
):
    """The CLI reports the empty-reference-set refusal and deletes nothing."""
    from fwl_io.cli import main

    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('')
    _install_manifest(monkeypatch, manifest)
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    exit_code = main(['prune', '--data-root', str(root), '--delete', '--yes', '--include-orphans'])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert 'no installed manifest declares any dataset' in err
    for directory in dirs.values():
        assert directory.exists(), 'nothing is deleted when the reference set is empty'


def test_the_cli_refuses_to_delete_when_a_manifest_did_not_load(tmp_path, monkeypatch, capsys):
    """The CLI reports the incomplete-reference-set refusal and deletes nothing.

    This drives ``main()`` through the same ``deletion_refusal`` branch that
    the library-level blocked test exercises, so the CLI's own wiring to it is
    checked directly rather than assumed from the library test.
    """
    from fwl_io.cli import main

    broken = tmp_path / 'broken.toml'
    broken.write_text('this is not valid toml [[[\n')
    _install_manifest(
        monkeypatch,
        _write_manifest(tmp_path),
        extra_eps=[('brokenprovider', lambda: broken)],
    )
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    exit_code = main(['prune', '--data-root', str(root), '--delete', '--yes'])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert 'the reference set is incomplete' in err
    assert dirs['superseded'].is_dir(), 'nothing is deleted when a manifest failed to load'


def test_an_unstamped_version_dir_is_unrecognised_and_never_deleted(tmp_path, monkeypatch):
    """A version-shaped directory with no stamp is left alone, even under orphans."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    unstamped = root / SUBDIR / 'r16000000'
    unstamped.mkdir(parents=True)
    (unstamped / 'data.dat').write_bytes(b'no stamp here\n')

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert _states(report)[f'{SUBDIR}/r16000000'] == UNRECOGNISED
    assert unstamped.is_dir(), 'an unstamped version directory is never removed'


def test_a_stamp_naming_a_different_record_id_is_unrecognised(tmp_path, monkeypatch):
    """A stamp that names another record id does not vouch for this directory."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    mismatched = root / SUBDIR / 'r17000000'
    mismatched.mkdir(parents=True)
    (mismatched / 'data.dat').write_bytes(b'mismatched stamp\n')
    _write_stamp(mismatched, '99999999', SUBDIR)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert _states(report)[f'{SUBDIR}/r17000000'] == UNRECOGNISED
    assert mismatched.is_dir(), 'a stamp naming another record id is never removed'


def test_remove_one_refuses_a_candidate_with_no_matching_stamp(tmp_path):
    """The delete step re-checks the stamp rather than trusting the plan's state."""
    root = tmp_path / 'data'
    version = root / SUBDIR / f'r{OLD_RECID}'
    version.mkdir(parents=True)
    (version / 'data.dat').write_bytes(b'no stamp\n')
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert result.state == REFUSED
    assert 'no matching stamp' in result.detail
    assert version.is_dir()


def test_a_superseded_dir_containing_a_nested_version_dir_is_unrecognised(tmp_path, monkeypatch):
    """A version directory is a leaf; one holding another version dir is not trusted.

    A candidate that contains a further ``r<digits>`` directory is downgraded
    to ``UNRECOGNISED`` at classification time, before deletion is even
    considered.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    nested = dirs['superseded'] / 'r18000000'
    nested.mkdir()
    (nested / 'data.dat').write_bytes(b'nested version\n')

    report = plan_prune(data_root=root)

    assert _states(report)[f'{SUBDIR}/r{OLD_RECID}'] == UNRECOGNISED
    assert dirs['superseded'].is_dir()


def test_remove_one_refuses_a_candidate_containing_a_nested_version_or_stamp(tmp_path):
    """The delete step re-checks for nested content rather than trusting the plan's state."""
    root = tmp_path / 'data'
    version = root / SUBDIR / f'r{OLD_RECID}'
    version.mkdir(parents=True)
    (version / 'data.dat').write_bytes(_SUPERSEDED_BYTES)
    _write_stamp(version, OLD_RECID, SUBDIR)
    nested = version / 'r19000000'
    nested.mkdir()
    (nested / 'data.dat').write_bytes(b'nested\n')
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert result.state == REFUSED
    assert result.detail == 'contains a nested version; not removed'
    assert version.is_dir() and nested.is_dir()


def test_remove_one_refuses_a_candidate_a_referenced_symlink_points_into(tmp_path):
    """A file a current pin symlinks into is protected even outside its own directory.

    ``referenced_symlink_targets`` is threaded in explicitly here rather than
    built from a real referenced tree, isolating the guard from the discovery
    step that normally computes it.
    """
    root = tmp_path / 'data'
    version = root / SUBDIR / f'r{OLD_RECID}'
    version.mkdir(parents=True)
    target = version / 'data.dat'
    target.write_bytes(_SUPERSEDED_BYTES)
    _write_stamp(version, OLD_RECID, SUBDIR)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(
        candidate,
        root,
        referenced=set(),
        referenced_symlink_targets=frozenset({target.resolve()}),
    )

    assert result.state == REFUSED
    assert 'symlinks into this directory' in result.detail
    assert version.is_dir()


def test_a_referenced_symlink_into_a_superseded_dir_blocks_its_removal(tmp_path, monkeypatch):
    """A superseded directory a live pin's own file symlinks into survives an end-to-end prune."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    link = dirs['referenced'] / 'shared.dat'
    link.symlink_to(dirs['superseded'] / 'data.dat')

    report = prune_versions(data_root=root, delete=True)

    assert dirs['superseded'].is_dir(), 'a directory a referenced symlink points into survives'
    assert _states(report)[f'{SUBDIR}/r{OLD_RECID}'] == REFUSED


def test_remove_one_refuses_a_candidate_reached_via_a_differently_cased_symlink_target(tmp_path):
    """A referenced symlink target still blocks removal when its path is spelled in another case.

    The containment check compares by filesystem identity (device and inode),
    not by path string, so a target reached through a case-flipped ancestor
    segment still names the same directory as the candidate on a
    case-insensitive filesystem. A plain string-prefix comparison would see
    two paths that share no common casing and miss the containment, letting
    the deletion through even though a live symlink points into it.
    """
    root = tmp_path / 'data'
    version = root / SUBDIR / f'r{OLD_RECID}'
    version.mkdir(parents=True)
    target = version / 'data.dat'
    target.write_bytes(_SUPERSEDED_BYTES)
    _write_stamp(version, OLD_RECID, SUBDIR)
    if not _fs_is_case_insensitive(root):
        pytest.skip('this filesystem is case-sensitive')
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)
    mismatched_target = version.parent / f'R{OLD_RECID}' / 'data.dat'

    result = _remove_one(
        candidate,
        root,
        referenced=set(),
        referenced_symlink_targets=frozenset({mismatched_target.resolve()}),
    )

    assert result.state == REFUSED
    assert 'symlinks into this directory' in result.detail
    assert version.is_dir()


def test_apply_prune_refuses_when_a_fetch_lock_appears_after_the_plan(tmp_path, monkeypatch):
    """apply_prune refuses and records why when a fetch lock appears after the plan.

    A plan and its apply are two separate calls, and a fetch can start
    between them. Both ``ok`` and ``apply_refusal`` must show the refusal, not
    just one of them, since a caller may check either.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    plan = plan_prune(data_root=root)
    lock_dir = root / _LOCK_DIRNAME
    lock_dir.mkdir(parents=True)
    held = FileLock(str(lock_dir / 'demo.lock'), timeout=0)
    held.acquire()
    try:
        result = apply_prune(plan, data_root=root)

        assert not result.ok, 'a lock taken after the plan must not read as a clean run'
        assert result.apply_refusal is not None
        assert 'a fetch lock is held on the data root' in result.apply_refusal
        assert 'refused at apply time' in result.summary()
        assert dirs['superseded'].is_dir(), 'nothing is deleted once the lock blocks apply'
    finally:
        held.release()


def test_apply_prune_does_not_delete_a_candidate_demoted_to_orphaned_before_apply(
    tmp_path, monkeypatch
):
    """A candidate the plan called superseded is not deleted once it turns orphaned.

    Uninstalling the manifest between the plan and the apply call drops the
    subdir from the current reference set, so the candidate's true state at
    apply time is orphaned, not superseded. A default apply, without the
    orphan opt-in, must read that fresh state rather than the plan's
    snapshot, or it deletes data no longer provably safe to remove.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    plan = plan_prune(data_root=root)
    assert _states(plan)[f'{SUBDIR}/r{OLD_RECID}'] == SUPERSEDED

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [])
    result = apply_prune(plan, data_root=root)

    assert dirs['superseded'].is_dir(), 'a candidate demoted to orphaned is not removed by default'
    assert _states(result)[f'{SUBDIR}/r{OLD_RECID}'] == ORPHANED


def test_prune_empty_parents_removes_an_empty_chain_up_to_the_root(tmp_path):
    """An empty chain of parent directories is removed up to, not including, the root."""
    root = tmp_path / 'data'
    leaf_parent = root / 'a' / 'b' / 'c'
    leaf_parent.mkdir(parents=True)

    _prune_empty_parents(leaf_parent, root)

    assert root.is_dir(), 'the root itself is never removed'
    assert not (root / 'a').exists(), 'the whole empty chain above is pruned'


def test_prune_empty_parents_stops_at_a_parent_still_holding_a_sibling(tmp_path):
    """The walk up stops as soon as a parent still holds something else."""
    root = tmp_path / 'data'
    sibling = root / 'a' / 'b' / 'sibling'
    empty_leaf_parent = root / 'a' / 'b' / 'c'
    sibling.mkdir(parents=True)
    empty_leaf_parent.mkdir(parents=True)

    _prune_empty_parents(empty_leaf_parent, root)

    assert not empty_leaf_parent.exists(), 'the now-empty leaf parent is removed'
    assert (root / 'a' / 'b').is_dir(), 'a parent still holding a sibling directory is kept'
    assert sibling.is_dir()


def test_prune_empty_parents_leaves_a_symlinked_directory_alone(tmp_path):
    """A symlink in the ascent chain is left alone, not removed or followed."""
    root = tmp_path / 'data'
    root.mkdir()
    real_target = root / 'real_target'
    real_target.mkdir()
    link = root / 'a' / 'link'
    link.parent.mkdir(parents=True)
    link.symlink_to(real_target, target_is_directory=True)

    _prune_empty_parents(link, root)

    assert link.is_symlink(), 'a symlinked directory is left alone, not removed'
    assert real_target.is_dir(), 'the symlink target is untouched'


def test_a_held_fetch_lock_blocks_every_deletion(tmp_path, monkeypatch):
    """A lock file currently held by another process stops deletion outright.

    The lock directory holds an opaque hash per guarded path, so the check
    covers the whole tree: any lock held anywhere means a fetch may be under
    way and nothing may be deleted until it releases.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    lock_dir = root / _LOCK_DIRNAME
    lock_dir.mkdir(parents=True)
    held = FileLock(str(lock_dir / 'demo.lock'), timeout=0)
    held.acquire()
    try:
        report = prune_versions(data_root=root, delete=True)

        assert report.lock_held
        refusal = report.deletion_refusal(include_orphans=False)
        assert refusal is not None
        assert 'a fetch lock is held on the data root' in refusal
        assert 'a fetch lock is held on the data root' in report.summary()
        assert dirs['superseded'].is_dir(), 'nothing is deleted while a fetch lock is held'
    finally:
        held.release()


def test_an_unreadable_lock_file_blocks_every_deletion_too(tmp_path, monkeypatch):
    """A lock file this cannot even test is treated as held, not as absent.

    Its state cannot be proven safe, so it must block deletion the same way
    an actually-held lock does, rather than being skipped over.
    """
    _skip_if_root()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    lock_dir = root / _LOCK_DIRNAME
    lock_dir.mkdir(parents=True)
    unreadable = lock_dir / 'demo.lock'
    unreadable.write_text('')
    os.chmod(unreadable, 0)
    try:
        report = prune_versions(data_root=root, delete=True)

        assert report.lock_held
        assert dirs['superseded'].is_dir(), 'nothing is deleted while a lock cannot be checked'
    finally:
        os.chmod(unreadable, stat.S_IRWXU)


def test_shadows_known_subdir_descends_through_two_levels():
    """A version-shaped segment two levels above a declared subdir is still descended into."""
    known = {'opacity/r1000/deep/nested'}

    assert _shadows_known_subdir('opacity/r1000', known, case_insensitive=False)
    assert not _shadows_known_subdir('opacity/r2000', known, case_insensitive=False)


# A third record id for a pin that advances between a plan and its apply.
NEW_RECID = '15800000'


def _write_pinned_manifest(tmp_path, record_id):
    """Write (or rewrite) the manifest so the one dataset pins ``record_id``."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{KEY}]\nzenodo = "10.5281/zenodo.{record_id}"\nrequired_by = ["mors"]\n')
    return manifest


def _stamped_version(root, rel_parent, record_id, stamp_subdir=None):
    """Create ``root/rel_parent/r<record_id>`` with one file and a fetcher stamp.

    The stamp names ``stamp_subdir``, which defaults to ``rel_parent``: the
    subdir a real fetch into that directory would have recorded.
    """
    directory = root / rel_parent / f'r{record_id}'
    directory.mkdir(parents=True)
    (directory / 'data.dat').write_bytes(b'version payload\n')
    _write_stamp(directory, record_id, rel_parent if stamp_subdir is None else stamp_subdir)
    return directory


def test_apply_prune_keeps_a_directory_the_plan_showed_as_referenced(tmp_path, monkeypatch):
    """A pin that advances between plan and apply cannot turn the old pin into a target.

    The plan showed the current pin as referenced, so the user never confirmed
    its deletion. When the manifest moves to a newer record before apply, that
    directory is superseded in a fresh view, but it was not in the confirmed
    delete set and must survive.
    """
    _install_manifest(monkeypatch, _write_pinned_manifest(tmp_path, RECID))
    root = tmp_path / 'data'
    old = _stamped_version(root, SUBDIR, OLD_RECID)
    current = _stamped_version(root, SUBDIR, RECID)
    new = _stamped_version(root, SUBDIR, NEW_RECID)
    plan = plan_prune(data_root=root)
    assert _states(plan) == {
        f'{SUBDIR}/r{OLD_RECID}': SUPERSEDED,
        f'{SUBDIR}/r{RECID}': REFERENCED,
        f'{SUBDIR}/r{NEW_RECID}': SUPERSEDED,
    }

    _write_pinned_manifest(tmp_path, NEW_RECID)
    result = apply_prune(plan, data_root=root)

    assert current.is_dir(), 'the plan listed this as referenced, so apply must not remove it'
    assert new.is_dir(), 'the new pin is referenced at apply time and is kept'
    assert not old.exists(), 'the planned superseded directory is still removed'
    assert {c.rel for c in result.removed} == {f'{SUBDIR}/r{OLD_RECID}'}


def test_apply_prune_keeps_a_directory_the_plan_showed_as_unrecognised(tmp_path, monkeypatch):
    """A directory unstamped at plan time is not removed after a stamp appears.

    A fetch in progress writes its stamp last, so a directory can be
    unrecognised in the plan and stamped by the time apply runs. It was never
    shown as a target, so apply leaves it alone.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    late = root / SUBDIR / 'r14000000'
    late.mkdir(parents=True)
    (late / 'data.dat').write_bytes(b'mid-fetch\n')
    plan = plan_prune(data_root=root)
    assert _states(plan)[f'{SUBDIR}/r14000000'] == UNRECOGNISED

    _write_stamp(late, '14000000', SUBDIR)
    apply_prune(plan, data_root=root)

    assert late.is_dir(), 'a directory the plan did not list as a target is never removed'


def test_the_cli_keeps_the_plans_referenced_pin_when_the_pin_moves_during_the_prompt(
    tmp_path, monkeypatch, capsys
):
    """The directories deleted after the confirm are at most the ones the confirm listed.

    The manifest pin moves while the prompt waits. The confirm listed only the
    old superseded directory, so the directory the plan showed as referenced
    must still be on disk after the user types yes.
    """
    from fwl_io.cli import main

    _install_manifest(monkeypatch, _write_pinned_manifest(tmp_path, RECID))
    root = tmp_path / 'data'
    old = _stamped_version(root, SUBDIR, OLD_RECID)
    current = _stamped_version(root, SUBDIR, RECID)
    new = _stamped_version(root, SUBDIR, NEW_RECID)
    new_pin_on_disk = new  # the pin the manifest moves to while the prompt waits

    def _move_pin_then_confirm(_prompt=''):
        _write_pinned_manifest(tmp_path, NEW_RECID)
        return 'yes'

    monkeypatch.setattr(builtins, 'input', _move_pin_then_confirm)

    exit_code = main(['prune', '--data-root', str(root), '--delete'])
    out = capsys.readouterr().out

    assert current.is_dir(), 'the pin the plan showed as referenced survives the confirm'
    assert new_pin_on_disk.is_dir()
    assert not old.exists()
    assert f'{SUBDIR}/r{RECID}: removed' not in out
    assert exit_code == 0


def test_a_candidate_with_an_unreadable_subdirectory_is_unrecognised_and_kept(
    tmp_path, monkeypatch
):
    """The leaf check fails closed when it cannot read part of a candidate.

    An unreadable subdirectory may hold a nested version or stamp, so a
    candidate the check cannot fully read is not proven to be a leaf. It is
    classed unrecognised and a delete run leaves every file of it in place.
    """
    _skip_if_root()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    inner = dirs['superseded'] / 'inner'
    _stamped_version(inner, '.', '7', stamp_subdir='elsewhere')
    os.chmod(inner, 0)
    try:
        report = prune_versions(data_root=root, delete=True)

        assert _states(report)[f'{SUBDIR}/r{OLD_RECID}'] == UNRECOGNISED
        assert (dirs['superseded'] / _STAMP_FILENAME).is_file(), 'the stamp is not removed'
        assert (dirs['superseded'] / 'data.dat').is_file(), 'no file of the candidate is removed'
    finally:
        os.chmod(inner, stat.S_IRWXU)


def test_an_unreadable_nested_version_dir_is_caught_by_its_name(tmp_path, monkeypatch):
    """A nested ``r<digits>`` directory counts even when it cannot be opened."""
    _skip_if_root()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    nested = dirs['superseded'] / 'r7'
    nested.mkdir()
    os.chmod(nested, 0)
    try:
        report = prune_versions(data_root=root, delete=True)

        assert _states(report)[f'{SUBDIR}/r{OLD_RECID}'] == UNRECOGNISED
        assert (dirs['superseded'] / _STAMP_FILENAME).is_file()
    finally:
        os.chmod(nested, stat.S_IRWXU)


def test_a_stamped_copy_outside_its_stamped_subdir_is_unrecognised_and_kept(tmp_path, monkeypatch):
    """A stamp only vouches for the directory the fetch wrote it into.

    A user who copies a fetched version into their own directory carries its
    stamp along, with the original subdir inside it. That copy is not the
    fetcher's directory, so even the orphan opt-in leaves it, and the user
    directory above it, in place.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)
    copy = _stamped_version(root, 'my_runs/backup', OLD_RECID, stamp_subdir=SUBDIR)

    report = prune_versions(data_root=root, delete=True, include_orphans=True)

    assert _states(report)[f'my_runs/backup/r{OLD_RECID}'] == UNRECOGNISED
    assert copy.is_dir(), 'the stamped copy in a user directory is kept'
    assert (root / 'my_runs').is_dir(), 'its user parent directory is kept'


def test_a_failed_delete_leaves_the_remnant_in_staging_and_names_it(tmp_path, monkeypatch):
    """A delete that fails part way never leaves a stampless tree at the old path.

    The directory is moved into the staging directory before it is deleted, so
    a failure leaves the remnant there, named in the result, and the version
    path itself is gone as a whole.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    def _fail(path, *args, **kwargs):
        raise OSError('simulated failure part way through')

    monkeypatch.setattr('fwl_io.prune.shutil.rmtree', _fail)

    report = prune_versions(data_root=root, delete=True)

    [failed] = report.problems
    assert failed.state == REMOVE_FAILED
    assert not dirs['superseded'].exists(), 'nothing is left at the version path'
    staged = [p for p in (root / _STAGING_DIRNAME).iterdir() if p.name.startswith('prune-')]
    assert len(staged) == 1
    assert (staged[0] / _STAMP_FILENAME).is_file(), 'the remnant is whole and still stamped'
    assert str(staged[0]) in failed.detail, 'the result names where the remnant is'
    assert not report.ok

    monkeypatch.undo()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    later = plan_prune(data_root=root)
    assert later.staged_remnants == (staged[0],), 'every later plan still reports the remnant'
    assert 'partly deleted version dir(s) left in' in later.summary()


def test_a_failed_move_aside_leaves_the_directory_whole(tmp_path, monkeypatch):
    """When the move into staging fails, the directory stays at its path untouched."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)

    def _fail(src, dst):
        raise OSError('simulated rename failure')

    monkeypatch.setattr('fwl_io.prune.os.rename', _fail)

    report = prune_versions(data_root=root, delete=True)

    [failed] = report.problems
    assert failed.state == REMOVE_FAILED
    assert (dirs['superseded'] / _STAMP_FILENAME).is_file()
    assert (dirs['superseded'] / 'data.dat').is_file()


def test_a_symlinked_staging_directory_is_never_used(tmp_path, monkeypatch):
    """A staging path that is a symlink could lead outside the root, so nothing moves."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    outside_staging = tmp_path / 'outside_staging'
    outside_staging.mkdir()
    shutil.rmtree(root / _STAGING_DIRNAME)
    (root / _STAGING_DIRNAME).symlink_to(outside_staging, target_is_directory=True)

    report = prune_versions(data_root=root, delete=True)

    [refused] = report.problems
    assert refused.state == REFUSED
    assert refused.detail.endswith('is not a plain directory; not removed')
    assert (dirs['superseded'] / 'data.dat').is_file()
    assert list(outside_staging.iterdir()) == [], 'nothing is moved outside the root'


def test_a_lock_taken_during_a_delete_run_stops_the_remaining_removals(tmp_path, monkeypatch):
    """The fetch lock is checked again before each removal, not once per run.

    A fetch that starts after the first directory is removed must stop the
    rest of the run, and the result must not read as a clean one.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    second = _stamped_version(root, SUBDIR, '14000000')
    lock_dir = root / _LOCK_DIRNAME
    lock_dir.mkdir()
    held = FileLock(str(lock_dir / 'demo.lock'), timeout=0)
    real_rmtree = shutil.rmtree

    def _rmtree_then_lock(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        if not held.is_locked:
            held.acquire()

    monkeypatch.setattr('fwl_io.prune.shutil.rmtree', _rmtree_then_lock)
    try:
        report = prune_versions(data_root=root, delete=True)
    finally:
        if held.is_locked:
            held.release()

    assert len(report.removed) == 1
    [refused] = report.problems
    assert (refused.state, refused.detail) == (
        REFUSED,
        'a fetch lock is held on the data root; not removed',
    )
    assert refused.path.is_dir(), 'the directory after the lock appeared is kept'
    assert {dirs['superseded'], second} == {report.removed[0].path, refused.path}
    assert not report.ok


def test_a_plan_never_creates_a_missing_data_root(tmp_path):
    """A dry run on a root that does not exist fails and leaves no directory behind."""
    missing = tmp_path / 'no_such_root'

    with pytest.raises(FileNotFoundError, match='does not exist'):
        plan_prune(data_root=missing)
    with pytest.raises(FileNotFoundError, match='does not exist'):
        prune_versions(data_root=missing)

    assert not missing.exists()


def test_the_cli_reports_a_missing_data_root_without_creating_it(tmp_path, capsys):
    """A mistyped --data-root is an error, not a new empty tree."""
    from fwl_io.cli import main

    missing = tmp_path / 'no_such_root'

    exit_code = main(['prune', '--data-root', str(missing)])

    assert exit_code == 1
    assert 'does not exist' in capsys.readouterr().err
    assert not missing.exists()


def test_apply_prune_refuses_a_plan_built_for_another_root(tmp_path, monkeypatch):
    """A plan from one root applied to another deletes nothing and is not ok."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root_a = tmp_path / 'a'
    root_b = tmp_path / 'b'
    dirs_a = _make_tree(root_a)
    dirs_b = _make_tree(root_b)
    plan = plan_prune(data_root=root_a)

    result = apply_prune(plan, data_root=root_b)

    assert not result.ok
    assert result.apply_refusal is not None
    assert 'built for another root' in result.apply_refusal
    assert dirs_a['superseded'].is_dir() and dirs_b['superseded'].is_dir()


def test_the_case_probe_looks_inside_the_root_not_at_its_name(tmp_path):
    """A root whose own name has no cased letters is still probed correctly.

    The answer must match what the filesystem holding the root's entries does
    with a flipped spelling of one of them.
    """
    root = tmp_path / '2026'
    root.mkdir()
    (root / 'Data').mkdir()

    assert _fs_is_case_insensitive(root) == (root / 'dATA').exists()


def test_the_case_probe_leaves_an_empty_root_empty(tmp_path):
    """The probe file used when the root has no suitable entry is removed again."""
    root = tmp_path / 'data'
    root.mkdir()
    (root / '123').mkdir()

    _fs_is_case_insensitive(root)

    assert [p.name for p in root.iterdir()] == ['123']


def test_remove_one_refuses_a_mount_point(tmp_path, monkeypatch):
    """A version directory that is a mount point is never deleted."""
    root = tmp_path / 'data'
    version = _stamped_version(root, SUBDIR, OLD_RECID)
    monkeypatch.setattr('fwl_io.prune.os.path.ismount', lambda path: Path(path) == version)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (
        REFUSED,
        'is a mount point or on another filesystem; not removed',
    )
    assert (version / 'data.dat').is_file()


def test_remove_one_refuses_a_directory_on_another_device(tmp_path, monkeypatch):
    """A version directory whose device differs from the root's is never deleted."""
    root = tmp_path / 'data'
    version = _stamped_version(root, SUBDIR, OLD_RECID)
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if Path(path) != version:
            return st
        fields = list(st[:10])
        fields[2] = st.st_dev + 1
        return os.stat_result(fields)

    monkeypatch.setattr('fwl_io.prune.os.lstat', _lstat)
    # ismount reads os.lstat too; pin it so only the device comparison can refuse.
    monkeypatch.setattr('fwl_io.prune.os.path.ismount', lambda path: False)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (
        REFUSED,
        'is a mount point or on another filesystem; not removed',
    )
    assert (version / 'data.dat').is_file()


def test_remove_one_refuses_a_candidate_with_a_mount_below_it(tmp_path, monkeypatch):
    """A subdirectory on another filesystem inside a candidate blocks its removal."""
    root = tmp_path / 'data'
    version = _stamped_version(root, SUBDIR, OLD_RECID)
    mounted = version / 'scratch'
    mounted.mkdir()
    (mounted / 'other_fs.dat').write_bytes(b'on another filesystem\n')
    real_lstat = os.lstat

    def _lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if Path(path) != mounted:
            return st
        fields = list(st[:10])
        fields[2] = st.st_dev + 1
        return os.stat_result(fields)

    monkeypatch.setattr('fwl_io.prune.os.lstat', _lstat)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (REFUSED, 'contains a mount point; not removed')
    assert (mounted / 'other_fs.dat').is_file()


def test_a_symlinked_version_name_inside_a_candidate_makes_it_unrecognised(tmp_path, monkeypatch):
    """A nested ``r<digits>`` entry counts even when it is a symlink the walk never follows."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (dirs['superseded'] / 'r7').symlink_to(elsewhere, target_is_directory=True)

    report = prune_versions(data_root=root, delete=True)

    assert _states(report)[f'{SUBDIR}/r{OLD_RECID}'] == UNRECOGNISED
    assert (dirs['superseded'] / _STAMP_FILENAME).is_file()


def test_remove_one_puts_back_a_directory_whose_parent_was_swapped(tmp_path, monkeypatch):
    """A parent replaced by a symlink after the checks cannot redirect the delete.

    The swap happens during the last lock check, so the rename would move a
    same-named directory from outside the data root. The moved entry is not the
    one that was checked, so it goes back where it came from and nothing is deleted.
    """
    root = tmp_path / 'data'
    version = _stamped_version(root, SUBDIR, OLD_RECID)
    outside = tmp_path / 'user_elsewhere'
    victim = _stamped_version(outside, '', OLD_RECID, stamp_subdir=SUBDIR)
    parent = version.parent

    def _swap(_root):
        parent.rename(parent.with_name('moved_away'))
        parent.symlink_to(outside, target_is_directory=True)
        return False

    monkeypatch.setattr('fwl_io.prune._any_lock_held', _swap)
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{OLD_RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced=set())

    assert (result.state, result.detail) == (
        REFUSED,
        'changed while it was checked; not removed',
    )
    assert (victim / 'data.dat').read_bytes() == b'version payload\n'
    assert (parent.with_name('moved_away') / version.name / 'data.dat').is_file()
    assert not list((root / _STAGING_DIRNAME).glob('prune-*'))


def test_a_symlink_chain_through_a_candidate_protects_it(tmp_path, monkeypatch):
    """A referenced link whose chain passes through a candidate keeps that candidate.

    The final target lies outside the candidate, so only the intermediate hop
    shows that removing the candidate would break the referenced link.
    """
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    target = tmp_path / 'outside.dat'
    target.write_bytes(b'outside\n')
    (dirs['superseded'] / 'hop.dat').symlink_to(target)
    (dirs['referenced'] / 'link.dat').symlink_to(dirs['superseded'] / 'hop.dat')

    report = prune_versions(data_root=root, delete=True)

    [refused] = report.problems
    assert (refused.state, refused.detail) == (
        REFUSED,
        'a referenced file symlinks into this directory; not removed',
    )
    assert (dirs['referenced'] / 'link.dat').read_bytes() == b'outside\n'


def test_apply_marks_a_planned_directory_gone_when_it_vanished(tmp_path, monkeypatch):
    """A planned target deleted out of band is reported gone, with no bytes to reclaim."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    plan = plan_prune(data_root=root)
    shutil.rmtree(dirs['superseded'])

    result = apply_prune(plan, data_root=root)

    [gone] = [c for c in result.candidates if c.rel == f'{SUBDIR}/r{OLD_RECID}']
    assert (gone.state, gone.size) == (GONE, 0)
    assert result.reclaimable(SUPERSEDED) == 0
    assert result.ok


def test_apply_refuses_a_planned_target_that_is_no_longer_a_version_dir(tmp_path, monkeypatch):
    """A planned target replaced by a plain file is refused, not left in its planned state."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    plan = plan_prune(data_root=root)
    shutil.rmtree(dirs['superseded'])
    dirs['superseded'].write_bytes(b'a user file now\n')

    result = apply_prune(plan, data_root=root)

    [refused] = result.problems
    assert (refused.state, refused.detail) == (REFUSED, 'no longer a version dir; not removed')
    assert dirs['superseded'].read_bytes() == b'a user file now\n'
    assert not result.ok


def test_apply_refuses_a_plan_whose_target_now_links_outside_the_root(tmp_path, monkeypatch):
    """A planned target swapped for a symlink out of the root refuses the whole apply."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    plan = plan_prune(data_root=root)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 'keep.dat').write_bytes(b'keep\n')
    shutil.rmtree(dirs['superseded'])
    dirs['superseded'].symlink_to(elsewhere, target_is_directory=True)

    result = apply_prune(plan, data_root=root)

    assert 'outside' in result.apply_refusal
    assert (elsewhere / 'keep.dat').is_file()
    assert dirs['orphaned'].is_dir() and not result.removed
    assert not result.ok


@pytest.mark.parametrize('insensitive', [True, False])
def test_the_case_insensitive_path_is_exercised_on_every_filesystem(
    tmp_path, monkeypatch, insensitive
):
    """The case-folded subdir and stamp match run on any filesystem, not only macOS.

    The probe is forced, so a case-sensitive CI leg covers the same logic a
    case-insensitive one does. Only the older pin exists on disk, spelled
    differently from the manifest and from its own stamp.
    """
    monkeypatch.setattr('fwl_io.prune._fs_is_case_insensitive', lambda root: insensitive)
    _install_manifest(monkeypatch, _write_case_mismatched_manifest(tmp_path))
    root = tmp_path / 'data'
    lower_subdir = _CI_SUBDIR.lower()
    old = _stamped_version(root, lower_subdir, OLD_RECID, stamp_subdir=_CI_SUBDIR)

    report = prune_versions(data_root=root, delete=True)

    if insensitive:
        assert _states(report)[f'{lower_subdir}/r{OLD_RECID}'] == 'removed'
        assert not old.exists()
    else:
        assert _states(report)[f'{lower_subdir}/r{OLD_RECID}'] == UNRECOGNISED
        assert old.is_dir()


def test_shadows_known_subdir_folds_case_only_when_asked():
    """A differently cased segment above a declared subdir matches only case-insensitively."""
    known = {'opacity/r1000/deep'}

    assert _shadows_known_subdir('Opacity/R1000', known, case_insensitive=True)
    assert not _shadows_known_subdir('Opacity/R1000', known, case_insensitive=False)


def test_the_cli_keeps_an_unstamped_user_directory_and_its_parent(tmp_path, monkeypatch, capsys):
    """An unstamped version-shaped directory outside every known subdir survives a full prune.

    This is a user's own directory that only happens to match the version
    name shape. Even the loudest run through the command line keeps it, and
    keeps the user directory above it.
    """
    from fwl_io.cli import main

    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    user_dir = root / 'my_runs' / 'r2024'
    user_dir.mkdir(parents=True)
    (user_dir / 'notes.txt').write_bytes(b'my own work\n')

    exit_code = main(['prune', '--data-root', str(root), '--delete', '--yes', '--include-orphans'])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert (user_dir / 'notes.txt').is_file()
    assert (root / 'my_runs').is_dir()
    assert 'my_runs/r2024: unrecognised' in out
    assert not dirs['superseded'].exists() and not dirs['orphaned'].exists()


def test_a_dry_run_does_not_walk_the_referenced_trees(tmp_path, monkeypatch):
    """The symlink targets inside referenced directories are collected only to delete."""
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    _make_tree(root)

    def _no_walk(referenced):
        raise AssertionError('a dry run must not walk the referenced trees')

    monkeypatch.setattr('fwl_io.prune._referenced_symlink_targets', _no_walk)

    plan_prune(data_root=root)
    prune_versions(data_root=root)


def test_an_unreadable_part_of_a_referenced_version_blocks_deletion(tmp_path, monkeypatch):
    """A referenced tree that cannot be fully read may hide a symlink into a candidate."""
    _skip_if_root()
    _install_manifest(monkeypatch, _write_manifest(tmp_path))
    root = tmp_path / 'data'
    dirs = _make_tree(root)
    hidden = dirs['referenced'] / 'hidden'
    hidden.mkdir()
    (hidden / 'link.dat').symlink_to(dirs['superseded'] / 'data.dat')
    os.chmod(hidden, 0)
    try:
        report = prune_versions(data_root=root, delete=True)

        assert report.scan_error is not None
        assert 'a referenced version cannot be fully read' in report.scan_error
        assert dirs['superseded'].is_dir()
        assert not report.ok
    finally:
        os.chmod(hidden, stat.S_IRWXU)


@pytest.mark.parametrize(
    'zenodo', ['10.5281/zenodo.15729114', '10.5281/zenodo.8', ' 10.5281/zenodo.42 ']
)
def test_the_prune_version_path_matches_the_fetchers_own(tmp_path, zenodo):
    """The reference set's version path is the one the Fetcher writes into.

    Prune computes each pinned directory with its own formula, so this pins
    that formula to the Fetcher's ``target_dir``: if the two ever differ, a
    live pin would read as superseded.
    """
    from fwl_io.fetch import create_fetcher
    from fwl_io.manifest import Dataset
    from fwl_io.relocate import _version_dir

    ds = Dataset(name='baraffe_2015', key=KEY, zenodo=zenodo)
    fetcher = create_fetcher(
        subdir=ds.subdir,
        zenodo=ds.zenodo,
        registry={'data.dat': 'md5:' + '0' * 32},
        data_root=tmp_path,
    )

    assert tmp_path / _version_dir(ds) == fetcher.target_dir
