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
    Relocation,
    plan_relocations,
    relocate_all,
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

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root)

    assert [e.state for e in report.entries] == [ALREADY_CURRENT]
    assert report.redundant == ()
    assert 'still have an old copy on disk' not in report.summary()
    assert 'redundant' not in report.entries[0].detail


def test_nothing_on_disk_is_reported_absent_rather_than_missing(tmp_path, monkeypatch):
    """A machine that never had the legacy layout has nothing to relocate."""
    _install_manifest(monkeypatch, tmp_path)
    root = tmp_path / 'data'

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root, dry_run=True)

    assert [e.state for e in report.entries] == [READY]
    assert report.ready and not report.moved
    after = {p.name: p.read_bytes() for p in sorted((root / LEGACY).iterdir())}
    assert after == before
    assert not (root / TARGET).exists()

    # Discrimination: the same call without dry_run does move it, so the
    # assertions above pin the flag and not some other refusal.
    assert relocate_all(data_root=root).moved
    assert (root / TARGET / 'notes.txt').is_file()


def test_a_dataset_whose_registry_is_missing_is_reported_not_moved(tmp_path, monkeypatch):
    """Without a registry there is nothing to verify against, so nothing moves.

    The files may well be the right ones, but a relocation that assumed so
    would be moving on the strength of a directory name.
    """
    _install_manifest(monkeypatch, tmp_path, with_registry=False)
    root = tmp_path / 'data'
    _populate(root / LEGACY)

    report = relocate_all(data_root=root)

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

    report = relocate_all(data_root=root)

    assert report.entries == (), 'no dataset was declared, so none could be considered'
    assert list(report.manifest_errors) == ['demoprovider']
    assert not report.ok, 'a report that looked at nothing must not read as a tidy tree'
    assert 'MANIFEST UNREADABLE' in report.summary()
    assert 'may be partial' in report.summary()
    assert (root / LEGACY / 'notes.txt').is_file(), 'the tree it could not judge is untouched'


def test_a_nested_member_leaves_no_husk_behind(tmp_path, monkeypatch):
    """A registry name may nest, and the emptied subdirectory goes too.

    Removing only the directories above the legacy one leaves an empty `sub/`
    inside it, which keeps the whole legacy tree standing and then reads on a
    later run as an old copy still holding data, when it holds nothing.
    """
    from fwl_io.relocate import MOVED, _move_one

    root = tmp_path / 'data'
    legacy = root / LEGACY
    (legacy / 'sub').mkdir(parents=True)
    (legacy / 'sub' / 'nested.dat').write_bytes(CONTENTS['notes.txt'])
    target = root / TARGET
    entry = Relocation(KEY, READY, legacy_dir=legacy, target_dir=target, files=('sub/nested.dat',))

    result = _move_one(entry, root)

    assert result.state == MOVED
    assert (target / 'sub' / 'nested.dat').read_bytes() == CONTENTS['notes.txt']
    assert not legacy.exists(), 'the emptied subdirectory must not keep the tree alive'
    assert not (root / 'stellar_evolution_tracks').exists()


@pytest.mark.parametrize('escape', ['relative', 'absolute'], ids=['dot-dot', 'absolute'])
def test_files_outside_the_data_root_are_never_moved(tmp_path, escape):
    """A legacy path leaving the root is refused rather than followed.

    The table ships with the package, but it still becomes a path that files
    are moved out of, so it gets the same suspicion as a name inside a stamp.
    A symlinked legacy directory escapes the same way and only shows it when
    the path is resolved.
    """
    from fwl_io.relocate import _move_one

    root = tmp_path / 'data'
    root.mkdir()
    outside = tmp_path / 'outside_dataset'
    outside.mkdir()
    (outside / 'a.dat').write_bytes(CONTENTS['notes.txt'])
    legacy = root / '../outside_dataset' if escape == 'relative' else outside
    entry = Relocation(KEY, READY, legacy_dir=legacy, target_dir=root / TARGET, files=('a.dat',))

    result = _move_one(entry, root)

    assert result.state == UNRESOLVABLE
    assert 'outside the data root' in result.detail
    assert (outside / 'a.dat').is_file(), 'the file outside the root is untouched'
    assert not (root / TARGET).exists()


def test_the_shipped_table_is_filtered_at_the_point_it_is_read(monkeypatch):
    """An entry naming a path outside the root is dropped, not merely asserted about.

    A test over the shipped file proves what ships today; this proves the code
    refuses a bad entry, which is what protects a tree if the file ever changes.
    """
    import fwl_io.relocate as module

    table = '[legacy]\n"a.b" = "../escape"\n"c.d" = "/etc"\n"e.f" = "good/place"\n'

    class _Resource:
        def read_text(self):
            return table

    class _Package:
        def joinpath(self, name):
            return _Resource()

    monkeypatch.setattr(module, 'files', lambda package: _Package())

    kept = module._legacy_locations()

    assert kept == {'e.f': 'good/place'}, 'only the contained entry survives'


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


def test_a_rollback_that_cannot_restore_is_reported_as_a_split_tree(tmp_path, monkeypatch):
    """When the files cannot be put back, say so rather than call it a failure.

    A plain failure means the tree is as it was and a rerun is the remedy. This
    one means the dataset is in two places at once, which no rerun fixes and a
    person has to look at, so it gets a state of its own.
    """
    import fwl_io.relocate as module
    from fwl_io.relocate import SPLIT, _move_one

    root = tmp_path / 'data'
    legacy = root / LEGACY
    _populate(legacy)
    real_replace = module.os.replace
    calls = []

    def failing_replace(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) == 1:
            return real_replace(src, dst)
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(module.os, 'replace', failing_replace)
    entry = Relocation(
        KEY,
        READY,
        legacy_dir=legacy,
        target_dir=root / TARGET,
        files=tuple(sorted(CONTENTS)),
    )

    result = _move_one(entry, root)

    assert result.state == SPLIT, 'a tree in two places is not the same as an untouched one'
    assert 'split between' in result.detail
    assert result.faulty
    assert len(calls) == 3, 'one move succeeded, one failed, one rollback was attempted'


def test_a_failed_move_stops_the_run_rather_than_moving_more_data(tmp_path, monkeypatch):
    """After a dataset fails to move, the ones behind it are left alone.

    Continuing would move more data past a tree somebody already has to look
    at, and the entries that never ran are reported still ready rather than
    quietly dropped.
    """
    import fwl_io.relocate as module
    from fwl_io.relocate import FAILED, RelocationReport

    planned = RelocationReport(
        (
            Relocation('a.first', READY, legacy_dir=tmp_path / 'l1', target_dir=tmp_path / 't1'),
            Relocation('b.second', READY, legacy_dir=tmp_path / 'l2', target_dir=tmp_path / 't2'),
        )
    )
    monkeypatch.setattr(module, 'plan_relocations', lambda data_root=None: planned)
    monkeypatch.setattr(
        module,
        '_move_one',
        lambda entry, root: Relocation(entry.key, FAILED, detail='disk full'),
    )

    report = module.relocate_all(data_root=tmp_path)

    assert [e.state for e in report.entries] == [FAILED, READY]
    assert [e.key for e in report.ready] == ['b.second'], 'the untried one is still ready'
    assert len(report.faults) == 1


def test_pruning_never_removes_the_data_root(tmp_path):
    """The walk upward stops at the root even when the root is left empty.

    Discriminating on purpose: after a real move the root holds the new tree,
    so it is never a candidate for removal and the guard is never reached. Here
    it is the only thing standing between an emptied chain and the root itself.
    """
    from fwl_io.relocate import _prune

    root = tmp_path / 'data'
    nested = root / 'stellar_evolution_tracks' / 'Baraffe'
    nested.mkdir(parents=True)

    _prune(nested, root)

    assert not (root / 'stellar_evolution_tracks').exists(), 'the emptied chain goes'
    assert root.is_dir(), 'the root is not a leftover of the previous layout'
    assert list(root.iterdir()) == [], 'and it really was left empty, or this proves nothing'


def test_pruning_stops_at_a_parent_that_still_holds_something(tmp_path):
    """A shared parent survives while another dataset still lives under it."""
    from fwl_io.relocate import _prune

    root = tmp_path / 'data'
    legacy = root / 'stellar_evolution_tracks' / 'Baraffe'
    legacy.mkdir(parents=True)
    sibling = root / 'stellar_evolution_tracks' / 'Spada'
    sibling.mkdir()
    (sibling / 'keep.dat').write_bytes(b'x')

    _prune(legacy, root)

    assert not legacy.exists()
    assert (sibling / 'keep.dat').read_bytes() == b'x', 'the neighbour is untouched'
    assert (root / 'stellar_evolution_tracks').is_dir(), 'a parent still in use stays'


def test_two_datasets_sharing_one_legacy_directory_come_apart(tmp_path, monkeypatch):
    """Each dataset moves only the files its own registry names.

    A directory that held more than one dataset is the case where a prune
    keyed on the directory rather than on the files would take a neighbour's
    data with it, so the shared parent may go only once both have moved.
    """
    import fwl_io.relocate as module

    shared = 'stellar_evolution_tracks'
    first, second = 'star.tracks.baraffe_2015', 'star.tracks.spada_2013'
    monkeypatch.setattr(module, '_legacy_locations', lambda: {first: shared, second: shared})

    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        f'[{first}]\nzenodo = "10.5281/zenodo.15729114"\n\n'
        f'[{second}]\nzenodo = "10.5281/zenodo.7654321"\n'
    )
    (tmp_path / f'{first}.registry.txt').write_text(
        f'BHAC15_tracks.dat {_digests(["BHAC15_tracks.dat"])["BHAC15_tracks.dat"]}\n'
    )
    (tmp_path / f'{second}.registry.txt').write_text(
        f'notes.txt {_digests(["notes.txt"])["notes.txt"]}\n'
    )

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])
    root = tmp_path / 'data'
    _populate(root / shared)

    report = module.relocate_all(data_root=root)

    assert sorted(e.state for e in report.entries) == [MOVED, MOVED]
    assert (root / TARGET / 'BHAC15_tracks.dat').is_file()
    assert (root / 'star/tracks/spada_2013/r7654321/notes.txt').is_file()
    assert not (root / shared).exists(), 'the shared directory goes once both have moved'


def test_a_nested_name_reaching_outside_the_root_is_refused(tmp_path):
    """A symlinked component inside a member name escapes, and is caught.

    The directory holding it looks perfectly ordinary and passes every check
    on the directories alone, which is why the files are checked too: `rename`
    follows symlinks in the middle of a path, so the file that moves in is one
    from outside the tree and the original is gone.
    """
    from fwl_io.relocate import _move_one

    root = tmp_path / 'data'
    legacy = root / LEGACY
    legacy.mkdir(parents=True)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'nested.dat').write_bytes(CONTENTS['notes.txt'])
    (legacy / 'sub').symlink_to(outside, target_is_directory=True)
    entry = Relocation(
        KEY, READY, legacy_dir=legacy, target_dir=root / TARGET, files=('sub/nested.dat',)
    )

    result = _move_one(entry, root)

    assert result.state == UNRESOLVABLE
    assert 'resolves outside the data root' in result.detail
    assert (outside / 'nested.dat').is_file(), 'the file outside the tree is untouched'
    assert not (root / TARGET / 'sub' / 'nested.dat').exists()


def test_a_symlink_in_the_legacy_tree_does_not_abort_the_prune(tmp_path):
    """One entry that cannot be removed must not stop the rest being removed.

    A symlink answers ``is_dir`` for whatever it points at and ``rmdir``
    refuses it, so treating that refusal as the end of the walk would leave
    every emptied directory standing while the command reported success.
    """
    from fwl_io.relocate import _prune

    root = tmp_path / 'data'
    legacy = root / LEGACY
    (legacy / 'aaa_empty_one').mkdir(parents=True)
    (legacy / 'aaa_empty_two').mkdir()
    # Empty on purpose: a symlink to a non-empty directory is skipped by the
    # emptiness test before the removal is ever tried, so it would prove nothing.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    # Named to sort first, so the walk meets the symlink before the two empty
    # directories and an abort there would leave both of them standing.
    (legacy / 'zzz_link').symlink_to(elsewhere, target_is_directory=True)

    _prune(legacy, root)

    assert not (legacy / 'aaa_empty_one').exists(), 'the walk carried on past the symlink'
    assert not (legacy / 'aaa_empty_two').exists()
    assert (legacy / 'zzz_link').is_symlink(), 'the symlink itself is not ours to remove'
    assert elsewhere.is_dir(), 'and what it points at is not ours to remove either'


def test_a_table_value_that_is_not_a_path_is_dropped_not_raised(monkeypatch):
    """A non-string entry is refused before anything tries to build a path from it.

    Ordering matters here: constructing the path first raises ``TypeError`` out
    of the command, so the check that is meant to reject the value has to come
    before the value is used.
    """
    import fwl_io.relocate as module

    table = '[legacy]\n"a.b" = 42\n"c.d" = ["x"]\n"e.f" = "good/place"\n'

    class _Resource:
        def read_text(self):
            return table

    class _Package:
        def joinpath(self, name):
            return _Resource()

    monkeypatch.setattr(module, 'files', lambda package: _Package())

    assert module._legacy_locations() == {'e.f': 'good/place'}


def test_an_empty_registry_does_not_report_an_untouched_tree_as_moved(tmp_path, monkeypatch):
    """A dataset whose registry lists nothing is refused, not declared complete.

    Every comparison this module makes asks whether the tree holds what the
    registry lists, and over an empty registry each one is vacuously true. The
    legacy tree must survive the run for the report to have meant anything.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{KEY}]\nzenodo = "{ZENODO}"\nrequired_by = ["mors"]\n')
    (tmp_path / f'{KEY}.registry.txt').write_text('# no files\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])

    legacy_dir = tmp_path / LEGACY
    _populate(legacy_dir)
    # The precondition the assertions below rest on: real files are present, so
    # a pass cannot come from an empty tree.
    assert (legacy_dir / 'BHAC15_tracks.dat').is_file()

    report = relocate_all(data_root=tmp_path)

    (entry,) = [e for e in report.entries if e.key == KEY]
    assert entry.state == UNRESOLVABLE, f'empty registry reported as {entry.state}'
    assert entry.state not in (MOVED, ALREADY_CURRENT, READY)
    assert 'empty registry' in entry.detail
    assert not report.ok
    for name, body in CONTENTS.items():
        assert (legacy_dir / name).read_bytes() == body
    assert not (tmp_path / TARGET).exists()


def test_an_archive_dataset_is_refused_rather_than_called_incomplete(tmp_path, monkeypatch):
    """An archive dataset's registry pins the archive, which a legacy tree never held.

    Hashing the tree against it would report an intact tree as incomplete, so
    the dataset is refused by name instead, and nothing is moved either way.
    """
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(f'[{KEY}]\nzenodo = "{ZENODO}"\nrequired_by = ["mors"]\nextract = "tar"\n')
    archive_digest = f'sha256:{hashlib.sha256(b"packed").hexdigest()}'
    (tmp_path / f'{KEY}.registry.txt').write_text(f'bundle.tar.gz {archive_digest}\n')

    class _EP:
        name = 'demoprovider'

        def load(self):
            return lambda: manifest

    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: [_EP()])

    legacy_dir = tmp_path / LEGACY
    _populate(legacy_dir)
    # The tree holds the extracted members, exactly as a real legacy tree does,
    # and never the archive the registry names.
    assert (legacy_dir / 'BHAC15_tracks.dat').is_file()
    assert not (legacy_dir / 'bundle.tar.gz').exists()

    report = relocate_all(data_root=tmp_path)

    (entry,) = [e for e in report.entries if e.key == KEY]
    assert entry.state == UNRESOLVABLE, f'archive dataset reported as {entry.state}'
    assert entry.state != INCOMPLETE
    assert 'archive' in entry.detail
    for name, body in CONTENTS.items():
        assert (legacy_dir / name).read_bytes() == body
    assert not (tmp_path / TARGET).exists()


@pytest.mark.parametrize(
    ('table', 'why'),
    [
        ('[legacy]\n"a.b" = \n', 'unparseable TOML'),
        ('legacy = "not-a-table"\n', 'legacy is a string'),
        ('legacy = [1, 2]\n', 'legacy is an array'),
    ],
)
def test_an_unreadable_layout_table_reports_nothing_rather_than_raising(monkeypatch, table, why):
    """A table that will not load leaves no legacy locations, and no traceback.

    Every other unreadable input this command meets is carried in the report,
    so this one does not get to be the exception that aborts the run.
    """
    import fwl_io.relocate as module

    class _Resource:
        def read_text(self):
            return table

    class _Package:
        def joinpath(self, name):
            return _Resource()

    monkeypatch.setattr(module, 'files', lambda package: _Package())

    assert module._legacy_locations() == {}, why
