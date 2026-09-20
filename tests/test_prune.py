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

import pytest

from fwl_io.fetch import _LOCK_DIRNAME, _STAGING_DIRNAME
from fwl_io.prune import (
    ORPHANED,
    REFERENCED,
    REFUSED,
    SHARED_TREE_WARNING,
    SUPERSEDED,
    PruneCandidate,
    _remove_one,
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


def _make_tree(root):
    """Build a data root with one dir of each state, plus a reserved-dir version.

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

    assert report.reclaimable(SUPERSEDED) == len(_SUPERSEDED_BYTES)
    assert report.reclaimable(ORPHANED) == len(_ORPHAN_BYTES)
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

    assert result.state == REFUSED
    assert link.is_symlink() and outside.is_dir()


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
    candidate = PruneCandidate(path=version, rel=f'{SUBDIR}/r{RECID}', state=SUPERSEDED)

    result = _remove_one(candidate, root, referenced={version.resolve()})

    assert result.state == REFUSED
    assert 'referenced' in result.detail
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
