"""Tests for :mod:`fwl_io.relocate`, moving a legacy tree into the current layout.

The contract exercised here is that files move only when they are provably the
files the registry describes, that everything else is reported and left exactly
as it was, and that a command whose whole job is moving data on disk cannot
delete anything the user still has.

No test here touches the network. Every fetcher a check might build is pointed
at a closed port, and the relocation path itself never downloads.
"""

from __future__ import annotations

import hashlib

import pytest

from fwl_io.relocate import (
    ABSENT,
    ALREADY_CURRENT,
    INCOMPLETE,
    MISMATCH,
    MOVED,
    READY,
    UNRESOLVABLE,
    plan_relocations,
    relocate,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

# A dataset that really did move, so the legacy path under test is the one the
# shipped table records rather than one invented for the test.
KEY = 'star.tracks.baraffe_2015'
RECID = '15729114'
ZENODO = f'10.5281/zenodo.{RECID}'
LEGACY = 'stellar_evolution_tracks/Baraffe'
TARGET = f'star/tracks/baraffe_2015/r{RECID}'

# Two files of different lengths and contents, so a move that paired a name
# with the wrong digest could not pass by coincidence.
CONTENTS = {'BHAC15_tracks.dat': b'0.1 0.2 0.3\n', 'notes.txt': b'9.87\n'}


def _digests(names=None):
    chosen = CONTENTS if names is None else {n: CONTENTS[n] for n in names}
    return {n: f'sha256:{hashlib.sha256(b).hexdigest()}' for n, b in chosen.items()}


def _install_manifest(monkeypatch, tmp_path, *, with_registry=True):
    """Install a manifest declaring the migrated dataset, and its registry."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{KEY}]\nzenodo = "{ZENODO}"\nrequired_by = ["mors"]\n')
    if with_registry:
        lines = ''.join(f'{n} {d}\n' for n, d in sorted(_digests().items()))
        (tmp_path / f'{KEY}.registry.txt').write_text(lines)

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    return manifest


def _populate(directory, names=None, corrupt=()):
    directory.mkdir(parents=True, exist_ok=True)
    for name in CONTENTS if names is None else names:
        body = b'not the recorded contents\n' if name in corrupt else CONTENTS[name]
        (directory / name).write_bytes(body)


def test_a_verified_legacy_tree_moves_and_leaves_nothing_behind(tmp_path, monkeypatch):
    """Files that match the registry move, and the emptied directories go.

    Tidying the tree is the whole point of the command, so an empty
    ``stellar_evolution_tracks`` left standing afterwards would mean the user
    still has to finish the job by hand.
    """
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / LEGACY)

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [MOVED]
    for name, body in CONTENTS.items():
        assert (root / TARGET / name).read_bytes() == body, 'contents must survive the move'
    assert not (root / LEGACY).exists()
    assert not (root / 'stellar_evolution_tracks').exists(), 'the emptied parent goes too'
    assert root.is_dir(), 'the data root is not a leftover of the old layout'


def test_a_file_that_differs_from_the_registry_stops_the_move(tmp_path, monkeypatch):
    """A tree that is not what it claims is reported, and nothing is touched.

    Moving first and hashing afterwards would turn a stale copy into a stale
    copy at the location the fetcher now trusts, which is worse than leaving
    it where a reader can still tell it is old.
    """
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / LEGACY, corrupt=['notes.txt'])

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [MISMATCH]
    assert report.faults, 'a legacy tree that cannot be moved has to fail the run'
    assert (root / LEGACY / 'notes.txt').read_bytes() == b'not the recorded contents\n'
    assert (root / LEGACY / 'BHAC15_tracks.dat').is_file(), 'the sound file stays put as well'
    assert not (root / TARGET).exists(), 'no half-move: the target is not created'


def test_a_legacy_tree_missing_a_file_is_reported_not_half_moved(tmp_path, monkeypatch):
    """An incomplete tree is left whole rather than partly relocated.

    The edge case that matters: moving the files that are present would leave
    the dataset split across two layouts, which is the one state neither the
    reader nor the fetcher can interpret.
    """
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / LEGACY, names=['BHAC15_tracks.dat'])

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [INCOMPLETE]
    assert '1 of 2 file(s) absent' in report.entries[0].detail
    assert (root / LEGACY / 'BHAC15_tracks.dat').is_file()
    assert not (root / TARGET).exists()


def test_a_tree_already_at_its_current_location_is_left_alone(tmp_path, monkeypatch):
    """A dataset that has already moved is not a fault, and the old copy survives.

    The redundant copy is named so the user can remove it, and not removed
    here: deleting data nobody asked to lose is not this command's business.
    """
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / TARGET)
    _populate(root / LEGACY)

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [ALREADY_CURRENT]
    assert not report.faults, 'an already-tidy tree is a success'
    assert (root / LEGACY / 'notes.txt').is_file(), 'the redundant copy is reported, not deleted'
    assert 'redundant' in report.entries[0].detail
    # The closing line is all some readers see, and on a tree where everything
    # has already been refetched the old copies are the only thing to say.
    assert len(report.redundant) == 1
    assert 'still have an old copy on disk' in report.summary()


def test_a_current_tree_with_no_old_copy_beside_it_reports_nothing_to_reclaim(
    tmp_path, monkeypatch
):
    """Without the old directory there is no disk to reclaim, and none is claimed.

    The discriminating half of the case above: both report ``already-current``,
    so only the count separates a tree that still carries a duplicate from one
    that is genuinely finished.
    """
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / TARGET)

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [ALREADY_CURRENT]
    assert report.redundant == ()
    assert 'still have an old copy on disk' not in report.summary()
    assert 'redundant' not in report.entries[0].detail


def test_nothing_on_disk_is_reported_absent_rather_than_missing(tmp_path, monkeypatch):
    """A machine that never had the legacy layout has nothing to relocate."""
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [ABSENT]
    assert not report.faults, 'never having had the old layout is not a fault'
    assert report.moved == () and report.ready == ()
    assert list(root.iterdir()) == [], 'a relocation must not create the tree it inspects'


def test_a_dry_run_reports_the_move_without_making_it(tmp_path, monkeypatch):
    """The plan names what would move, and the tree is untouched afterwards."""
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'
    _populate(root / LEGACY)
    before = {p.name: p.read_bytes() for p in sorted((root / LEGACY).iterdir())}

    report = relocate(data_root=root, dry_run=True)

    assert [e.state for e in report.entries] == [READY]
    assert report.ready and not report.moved
    after = {p.name: p.read_bytes() for p in sorted((root / LEGACY).iterdir())}
    assert after == before
    assert not (root / TARGET).exists()

    # Discrimination: the same call without dry_run does move it, so the
    # assertions above pin the flag and not some other refusal.
    assert relocate(data_root=root).moved
    assert (root / TARGET / 'notes.txt').is_file()


def test_a_dataset_whose_registry_is_missing_is_reported_not_moved(tmp_path, monkeypatch):
    """Without a registry there is nothing to verify against, so nothing moves.

    The files may well be the right ones, but a relocation that assumed so
    would be moving on the strength of a directory name.
    """
    _install_manifest(monkeypatch, tmp_path, with_registry=False)
    root = tmp_path / 'data'
    _populate(root / LEGACY)

    report = relocate(data_root=root)

    assert [e.state for e in report.entries] == [UNRESOLVABLE]
    assert report.faults
    assert (root / LEGACY / 'notes.txt').is_file(), 'an unverifiable tree stays where it is'
    assert not (root / TARGET).exists()


def test_a_manifest_that_did_not_load_keeps_the_report_from_reading_complete(tmp_path, monkeypatch):
    """An unread manifest may be the one declaring the tree still sitting there.

    Nothing moved and nothing was found, which on its own is exactly what a
    finished tree looks like. The manifest that failed has to be carried, or
    the command reports success over data it never considered.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('this is not valid toml [[[\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    root = tmp_path / 'data'
    _populate(root / LEGACY)

    report = relocate(data_root=root)

    assert report.entries == (), 'no dataset was declared, so none could be considered'
    assert list(report.manifest_errors) == ['demoprovider']
    assert not report.ok, 'a report that looked at nothing must not read as a tidy tree'
    assert 'MANIFEST UNREADABLE' in report.summary()
    assert 'may be partial' in report.summary()
    assert (root / LEGACY / 'notes.txt').is_file(), 'the tree it could not judge is untouched'


def test_a_dataset_with_no_legacy_location_is_not_considered(tmp_path, monkeypatch):
    """A dataset created after the migration has no old location to leave.

    Its absence from the report is the point: the command speaks only about
    datasets that predate the layout, so a clean install has nothing to say.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[atmos_chem.networks.demo]\nzenodo = "10.5281/zenodo.1234567"\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])

    report = plan_relocations(data_root=tmp_path / 'data')

    assert report.entries == ()
    assert report.summary() == 'no dataset declares a legacy location'


def test_the_shipped_table_names_only_datasets_and_relative_locations():
    """Every entry is a dotted key and a location inside the data root.

    An absolute path or one climbing out of the root would be joined onto the
    tree and then walked, so the table is held to the same suspicion as any
    other file the package reads.
    """
    from pathlib import Path

    from fwl_io.relocate import _legacy_locations

    table = _legacy_locations()

    assert table, 'the table has to declare the datasets that have already moved'
    for key, location in table.items():
        assert '.' in key, f'{key!r} is not a dotted dataset key'
        assert not Path(location).is_absolute(), f'{location!r} is absolute'
        assert '..' not in Path(location).parts, f'{location!r} climbs out of the data root'
        assert location.strip('/') == location, f'{location!r} is not a clean relative path'
