"""Move a dataset left by the previous layout into the place it belongs now.

A tree fetched before the current layout existed holds directories such as
``stellar_evolution_tracks/Baraffe``. Unmigrated code still reads them, so
they are left alone and age out as their consumers migrate. That is the right
default and the wrong one for anybody who wants the tree tidy today, which is
what this does: it finds the datasets an installed manifest declares, works
out where each one used to live, and moves the files across.

Nothing is moved on trust. Every present file is hashed against the registry
the manifest ships before anything is touched, and only a file that matches
moves; a present file whose contents do not match blocks the whole dataset,
so an absent file can never mask a corrupt one. A legacy tree holding none of
its registry's files is reported and left exactly where it is. The
alternative, moving first and discovering afterwards, turns a stale copy into
a stale copy in the place the fetcher will now believe.

Nothing is downloaded either. A dataset moved with some registry files still
absent stays that way at its new location; the fetcher is what fills it in,
once the move has happened.

A dataset packaged as an archive is reported rather than moved. Its registry
pins the packed archive, and a legacy tree holds the extracted members, so
there is nothing to hash the tree against.

A run assumes that no other process renames or replaces directories under the
data root while it moves files. Unlike a deletion, a move does not check the
fetch lock, so a fetch running at the same time is not detected at all; do
not relocate while a fetch could be running.
"""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from fwl_io.fetch import _hash_matches
from fwl_io.fs_guard import _inside, _is_regular_file, _open_dir_below, _open_or_make_dir_below
from fwl_io.paths import resolve_data_root

if TYPE_CHECKING:
    from fwl_io.manifest import Dataset

log = logging.getLogger('fwl.' + __name__)

_LAYOUT_RESOURCE = 'legacy_layout.toml'

# What a dataset's legacy tree turned out to be. ``READY`` covers a complete
# tree and one where every present file matches but others are absent, left
# for the fetcher to fill in later. ``INCOMPLETE`` is a legacy tree holding
# none of its registry's files; ``MISMATCH`` is a present file that fails
# its hash check.
READY = 'ready'
ABSENT = 'absent'
ALREADY_CURRENT = 'already-current'
INCOMPLETE = 'incomplete'
MISMATCH = 'mismatch'
UNRESOLVABLE = 'unresolvable'
MOVED = 'moved'
FAILED = 'failed'
SPLIT = 'split'

#: States that mean a legacy tree is there but cannot be moved as it stands.
FAULT_STATES = (INCOMPLETE, MISMATCH, UNRESOLVABLE, FAILED, SPLIT)


@dataclass(frozen=True)
class Relocation:
    """One dataset's legacy tree, and what can be done with it."""

    key: str
    state: str
    legacy_dir: Path | None = None
    target_dir: Path | None = None
    #: The present-and-verified subset of the registry this move attempted:
    #: set from ``READY`` on, carried unchanged through ``MOVED``, ``FAILED``
    #: and ``SPLIT``; empty for every other state. Not the dataset's whole
    #: registry, an absent file is never in here.
    files: tuple[str, ...] = ()
    detail: str = ''
    legacy_present: bool = False

    @property
    def faulty(self) -> bool:
        """True when a legacy tree is present but was not usable."""
        return self.state in FAULT_STATES

    def summary(self) -> str:
        """One line a person reads, naming the dataset and its outcome."""
        line = f'{self.key}: {self.state}'
        if self.state in (READY, MOVED):
            line += f', {len(self.files)} file(s) {self.legacy_dir} -> {self.target_dir}'
        if self.detail:
            line += f', {self.detail}'
        return line


@dataclass(frozen=True)
class RelocationReport:
    """Every dataset considered, whether or not anything happened to it.

    ``manifest_errors`` is carried beside them because a manifest that failed
    to load, or was dropped for a conflict, declares datasets nobody here got to
    look at. Without it a report
    covering nothing would read exactly like a tree with nothing left to move.
    ``conflict_providers`` names which of ``manifest_errors`` was a conflict
    rather than a load failure, so the summary can tell a caller which repair
    applies.
    """

    entries: tuple[Relocation, ...] = ()
    manifest_errors: dict[str, str] = field(default_factory=dict)
    layout_error: str | None = None
    conflict_providers: frozenset[str] = field(default_factory=frozenset)

    def _in_state(self, *states: str) -> tuple[Relocation, ...]:
        return tuple(e for e in self.entries if e.state in states)

    @property
    def ok(self) -> bool:
        """True when every legacy tree found was dealt with and none was skipped."""
        return not self.faults and not self.manifest_errors and self.layout_error is None

    @property
    def ready(self) -> tuple[Relocation, ...]:
        """Datasets whose legacy tree checks out and is waiting to be moved."""
        return self._in_state(READY)

    @property
    def moved(self) -> tuple[Relocation, ...]:
        """Datasets whose files were moved into the current layout."""
        return self._in_state(MOVED)

    @property
    def redundant(self) -> tuple[Relocation, ...]:
        """Datasets whose old copy is still on disk beside the current one.

        Nothing here removes it, so it is worth counting: this is the disk the
        user can reclaim by hand, and it is the whole reason to run the
        command against a tree where every dataset has already been refetched.
        """
        return tuple(e for e in self.entries if e.state == ALREADY_CURRENT and e.legacy_present)

    @property
    def faults(self) -> tuple[Relocation, ...]:
        """Legacy trees that are present and could not be moved."""
        return tuple(e for e in self.entries if e.faulty)

    def summary(self) -> str:
        """A short report, one line per dataset plus a closing count."""
        lines = [e.summary() for e in sorted(self.entries, key=lambda e: e.key)]
        for provider, error in sorted(self.manifest_errors.items()):
            if provider in self.conflict_providers:
                lines.append(f'{provider}: MANIFEST NOT USED, {error}')
            else:
                lines.append(f'{provider}: MANIFEST FAILED TO LOAD, {error}')
        if self.layout_error is not None:
            # Without this the run reports nothing to do, which is what a tidy
            # tree also reports, and the two are not the same answer.
            lines.append(f'LEGACY LAYOUT UNREADABLE, {self.layout_error}')
        if not lines:
            return 'no dataset declares a legacy location'
        done, waiting, bad = len(self.moved), len(self.ready), len(self.faults)
        closing = f'{done} moved, {waiting} ready to move, {bad} left in place'
        if self.redundant:
            # Named because it is the disk a user can reclaim by hand, and
            # because on a tree where everything has already been refetched it
            # is the only thing the run has to tell them.
            closing += (
                f'; {len(self.redundant)} dataset(s) still have an old copy on disk, '
                'which was left alone'
            )
        if self.manifest_errors:
            # An unloaded or conflict-dropped manifest may declare a dataset this
            # tree still holds, so the counts above are a floor.
            closing += f'; {len(self.manifest_errors)} manifest(s) not used, so this may be partial'
        lines.append(closing)
        return '\n'.join(lines)


def _legacy_locations() -> tuple[dict[str, str], str | None]:
    """Read the shipped table of where each dataset used to live.

    An entry naming an absolute path or climbing out of the data root is
    dropped. The table ships with the package, but it is still a file being
    turned into a path that files get moved out of, so it earns the same
    suspicion as a name inside a provenance stamp.

    Returns
    -------
    tuple[dict[str, str], str | None]
        Dataset keys mapped to their legacy directory, and why the table could
        not be read, which is ``None`` when it was read.
    """
    try:
        text = files('fwl_io.data').joinpath(_LAYOUT_RESOURCE).read_text()
        table = tomllib.loads(text).get('legacy', {})
        if not isinstance(table, dict):
            raise TypeError(f'[legacy] is {type(table).__name__}, not a table')
    except (OSError, ValueError, TypeError, ImportError) as exc:
        # Reported rather than raised, like a manifest that will not load, and
        # carried back so the run cannot read as a tree with nothing to do.
        log.error('cannot read %s, so no legacy location is known: %s', _LAYOUT_RESOURCE, exc)
        return {}, f'{_LAYOUT_RESOURCE}: {exc}'
    safe = {}
    for key, location in table.items():
        if not isinstance(location, str):
            log.warning('legacy location for %s is not a path: %r', key, location)
            continue
        if Path(location).is_absolute() or '..' in Path(location).parts:
            log.warning('legacy location for %s is not inside the data root: %r', key, location)
            continue
        safe[key] = location
    return safe, None


def _escaping(
    legacy_dir: Path, target_dir: Path, names: tuple[str, ...], root: Path
) -> Path | None:
    """The first path here that leaves ``root``, or ``None`` if all stay inside.

    Every file is checked, not just the two directories: a registry name may
    nest, and a symlinked component inside it resolves somewhere else entirely
    while the directory holding it looks perfectly ordinary.
    """
    for path in (legacy_dir, target_dir):
        if not _inside(path, root):
            return path
    for name in names:
        for path in (legacy_dir / name, target_dir / name):
            if not _inside(path, root):
                return path
    return None


def _unmovable(ds: Dataset, registry: dict[str, str]) -> str | None:
    """Why this dataset cannot be relocated at all, or ``None`` if it can.

    Both cases would otherwise reach :func:`_classify` and be answered from a
    comparison that cannot mean what it says.

    Parameters
    ----------
    ds : Dataset
        The dataset as its manifest declares it.
    registry : dict[str, str]
        Registry filenames mapped to their expected digests.

    Returns
    -------
    str | None
        A sentence naming the obstacle, or ``None`` when there is none.
    """
    if not registry:
        # Every check here is "does the tree hold what the registry lists", and
        # over an empty registry that is vacuously true: an untouched legacy
        # directory would be reported as already moved.
        return 'empty registry: run "fwl-io sync" for this dataset first'
    if ds.extract is not None:
        # The registry pins the packed archive, which a legacy tree never held:
        # it holds the extracted members. Comparing against it would call an
        # intact tree incomplete, and moving on that basis would be worse.
        return (
            f'{ds.extract} archive dataset: its registry pins the archive rather '
            'than the extracted files, so this tree cannot be verified against it; '
            'move it by hand, or delete it and let the fetcher rebuild it'
        )
    return None


def _classify(
    legacy_dir: Path, target_dir: Path, registry: dict[str, str]
) -> tuple[str, str, tuple[str, ...]]:
    """Decide what the two trees on disk allow, without touching either.

    Returns
    -------
    tuple
        ``(state, detail, files)``, where ``files`` is the subset of
        ``registry`` this dataset should actually move: empty for every state
        but ``READY``, where it is every present file, verified against the
        registry. A present file that does not match still blocks the whole
        move, so an absent file never masks a corrupt one.
    """
    if _all_match(target_dir, registry):
        detail = f'already at {target_dir}'
        if legacy_dir.is_dir():
            # Both copies are intact, so the legacy one is redundant rather
            # than needed. Naming it is as far as this goes: deleting data the
            # user has not asked to lose is not this command's business.
            detail += f'; the copy at {legacy_dir} is now redundant and was left alone'
        return ALREADY_CURRENT, detail, ()
    if not legacy_dir.is_dir():
        return ABSENT, '', ()
    if legacy_dir.is_symlink():
        return UNRESOLVABLE, f'{legacy_dir} is a symlink, not a plain directory; not moved', ()
    try:
        present, linked = [], []
        for name in registry:
            path = legacy_dir / name
            if path.is_symlink():
                linked.append(name)
            elif _is_regular_file(path):
                present.append(name)
    except OSError as exc:
        return UNRESOLVABLE, f'cannot read {legacy_dir}: {exc}', ()
    if linked:
        return (
            UNRESOLVABLE,
            f'{len(linked)} of {len(registry)} file(s) in {legacy_dir} are symlinks, '
            'not plain files; not moved',
            (),
        )
    if not present:
        return INCOMPLETE, f'0 of {len(registry)} file(s) present in {legacy_dir}', ()
    try:
        wrong = [name for name in present if not _hash_matches(legacy_dir / name, registry[name])]
    except OSError as exc:
        return UNRESOLVABLE, f'cannot read {legacy_dir}: {exc}', ()
    if wrong:
        return MISMATCH, f'{len(wrong)} file(s) differ from the registry in {legacy_dir}', ()
    absent = sorted(set(registry) - set(present))
    if absent:
        detail = (
            f'{len(present)} of {len(registry)} file(s) present and verified in {legacy_dir}; '
            f'{len(absent)} absent, to be fetched at the new location'
        )
    else:
        detail = ''
    return READY, detail, tuple(sorted(present))


def _all_match(directory: Path, registry: dict[str, str]) -> bool:
    """True when ``directory`` already holds every registry file, intact."""
    if not directory.is_dir():
        return False
    try:
        return all(
            _is_regular_file(directory / name) and _hash_matches(directory / name, digest)
            for name, digest in registry.items()
        )
    except OSError:
        return False


def plan_relocations(data_root: str | Path | None = None) -> RelocationReport:
    """Report what a relocation would do, touching nothing.

    Parameters
    ----------
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.

    Returns
    -------
    RelocationReport
        One entry per dataset that declares a legacy location, whether or not
        that location exists on this machine.
    """
    from fwl_io.manifest import _discover_all

    root = resolve_data_root(data_root)
    locations, layout_error = _legacy_locations()
    entries: list[Relocation] = []
    seen: set[str] = set()
    discovery = _discover_all()
    conflict_providers = frozenset(discovery.conflict_models)
    for provider_datasets in discovery.found.values():
        for ds in provider_datasets:
            legacy = locations.get(ds.key)
            if legacy is None or ds.key in seen:
                continue
            seen.add(ds.key)
            legacy_dir = root / legacy
            try:
                registry = ds.registry()
                target_dir = root / _version_dir(ds)
            except Exception as exc:  # noqa: BLE001 -- reported, never raised
                entries.append(
                    Relocation(ds.key, UNRESOLVABLE, legacy_dir=legacy_dir, detail=str(exc))
                )
                continue
            unmovable = _unmovable(ds, registry) if legacy_dir.is_dir() else None
            if unmovable is not None:
                entries.append(
                    Relocation(
                        ds.key,
                        UNRESOLVABLE,
                        legacy_dir=legacy_dir,
                        target_dir=target_dir,
                        detail=unmovable,
                    )
                )
                continue
            outside = _escaping(legacy_dir, target_dir, tuple(registry), root)
            if outside is not None:
                # A symlink is how this happens in a real tree: every joined
                # path looks clean and only resolving one shows it leaves.
                entries.append(
                    Relocation(
                        ds.key,
                        UNRESOLVABLE,
                        legacy_dir=legacy_dir,
                        target_dir=target_dir,
                        detail=f'{outside} resolves outside the data root {root}',
                    )
                )
                continue
            state, detail, files = _classify(legacy_dir, target_dir, registry)
            if state == READY:
                # The same no-follow open the actual move uses, so a symlinked
                # component anywhere in legacy_dir's path is caught here,
                # before the plan says the dataset is ready, not only once the
                # move itself opens it: is_dir() inside _classify already
                # resolves symlinks, so a legacy_dir reached through one still
                # passed as READY; only this open can tell the two apart. Only
                # a READY result needs it, so a dataset _classify already
                # refuses or already calls done never depends on this
                # platform's capabilities.
                try:
                    fd = _open_dir_below(root, legacy_dir.relative_to(root).parts)
                except (OSError, AttributeError) as exc:
                    # AttributeError is this platform lacking O_DIRECTORY: the
                    # same primitive the actual move uses, probed here so a
                    # legacy tree that is actually present is refused by name
                    # instead of crashing the plan or the move itself.
                    state, detail, files = (
                        UNRESOLVABLE,
                        f'{legacy_dir} cannot be safely opened: {exc}',
                        (),
                    )
                else:
                    os.close(fd)
            entries.append(
                Relocation(
                    ds.key,
                    state,
                    legacy_dir=legacy_dir,
                    target_dir=target_dir,
                    files=files,
                    detail=detail,
                    legacy_present=legacy_dir.is_dir(),
                )
            )
    return RelocationReport(
        tuple(entries), dict(discovery.errors), layout_error, conflict_providers
    )


def _version_dir(ds: Dataset) -> str:
    """The dataset's location below the data root, version directory included."""
    from fwl_io.doi import zenodo_record_id

    return f'{ds.subdir}/r{zenodo_record_id(ds.zenodo)}'


def _move_below_root(
    root: Path, src_parent: tuple[str, ...], dst_parent: tuple[str, ...], filename: str
) -> None:
    """Move ``filename`` from ``root/src_parent`` to ``root/dst_parent``.

    Both parents are opened as directory handles from ``root``, following no
    symlink below it, so a parent directory replaced by a symlink after the
    plan's containment check is refused rather than walked into; the
    destination's parent is created the same no-follow way if it does not
    exist yet. The source entry is checked once more, against the open parent
    handle, to still be a plain file rather than a symlink, immediately
    before the move. That check and the move itself are still two syscalls on
    the same name, so this does not cover the entry itself being swapped in
    the instant between them; closing that window needs no other process to
    rename or replace entries under the data root while a run is in progress,
    the same assumption this module states for the rest of a run.
    """
    src_fd = _open_dir_below(root, src_parent)
    try:
        dst_fd = _open_or_make_dir_below(root, dst_parent)
        try:
            st = os.stat(filename, dir_fd=src_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                rel = '/'.join((*src_parent, filename))
                raise OSError(f'{rel} is not a plain file; not moved')
            os.replace(filename, filename, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _move_one(entry: Relocation, root: Path) -> Relocation:
    """Move one verified legacy tree, leaving nothing half-moved behind."""
    assert entry.legacy_dir is not None and entry.target_dir is not None
    outside = _escaping(entry.legacy_dir, entry.target_dir, entry.files, root)
    if outside is not None:
        # Checked here as well as when the plan is built, so the guarantee that
        # this only ever moves files inside the data root belongs to the code
        # that does the moving rather than to whoever called it.
        return Relocation(
            entry.key,
            UNRESOLVABLE,
            legacy_dir=entry.legacy_dir,
            target_dir=entry.target_dir,
            detail=f'{outside} resolves outside the data root {root}',
        )
    try:
        legacy_rel = entry.legacy_dir.relative_to(root).parts
        target_rel = entry.target_dir.relative_to(root).parts
    except ValueError as exc:
        return Relocation(
            entry.key,
            UNRESOLVABLE,
            legacy_dir=entry.legacy_dir,
            target_dir=entry.target_dir,
            detail=str(exc),
        )
    done: list[str] = []
    try:
        for name in entry.files:
            name_parts = PurePosixPath(name).parts
            _move_below_root(
                root,
                legacy_rel + name_parts[:-1],
                target_rel + name_parts[:-1],
                name_parts[-1],
            )
            done.append(name)
    except OSError as exc:
        # Put back what was moved, so a failure part way leaves the tree as it
        # was rather than split across two layouts. The same dir-fd primitive
        # as the forward move, reversed, so the restore gets the same
        # containment guarantee the move itself does.
        unrestored = []
        for name in done:
            name_parts = PurePosixPath(name).parts
            try:
                _move_below_root(
                    root,
                    target_rel + name_parts[:-1],
                    legacy_rel + name_parts[:-1],
                    name_parts[-1],
                )
            except OSError:
                unrestored.append(name)
        if unrestored:
            # The state the rollback exists to prevent, reached anyway. It is
            # reported as its own thing because the remedy is a person looking
            # at two directories, not a rerun.
            log.error('could not restore %s to %s', ', '.join(unrestored), entry.legacy_dir)
            return Relocation(
                entry.key,
                SPLIT,
                legacy_dir=entry.legacy_dir,
                target_dir=entry.target_dir,
                files=entry.files,
                detail=(
                    f'{exc}; {len(unrestored)} file(s) could not be put back, so this '
                    f'dataset is now split between {entry.legacy_dir} and {entry.target_dir}'
                ),
            )
        return Relocation(
            entry.key,
            FAILED,
            legacy_dir=entry.legacy_dir,
            target_dir=entry.target_dir,
            files=entry.files,
            detail=str(exc),
        )
    _prune(entry.legacy_dir, root)
    log.info('relocated %s to %s', entry.key, entry.target_dir)
    return Relocation(
        entry.key,
        MOVED,
        legacy_dir=entry.legacy_dir,
        target_dir=entry.target_dir,
        files=entry.files,
        detail=entry.detail,
    )


def _prune(directory: Path, root: Path) -> None:
    """Remove the emptied legacy tree, and any parent it leaves empty.

    Only ever removes a directory with nothing in it, so no data can be lost
    here, and the walk upward stops at the data root: the root itself is not a
    leftover of the previous layout and other datasets live beside it. A
    registry name may nest, so the emptied subdirectories inside go first, or
    the husk they leave keeps the whole legacy directory standing.
    """
    root = root.resolve()
    if directory.is_dir():
        # Deepest first so a child is gone before its parent is tried, and the
        # name breaks the tie so the walk is the same on every filesystem.
        for path in sorted(
            directory.rglob('*'), key=lambda p: (len(p.parts), str(p)), reverse=True
        ):
            # A symlink answers is_dir() for whatever it points at, and rmdir
            # refuses it, so following one here would both leave the tree
            # standing and reach outside it.
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
    while directory.resolve() != root and directory.resolve().is_relative_to(root):
        try:
            if any(directory.iterdir()):
                return
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def relocate_all(data_root: str | Path | None = None, dry_run: bool = False) -> RelocationReport:
    """Move every legacy tree that checks out into the current layout.

    Every present file in the legacy location that matches its recorded
    digest moves; an absent file is reported for the fetcher to fill in at
    the new location. A present file that does not match its digest, or a
    legacy tree holding none of its registry's files, is reported and left
    untouched, as is a dataset already at its current location, which is the
    ordinary state once a fetch has happened there.

    Parameters
    ----------
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.
    dry_run : bool
        Report what would move without moving it.

    Returns
    -------
    RelocationReport
        The plan, with each moved dataset's entry rewritten to say so.
    """
    plan = plan_relocations(data_root)
    if dry_run:
        return plan
    root = resolve_data_root(data_root)
    done = []
    for entry in plan.entries:
        if entry.state != READY:
            done.append(entry)
            continue
        moved = _move_one(entry, root)
        done.append(moved)
        if moved.state in (FAILED, SPLIT):
            # Reported and left for a person to look at; a fault in one
            # dataset says nothing about the ones after it, so they still run.
            log.error('%s could not be relocated', moved.key)
    return RelocationReport(
        tuple(done), dict(plan.manifest_errors), plan.layout_error, plan.conflict_providers
    )
