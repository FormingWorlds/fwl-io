"""Remove versioned dataset directories that no installed manifest references.

A manifest dataset lives at ``<data_root>/<subdir>/r<record-id>``, where the
record id is the Zenodo version the manifest pins. When a pin advances the
fetcher writes a new ``r<record-id>`` beside the old one, and the old one stays
on disk for as long as the tree exists. This finds those left-behind version
directories and, only when asked, deletes them.

Nothing is deleted on trust. A version directory is removed only when it can be
proven unreferenced: it must carry the fetcher's own stamp naming the record it
holds and the subdir it sits under, it must sit under a subdirectory that a
currently declared dataset uses (an older pin of a known dataset), and the
reference set it is checked against must be complete. If any installed
manifest fails to load, any dataset's version directory cannot be computed,
part of the tree cannot be read, or a fetch lock is held anywhere on the tree,
the run refuses to delete rather than act on a partial or contested view. An
orphan-including run also refuses when the reference set is empty.

A directory that matches the version-directory name shape but carries no stamp
naming its own record id and location is never a delete target: it is reported
separately as unrecognised, since the name alone is not proof of what it holds.

A version directory under a subdirectory no installed manifest knows is
reported as orphaned rather than removed. On a shared data tree it may be the
pinned version for a manifest installed in another environment, which this
process cannot see, so it is never deleted without an explicit opt-in.

The default is a dry run: the plan is printed and nothing is touched. Deletion
needs an explicit request and, unless suppressed, an interactive confirmation.

A run assumes that no other process renames or replaces directories under the
data root while it deletes. An fwl-io fetch is kept out only while it holds the
fetch lock, which is not the whole of every fetch.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

try:
    import fcntl
except ImportError:  # Windows: no flock, so locks cannot be probed and deletion is refused
    fcntl = None

from fwl_io.fetch import _LOCK_DIRNAME, _STAGING_DIRNAME, _STAMP_FILENAME, Fetcher
from fwl_io.paths import resolve_data_root
from fwl_io.relocate import _inside, _version_dir

log = logging.getLogger('fwl.' + __name__)

#: A version directory is ``r`` followed by the Zenodo record-id digits.
_VERSION_DIR_PATTERN = re.compile(r'r[0-9]+\Z')

#: Directories under the data root that fwl-io owns and this never walks into.
_RESERVED_DIRNAMES = frozenset({_LOCK_DIRNAME, _STAGING_DIRNAME})

# The state of one version directory on disk. Only ``SUPERSEDED`` (and
# ``ORPHANED`` under an explicit opt-in) is ever a delete target; the rest
# record why a directory was kept or what happened when one was removed.
REFERENCED = 'referenced'
SUPERSEDED = 'superseded'
ORPHANED = 'orphaned'
UNRECOGNISED = 'unrecognised'
REMOVED = 'removed'
REMOVE_FAILED = 'remove-failed'
REFUSED = 'refused'
GONE = 'gone'

#: Whether this Python's rmtree resists symlink swaps, read once at import.
_RMTREE_IS_SAFE = shutil.rmtree.avoids_symlink_attacks

#: Whether directory-relative open, rename and stat exist here, read once at import.
_DIR_FD_OK = {os.open, os.rename, os.stat, os.mkdir} <= os.supports_dir_fd

#: Largest stamp read; a real stamp lists at most one line per archive member.
_MAX_STAMP_BYTES = 32 * 1024 * 1024

#: Printed before any deletion that includes a superseded or orphaned directory.
SHARED_TREE_WARNING = (
    'WARNING: FWL_DATA is often a single tree shared between several Python '
    'environments. A version listed as superseded or orphaned here is only '
    'unreferenced for the manifests installed in THIS environment. It may still '
    'be the pinned version for another environment on this machine, and '
    'deleting it will break that environment. Continue only if you know no '
    'other environment relies on these versions.'
)


@dataclass(frozen=True)
class PruneCandidate:
    """One ``r<record-id>`` directory on disk, and what may be done with it."""

    path: Path
    rel: str
    state: str
    size: int = 0
    detail: str = ''

    def summary(self) -> str:
        """One line naming the directory, its state, and its size on disk."""
        line = f'{self.rel}: {self.state}, {_human_bytes(self.size)}'
        if self.detail:
            line += f', {self.detail}'
        return line


@dataclass(frozen=True)
class PruneReport:
    """Every version directory found, and the state of the reference set.

    ``manifest_errors`` and ``resolve_error`` are carried because either one
    means the reference set is incomplete. Without them a run that could prove
    nothing unreferenced would read exactly like a tree with nothing to remove.
    """

    candidates: tuple[PruneCandidate, ...] = ()
    manifest_errors: dict[str, str] = field(default_factory=dict)
    resolve_error: str | None = None
    scan_error: str | None = None
    known_subdirs: frozenset[str] = frozenset()
    lock_problem: str | None = None
    apply_refusal: str | None = None
    lock_warnings: tuple[str, ...] = ()
    staged_remnants: tuple[Path, ...] = ()

    def _in_state(self, *states: str) -> tuple[PruneCandidate, ...]:
        return tuple(c for c in self.candidates if c.state in states)

    @property
    def blocked(self) -> bool:
        """True when the reference set is incomplete, so nothing may be deleted."""
        return bool(self.manifest_errors) or self.resolve_error is not None

    @property
    def empty_reference_set(self) -> bool:
        """True when no installed manifest declares a single dataset."""
        return not self.known_subdirs

    @property
    def referenced(self) -> tuple[PruneCandidate, ...]:
        """Version directories a current pin uses; always kept."""
        return self._in_state(REFERENCED)

    @property
    def superseded(self) -> tuple[PruneCandidate, ...]:
        """Older pins of known datasets; the default delete target."""
        return self._in_state(SUPERSEDED)

    @property
    def orphaned(self) -> tuple[PruneCandidate, ...]:
        """Under a subdir no installed manifest knows; deleted only on opt-in."""
        return self._in_state(ORPHANED)

    @property
    def unrecognised(self) -> tuple[PruneCandidate, ...]:
        """Version-shaped directories with no matching stamp; never deleted."""
        return self._in_state(UNRECOGNISED)

    @property
    def removed(self) -> tuple[PruneCandidate, ...]:
        """Version directories this run deleted."""
        return self._in_state(REMOVED)

    @property
    def problems(self) -> tuple[PruneCandidate, ...]:
        """Delete targets that could not be removed or were refused at act time."""
        return self._in_state(REMOVE_FAILED, REFUSED)

    def reclaimable(self, *states: str) -> int:
        """Total bytes held by the version directories in the given states."""
        return sum(c.size for c in self.candidates if c.state in states)

    @property
    def ok(self) -> bool:
        """True when the reference set was complete and no removal failed or was refused."""
        return (
            not self.blocked
            and self.scan_error is None
            and self.lock_problem is None
            and not self.problems
            and self.apply_refusal is None
        )

    def deletion_refusal(
        self, *, include_orphans: bool, allow_empty_reference_set: bool = False
    ) -> str | None:
        """Why deletion may not proceed, or ``None`` if it may.

        Checked in order: an incomplete reference set, an unreadable tree, a
        held or untestable fetch lock, and, only when orphans are in scope, an empty
        reference set without the explicit override. Every one of these makes
        "not referenced" indistinguishable from "not yet proven referenced",
        so each blocks deletion on its own.
        """
        if self.blocked:
            return (
                'the reference set is incomplete '
                '(a manifest did not load or a version dir was unresolvable)'
            )
        if self.scan_error is not None:
            return f'the data root cannot be fully read ({self.scan_error})'
        if self.lock_problem is not None:
            return self.lock_problem
        if include_orphans and self.empty_reference_set and not allow_empty_reference_set:
            return (
                'no installed manifest declares any dataset; '
                'pass the override to prune orphans anyway'
            )
        return None

    def summary(self) -> str:
        """A short report: one line per version directory plus per-category totals."""
        lines: list[str] = []
        if self.scan_error is not None:
            lines.append(f'DATA ROOT UNREADABLE, {self.scan_error}')
        for c in sorted(self.candidates, key=lambda c: c.rel):
            lines.append(c.summary())
        for provider, error in sorted(self.manifest_errors.items()):
            lines.append(f'{provider}: MANIFEST UNREADABLE, {error}')
        if self.resolve_error is not None:
            lines.append(f'VERSION DIR UNRESOLVABLE, {self.resolve_error}')
        kept = len(self.referenced)
        sup, orph, unrec = len(self.superseded), len(self.orphaned), len(self.unrecognised)
        gone, bad = len(self.removed), len(self.problems)
        closing = (
            f'{kept} referenced kept; '
            f'{sup} superseded ({_human_bytes(self.reclaimable(SUPERSEDED))}); '
            f'{orph} orphaned ({_human_bytes(self.reclaimable(ORPHANED))}); '
            f'{unrec} unrecognised ({_human_bytes(self.reclaimable(UNRECOGNISED))})'
        )
        if gone or bad:
            closing += f'; {gone} removed, {bad} not removed'
        if self.blocked:
            # The reason deletion is refused: a version listed superseded above
            # could be the referenced one whose pin this run failed to read.
            closing += (
                '; reference set is INCOMPLETE (a manifest did not load or a '
                'version dir was unresolvable), so nothing can be deleted'
            )
        if self.lock_problem is not None:
            closing += f'; {self.lock_problem}, so nothing can be deleted'
        if self.apply_refusal is not None:
            closing += f'; deletion was refused at apply time ({self.apply_refusal})'
        lines.append(closing)
        lines.extend(f'WARNING: {warning}' for warning in self.lock_warnings)
        if self.staged_remnants:
            lines.append(
                f'{len(self.staged_remnants)} partly deleted version dir(s) left in '
                f'{self.staged_remnants[0].parent} by an earlier prune; delete them by hand'
            )
        return '\n'.join(lines)


def _human_bytes(count: int) -> str:
    """Render a byte count as a short human-readable size."""
    size = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{count} B'


def _dir_size(directory: Path) -> int:
    """Sum the sizes of the regular files under ``directory``, symlinks excluded.

    A symlink is measured as the link, not its target, so a link pointing out
    of the tree cannot inflate the figure or reach a file the delete would not
    touch. Anything unreadable is skipped rather than raised: the size is a
    report figure, not a decision the deletion depends on.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [d for d in dirnames if _is_plain_dir(Path(dirpath) / d)]
        for name in filenames:
            try:
                st = os.lstat(Path(dirpath) / name)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def _is_plain_dir(path: Path) -> bool:
    """True when ``path`` is a directory and not a symlink; False when it cannot be read."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _same_dir(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` name the same directory on disk.

    Uses ``os.path.samefile`` so the comparison is by filesystem identity
    (device and inode), not by path string: on a case-insensitive filesystem
    two differently-cased spellings of the same directory compare equal here,
    where a resolved-path string comparison would not. Falls back to resolved
    path equality when either side cannot be stat'd (already removed).
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        try:
            return a.resolve() == b.resolve()
        except (OSError, RuntimeError):
            return False


def _existing_root(data_root: str | Path | None) -> Path:
    """The data root, which must already exist; this never creates it.

    Raises
    ------
    FileNotFoundError
        When the resolved root is not an existing directory, so a mistyped
        root fails clearly rather than reading as an empty tree.
    """
    root = resolve_data_root(data_root, create=False)
    if not root.is_dir():
        raise FileNotFoundError(f'data root {root} does not exist; nothing to prune')
    return root


def _same_entry(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are the same directory entry, symlinks not followed."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _fs_is_case_insensitive(root: Path, *, may_write: bool = False) -> bool:
    """True when the filesystem holding ``root``'s entries treats case as insignificant.

    The probe looks inside ``root``, since ``root``'s own name lives on its
    parent's filesystem. A deleting run (``may_write``) always creates a fresh
    probe file in ``root``, looks it up under the flipped spelling and removes
    it again. A dry run touches nothing: it looks up a directory whose name has
    cased letters under the flipped spelling (only a directory, since a file
    could be a hard link) and answers False when there is none. Any failure
    answers False, which only makes subdir matching stricter.
    """
    if may_write:
        return _probe_case_with_a_file(root)
    try:
        with os.scandir(root) as entries:
            name = next(
                (
                    e.name
                    for e in entries
                    if e.name.swapcase() != e.name and e.is_dir(follow_symlinks=False)
                ),
                None,
            )
    except OSError:
        return False
    if name is not None:
        return _same_entry(root / name, root / name.swapcase())
    return False


def _probe_case_with_a_file(root: Path) -> bool:
    """Create a probe file in ``root``, look it up under the flipped case, remove it."""
    try:
        fd, probe_name = tempfile.mkstemp(dir=root, prefix='.fwl-io-case-probe-')
    except OSError:
        return False
    os.close(fd)
    probe = Path(probe_name)
    try:
        return _same_entry(probe, probe.with_name(probe.name.swapcase()))
    finally:
        probe.unlink(missing_ok=True)


def _matches_subdir(parent_subdir: str, known_subdirs: set[str], case_insensitive: bool) -> bool:
    """True when ``parent_subdir`` names a subdir a manifest declares."""
    if parent_subdir in known_subdirs:
        return True
    if not case_insensitive:
        return False
    folded = parent_subdir.casefold()
    return any(folded == known.casefold() for known in known_subdirs)


def _shadows_known_subdir(rel: str, known_subdirs: set[str], case_insensitive: bool) -> bool:
    """True when ``rel`` is, or sits above, a subdir a dataset declares.

    A directory matching the version-directory name shape is still a real
    dataset subdir segment, not a version directory, when a declared subdir is
    ``rel`` itself or continues below it. Descending into it lets the actual
    version directory further down be found, rather than hidden behind a
    match on the segment above it.
    """
    if case_insensitive:
        rel = rel.casefold()
        known_subdirs = {k.casefold() for k in known_subdirs}
    if rel in known_subdirs:
        return True
    prefix = rel + '/'
    return any(known.startswith(prefix) for known in known_subdirs)


def _read_stamp_safely(directory: Path) -> dict | None:
    """The fetcher's stamp record in ``directory``, or ``None`` when it cannot vouch.

    Only a regular file up to ``_MAX_STAMP_BYTES`` is read, so a symlinked
    stamp never vouches for the directory it sits in, and JSON nested deeply
    enough to exhaust the parser counts as no stamp rather than raising.
    """
    try:
        st = os.lstat(directory / _STAMP_FILENAME)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_STAMP_BYTES:
        return None
    try:
        return Fetcher._read_stamp(directory)
    except RecursionError:
        return None


def _has_matching_stamp(path: Path, rel_parent: str, *, case_insensitive: bool) -> bool:
    """True when ``path`` carries the stamp a fetch wrote into this very directory.

    The stamp must name the directory's own record id and, as its subdir,
    ``rel_parent``: the directory's parent relative to the data root. A copy
    of a fetched version elsewhere in the tree keeps its stamp, whose subdir
    then names somewhere else, so the stamp does not vouch for the copy.
    """
    stamp = _read_stamp_safely(path)
    if stamp is None or stamp.get('record_id') != path.name.removeprefix('r'):
        return False
    subdir = stamp.get('subdir')
    if not isinstance(subdir, str):
        return False
    subdir = PurePosixPath(subdir).as_posix()
    if case_insensitive:
        return subdir.casefold() == rel_parent.casefold()
    return subdir == rel_parent


def _leaf_problem(path: Path) -> str | None:
    """Why ``path`` is not a plain leaf version directory, or ``None`` if it is.

    A version directory is a leaf: nothing a manifest declares lives inside
    one. A candidate holding a further ``r<digits>`` entry or a stamp file below
    its own top level is a subtree that also matches the version-name shape,
    and removing it could take a live pin nested inside. A subdirectory on
    another filesystem is a mount that ``rmtree`` would empty. Names are checked
    before descending, and anything unreadable fails closed.
    """
    unreadable = False

    def _fail(_exc: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    try:
        device = os.lstat(path).st_dev
    except OSError:
        return 'cannot be fully read'
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False, onerror=_fail):
        current = Path(dirpath)
        if current != path and _STAMP_FILENAME in filenames:
            return 'contains a nested stamp'
        if any(_VERSION_DIR_PATTERN.fullmatch(d) for d in dirnames):
            return 'contains a nested version'
        keep = []
        for name in dirnames:
            try:
                st = os.lstat(current / name)
            except OSError:
                return 'cannot be fully read'
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_dev != device:
                return 'contains a mount point'
            keep.append(name)
        dirnames[:] = keep
    return 'cannot be fully read' if unreadable else None


#: The consequence every lock warning names.
_UNLOCKED_FETCH = 'runs without a lock, which prune cannot see; do not fetch while prune runs'


def _missing_access(path: Path) -> str | None:
    """Name the permission this user lacks on the directory ``path``, or ``None``."""
    writable, searchable = os.access(path, os.W_OK), os.access(path, os.X_OK)
    if writable and searchable:
        return None
    if not writable and not searchable:
        return 'writable or searchable'
    return 'searchable' if writable else 'writable'


def _lock_scan(root: Path, *, with_warnings: bool = True) -> tuple[str | None, tuple[str, ...]]:
    """Probe every fetch lock under ``root``: why deletion must wait, and warnings.

    A lock file name is an opaque hash of the path it guards, so the whole
    lock directory is checked. Each regular lock file is opened read-only
    without following symlinks and probed with a shared, non-blocking flock,
    which conflicts with the exclusive flock a fetch holds; nothing is created,
    truncated or written. A lock entry that is not a regular file is not
    probed, so its state is unknown and it blocks under its own reason, as do
    a lock directory that is a symlink or not a directory and a lock file that
    cannot be probed. A lock file, the lock directory or (with no lock
    directory yet) the data root that this user cannot write does not block,
    so a tree shared with other users stays usable, but is named in the
    warnings: this user's own fetch through it would run without a lock.
    ``with_warnings=False`` skips those access checks, for the probe before
    each removal, which only needs the reason.

    Returns
    -------
    tuple
        ``(problem, warnings)``: the reason deletion must wait, or ``None``,
        and the warning lines for the report.
    """
    lock_dir = root / _LOCK_DIRNAME
    warnings: list[str] = []
    unwritable = 0

    def _result(problem: str | None) -> tuple[str | None, tuple[str, ...]]:
        if unwritable:
            warnings.append(
                f'{unwritable} lock file(s) are not writable by this user; '
                f'a fetch by this user through them {_UNLOCKED_FETCH}'
            )
        return problem, tuple(warnings)

    try:
        st = os.lstat(lock_dir)
    except FileNotFoundError:
        if with_warnings and not os.access(root, os.W_OK | os.X_OK):
            warnings.append(
                f'{root} is not writable by this user, so a fetch by this user cannot create '
                f'{_LOCK_DIRNAME} and {_UNLOCKED_FETCH}'
            )
        return _result(None)
    except OSError as exc:
        return _result(f'cannot read {lock_dir}: {exc}')
    if stat.S_ISLNK(st.st_mode):
        return _result(f'{lock_dir} is a symlink; fetch locks cannot be checked')
    if not stat.S_ISDIR(st.st_mode):
        # A fetch then runs without a lock, so its state cannot be seen here.
        return _result(f'{lock_dir} is not a directory; fetch locks cannot be checked')
    if fcntl is None or not hasattr(os, 'O_NOFOLLOW'):
        return _result('fetch locks cannot be checked on this platform')
    try:
        with os.scandir(lock_dir) as entries:
            names = sorted(e.name for e in entries if e.name.endswith('.lock'))
    except OSError as exc:
        return _result(f'cannot read {lock_dir}: {exc}')
    missing = _missing_access(lock_dir) if with_warnings else None
    if missing is not None:
        warnings.append(
            f'{lock_dir} is not {missing} by this user; '
            f'a fetch by this user whose lock file does not exist yet {_UNLOCKED_FETCH}'
        )
    for name in names:
        path = lock_dir / name
        try:
            entry = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return _result(f'cannot test lock file {path}: {exc}')
        if not stat.S_ISREG(entry.st_mode):
            return _result(f'lock file {path} is not a regular file; fetch locks cannot be checked')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return _result(f'cannot test lock file {path}: {exc}')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _result('a fetch lock is held on the data root')
        except OSError as exc:
            return _result(f'cannot test lock file {path}: {exc}')
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        if with_warnings and not os.access(path, os.W_OK):
            unwritable += 1
    return _result(None)


def _lock_problem(root: Path) -> str | None:
    """Why deletion must wait for a fetch lock, or ``None``; the reason only, no warnings."""
    return _lock_scan(root, with_warnings=False)[0]


#: Symlinks followed while tracing one link before giving up, the usual kernel limit.
_MAX_SYMLINKS = 40


class _WalkEnds(Exception):
    """Raised inside :func:`_symlink_hops` where the kernel would stop resolving."""


def _symlink_hops(link: Path) -> set[Path]:
    """Every path the kernel visits while resolving ``link``.

    The link text is walked one name at a time, as the kernel does, and each
    name is recorded before any symlink it names is followed. A chain that
    passes through a candidate, through a symlinked directory inside one, or
    by way of ``..``, therefore names the candidate itself. Like the kernel,
    the walk ends at the first name that is missing or that would have to be
    walked through while it is not a directory; the names recorded before it
    are kept, and nothing after it is reached.

    Raises
    ------
    OSError
        When the link cannot be read or more than ``_MAX_SYMLINKS`` links are
        met, which includes a loop.
    """
    hops: set[Path] = set()
    budget = _MAX_SYMLINKS

    def _require_dir(path: Path) -> None:
        try:
            is_dir = stat.S_ISDIR(os.lstat(path).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            is_dir = False
        if not is_dir:
            raise _WalkEnds

    def _visit(text: str, base: Path) -> Path:
        nonlocal budget
        target = Path(text)
        current = Path(target.anchor) if target.is_absolute() else base
        for part in target.parts[1:] if target.is_absolute() else target.parts:
            _require_dir(current)
            if part == '.':
                continue
            if part == '..':
                current = current.parent
                continue
            step = current / part
            hops.add(step)
            try:
                is_link = stat.S_ISLNK(os.lstat(step).st_mode)
            except (FileNotFoundError, NotADirectoryError):
                raise _WalkEnds from None
            if not is_link:
                current = step
                continue
            budget -= 1
            if budget < 0:
                raise OSError(errno.ELOOP, 'too many levels of symbolic links', str(link))
            current = _visit(os.readlink(step), current)
        return current

    try:
        _visit(os.readlink(link), link.parent)
    except _WalkEnds:
        pass
    return hops


def _hop_identities(hops: set[Path]) -> frozenset[tuple[int, int]]:
    """Device and inode of every hop and of every ancestor of one, symlinks not followed.

    A candidate is passed through by a link exactly when its own identity is
    in this set, so each candidate costs one lookup instead of a walk over
    every hop. Ancestors shared between hops are read once.
    """
    ids: set[tuple[int, int]] = set()
    seen: set[Path] = set()
    for hop in hops:
        for entry in (hop, *hop.parents):
            if entry in seen:
                break
            seen.add(entry)
            try:
                st = os.lstat(entry)
            except OSError:
                continue
            ids.add((st.st_dev, st.st_ino))
    return frozenset(ids)


def _referenced_symlink_targets(referenced: set[Path]) -> tuple[set[Path], str | None]:
    """Every hop of every symlink found inside a referenced directory.

    Collected once so ``_remove_one`` can refuse a candidate that a current
    pin's own links pass through, rather than walking every referenced
    directory again for each candidate. The second value is the first error
    met, if any: an unreadable part of a referenced directory, or a link that
    cannot be traced, may lead into any candidate, so the caller must not delete.
    """
    targets: set[Path] = set()
    error: str | None = None

    def _capture(exc: OSError) -> None:
        nonlocal error
        if error is None:
            error = str(exc)

    for ref in referenced:
        if not _is_plain_dir(ref):
            continue
        for dirpath, dirnames, filenames in os.walk(ref, followlinks=False, onerror=_capture):
            current = Path(dirpath)
            for name in (*dirnames, *filenames):
                candidate = current / name
                try:
                    if not stat.S_ISLNK(os.lstat(candidate).st_mode):
                        continue
                    targets |= _symlink_hops(candidate)
                except OSError as exc:
                    _capture(exc)
    return targets, error


@dataclass(frozen=True)
class _Build:
    """Everything one classification pass computed, kept together for reuse."""

    candidates: tuple[PruneCandidate, ...]
    referenced: set[Path]
    known_subdirs: frozenset[str]
    manifest_errors: dict[str, str]
    resolve_error: str | None
    scan_error: str | None
    lock_problem: str | None
    lock_warnings: tuple[str, ...]
    referenced_link_ids: frozenset[tuple[int, int]]
    case_insensitive: bool
    staged_remnants: tuple[Path, ...]

    def report(self) -> PruneReport:
        """The plan as a :class:`PruneReport`, without acting on anything."""
        return PruneReport(
            self.candidates,
            self.manifest_errors,
            self.resolve_error,
            self.scan_error,
            self.known_subdirs,
            self.lock_problem,
            staged_remnants=self.staged_remnants,
            lock_warnings=self.lock_warnings,
        )


def _reference_set(root: Path) -> tuple[set[Path], set[str], dict[str, str], str | None]:
    """Compute the version directories in use and the subdirs datasets declare.

    Returns
    -------
    tuple
        ``(referenced, known_subdirs, manifest_errors, resolve_error)``.
        ``referenced`` holds the resolved path of every pinned version
        directory. ``known_subdirs`` holds the posix subdir of every dataset,
        pinned or not, used to tell an older pin of a known dataset apart from
        a directory no manifest references. Either error field being set means
        the reference set is incomplete.
    """
    from fwl_io.manifest import _discover

    providers, manifest_errors = _discover()
    referenced: set[Path] = set()
    known_subdirs: set[str] = set()
    resolve_error: str | None = None
    for provider_datasets in providers.values():
        for ds in provider_datasets:
            known_subdirs.add(ds.subdir)
            if ds.zenodo is None:
                continue
            try:
                referenced.add((root / _version_dir(ds)).resolve())
            except Exception as exc:  # noqa: BLE001 -- reported as a block, never raised
                # One unresolvable pin makes the whole reference set a subset of
                # the truth, so the run must refuse to delete; record it and stop.
                resolve_error = f'{ds.key}: {exc}'
                return referenced, known_subdirs, dict(manifest_errors), resolve_error
    return referenced, known_subdirs, dict(manifest_errors), resolve_error


def _scan(
    root: Path, known_subdirs: set[str], *, case_insensitive: bool
) -> tuple[list[Path], str | None]:
    """Find every ``r<record-id>`` directory under ``root``, symlinks excluded.

    A reserved fwl-io directory and its contents are never entered, a symlinked
    directory is never followed or reported, and a version directory's own
    contents are not descended into: a name inside a dataset is not a version
    directory of the tree.

    A directory whose name matches the version shape but whose path is itself
    a declared dataset subdir, or sits above one, is a subdir segment, not a
    version directory: a dataset key may end in a segment like ``r1000``, and
    a manifest may declare a subdir two levels below that segment. Such a
    directory is descended into so the real version directory below it is
    found, rather than matched and skipped, which would hide it.

    A directory the walk cannot read (a permission error on a shared tree) is
    recorded as a scan error so the caller refuses to delete, rather than
    reporting a partial tree as clean.
    """
    found: list[Path] = []
    scan_error: str | None = None

    def _capture(exc: OSError) -> None:
        nonlocal scan_error
        if scan_error is None:
            scan_error = str(exc)

    for dirpath, dirnames, _ in os.walk(root, followlinks=False, onerror=_capture):
        keep: list[str] = []
        for name in dirnames:
            child = Path(dirpath) / name
            try:
                if stat.S_ISLNK(os.lstat(child).st_mode):
                    continue
            except OSError as exc:
                _capture(exc)
                continue
            if name in _RESERVED_DIRNAMES:
                continue
            rel = child.relative_to(root).as_posix()
            if _VERSION_DIR_PATTERN.fullmatch(name) and not _shadows_known_subdir(
                rel, known_subdirs, case_insensitive
            ):
                found.append(child)
                continue
            keep.append(name)
        dirnames[:] = keep
    return found, scan_error


def _staged_remnants(root: Path) -> tuple[Path, ...]:
    """Leftovers of removals that failed part way, which the scan never enters."""
    staging = root / _STAGING_DIRNAME
    try:
        return tuple(sorted(p for p in staging.glob('prune-*') if _is_plain_dir(p)))
    except OSError:
        return ()


def _classify(
    parent_subdir: str,
    is_referenced: bool,
    known_subdirs: set[str],
    blocked: bool,
    *,
    case_insensitive: bool,
) -> str:
    """Decide a version directory's provisional state from the reference set.

    This is provisional: the caller still applies the stamp and nested-content
    checks that can override a non-referenced result to ``UNRECOGNISED``.
    """
    if is_referenced:
        return REFERENCED
    if blocked:
        # The reference set is incomplete, so "not referenced" is not "proven
        # unreferenced". Nothing may be classed superseded off a partial set.
        return ORPHANED
    if _matches_subdir(parent_subdir, known_subdirs, case_insensitive):
        return SUPERSEDED
    return ORPHANED


def _build(root: Path, *, for_delete: bool = False) -> _Build:
    """Classify every version directory under ``root`` without touching anything.

    ``for_delete`` also collects the symlink targets inside referenced
    directories, which only a removal checks; a dry run skips that walk.
    """
    referenced, known_subdirs, manifest_errors, resolve_error = _reference_set(root)
    blocked = bool(manifest_errors) or resolve_error is not None
    case_insensitive = _fs_is_case_insensitive(root, may_write=for_delete)
    version_dirs, scan_error = _scan(root, known_subdirs, case_insensitive=case_insensitive)
    candidates: list[PruneCandidate] = []
    for path in version_dirs:
        rel = path.relative_to(root)
        rel_parent = rel.parent.as_posix()
        is_referenced = any(_same_dir(path, ref) for ref in referenced)
        state = _classify(
            rel_parent,
            is_referenced,
            known_subdirs,
            blocked,
            case_insensitive=case_insensitive,
        )
        if state != REFERENCED and (
            not _has_matching_stamp(path, rel_parent, case_insensitive=case_insensitive)
            or _leaf_problem(path) is not None
        ):
            state = UNRECOGNISED
        candidates.append(
            PruneCandidate(path=path, rel=rel.as_posix(), state=state, size=_dir_size(path))
        )
    lock_problem, lock_warnings = _lock_scan(root)
    symlink_targets: set[Path] = set()
    if for_delete:
        symlink_targets, symlink_error = _referenced_symlink_targets(referenced)
        if scan_error is None and symlink_error is not None:
            scan_error = f'a referenced version cannot be fully read: {symlink_error}'
    return _Build(
        candidates=tuple(candidates),
        referenced=referenced,
        known_subdirs=frozenset(known_subdirs),
        manifest_errors=manifest_errors,
        resolve_error=resolve_error,
        scan_error=scan_error,
        lock_problem=lock_problem,
        lock_warnings=lock_warnings,
        referenced_link_ids=_hop_identities(symlink_targets),
        case_insensitive=case_insensitive,
        staged_remnants=_staged_remnants(root),
    )


def plan_prune(data_root: str | Path | None = None) -> PruneReport:
    """Report which version directories would be removed, touching nothing.

    Parameters
    ----------
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.

    Returns
    -------
    PruneReport
        One entry per ``r<record-id>`` directory found, classified against the
        installed manifests, with per-category reclaimable bytes.

    Raises
    ------
    FileNotFoundError
        When the data root does not exist. A plan never creates it.
    """
    return _build(_existing_root(data_root)).report()


def _delete_unsupported() -> str | None:
    """Why this platform cannot delete safely, or ``None`` when it can.

    Deletion moves directories through handles opened without following
    symlinks and probes fetch locks with flock; a platform without these (such
    as Windows) is refused up front rather than failing part way. Checked by
    feature, not by platform name.
    """
    missing = [name for name in ('O_DIRECTORY', 'O_NOFOLLOW') if not hasattr(os, name)]
    if not _DIR_FD_OK:
        missing.append('dir_fd')
    if not _RMTREE_IS_SAFE:
        missing.append('a symlink-safe rmtree')
    if fcntl is None:
        missing.append('flock')
    if missing:
        return f'deletion is not supported on this platform (missing {", ".join(missing)})'
    return None


def _open_dir_below(root: Path, parts: tuple[str, ...]) -> int:
    """Open ``root/parts...`` as a directory, following no symlink below ``root``."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise
    return fd


def _remove_one(
    candidate: PruneCandidate,
    root: Path,
    referenced: set[Path],
    referenced_link_ids: frozenset[tuple[int, int]] = frozenset(),
    *,
    case_insensitive: bool = False,
) -> PruneCandidate:
    """Delete one version directory, re-checking every guard at the last moment.

    This refuses a symlink or a path outside the data root, opens the
    candidate's parent from the root without following any symlink, and hands
    over to :func:`_remove_checked`, which repeats the remaining guards and
    does the move and the deletion. ``referenced_link_ids`` holds the identities from
    :func:`_hop_identities` for the links inside referenced versions.
    """
    path = candidate.path

    def _refuse(detail: str) -> PruneCandidate:
        return replace(candidate, state=REFUSED, detail=detail)

    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return _refuse('is a symlink; not removed')
    except OSError as exc:
        return _refuse(f'cannot be checked ({exc}); not removed')
    if not _inside(path, root):
        return _refuse(f'resolves outside {root}; not removed')
    try:
        rel = path.relative_to(root)
    except ValueError:
        return _refuse(f'is not spelled below {root}; not removed')
    try:
        parent_fd = _open_dir_below(root, rel.parent.parts)
    except OSError as exc:
        return _refuse(f'its parent cannot be opened without symlinks ({exc}); not removed')
    try:
        return _remove_checked(
            candidate,
            root,
            rel,
            parent_fd,
            referenced,
            referenced_link_ids,
            case_insensitive=case_insensitive,
        )
    finally:
        os.close(parent_fd)


def _remove_checked(
    candidate: PruneCandidate,
    root: Path,
    rel: Path,
    parent_fd: int,
    referenced: set[Path],
    referenced_link_ids: frozenset[tuple[int, int]],
    *,
    case_insensitive: bool,
) -> PruneCandidate:
    """Repeat every guard on one candidate, then move it into staging and delete it.

    The filesystem, not-referenced, stamp, leaf, link and fetch-lock checks
    run here rather than being trusted from the plan; the caller checked
    containment and opened ``parent_fd``. Right
    before the move, the candidate and its parent are checked once more to be
    the entries the checks read, and the moved entry is compared by device
    and inode after the move and put back if it differs. The move into the
    staging directory is atomic, so a deletion that fails part way leaves its
    remnant in staging, named in the result, and never a stampless tree at
    the old path.

    This assumes that no other process renames or replaces directories under
    the data root during a run; an fwl-io fetch is kept out only while it holds
    the fetch lock. The re-checks narrow, but cannot close, the window such a
    process would have.
    """
    path = candidate.path
    name = path.name

    def _refuse(detail: str) -> PruneCandidate:
        return replace(candidate, state=REFUSED, detail=detail)

    def _failed(detail: str) -> PruneCandidate:
        return replace(candidate, state=REMOVE_FAILED, detail=detail)

    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        on_other_device = before.st_dev != os.stat(root).st_dev
    except OSError as exc:
        return _refuse(f'cannot be checked ({exc}); not removed')
    if not stat.S_ISDIR(before.st_mode):
        return _refuse('is not a plain directory; not removed')
    if on_other_device or os.path.ismount(path):
        return _refuse('is a mount point or on another filesystem; not removed')
    if any(_same_dir(path, ref) for ref in referenced):
        return _refuse('is a referenced version; not removed')
    if any(_inside(ref, path) for ref in referenced):
        return _refuse('contains a referenced version; not removed')
    if not _has_matching_stamp(path, rel.parent.as_posix(), case_insensitive=case_insensitive):
        return _refuse('has no matching stamp; not removed')
    problem = _leaf_problem(path)
    if problem is not None:
        return _refuse(f'{problem}; not removed')
    if (before.st_dev, before.st_ino) in referenced_link_ids:
        return _refuse('a referenced file symlinks into this directory; not removed')
    lock_problem = _lock_problem(root)
    if lock_problem is not None:
        return _refuse(f'{lock_problem}; not removed')
    staging = root / _STAGING_DIRNAME
    staged_name = f'prune-{uuid.uuid4().hex}'
    staged = staging / staged_name
    try:
        root_fd = _open_dir_below(root, ())
        try:
            try:
                os.mkdir(_STAGING_DIRNAME, dir_fd=root_fd)
            except FileExistsError:
                pass
        finally:
            os.close(root_fd)
        staging_fd = _open_dir_below(root, (_STAGING_DIRNAME,))
    except OSError as exc:
        return _refuse(f'{staging} is not a usable plain directory ({exc}); not removed')
    try:
        if not _unchanged(path, before, parent_fd, root, rel.parent.parts):
            return _refuse('changed during prune; not removed')
        try:
            os.rename(name, staged_name, src_dir_fd=parent_fd, dst_dir_fd=staging_fd)
        except OSError as exc:
            return _failed(f'could not move it aside: {exc}')
        try:
            moved = os.stat(staged_name, dir_fd=staging_fd, follow_symlinks=False)
        except OSError as exc:
            return _failed(f'moved to {staged}, then could not be checked ({exc})')
        if (moved.st_dev, moved.st_ino) != (before.st_dev, before.st_ino):
            # A different entry took the name after the checks: put it back.
            try:
                os.rename(staged_name, name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd)
            except OSError as exc:
                return _failed(f'changed during prune; moved to {staged}, not put back ({exc})')
            return _refuse('changed during prune; not removed')
        try:
            shutil.rmtree(staged_name, dir_fd=staging_fd)
        except OSError as exc:
            try:
                os.stat(staged_name, dir_fd=staging_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as stat_exc:
                return _failed(f'moved to {staged}, deletion not confirmed ({exc}; {stat_exc})')
            else:
                return _failed(
                    f'moved to {staged} but not fully deleted ({exc}); delete it by hand'
                )
    finally:
        os.close(staging_fd)
    log.info('removed unreferenced version directory %s', path)
    tidy_problem = _prune_empty_parents(path.parent, root)
    detail = f'empty parent directories kept ({tidy_problem})' if tidy_problem else ''
    return replace(candidate, state=REMOVED, detail=detail)


def _unchanged(
    path: Path, before: os.stat_result, parent_fd: int, root: Path, parent_parts: tuple[str, ...]
) -> bool:
    """True when ``path`` and its held parent are still the entries the checks read.

    ``path`` must still name the entry ``before`` describes, and a fresh
    no-follow open of the parent from the root must reach the directory
    ``parent_fd`` holds. Anything that cannot be read answers False.
    """
    try:
        now = os.lstat(path)
        fresh_fd = _open_dir_below(root, parent_parts)
    except OSError:
        return False
    try:
        held, fresh = os.fstat(parent_fd), os.fstat(fresh_fd)
    except OSError:
        return False
    finally:
        os.close(fresh_fd)
    return (now.st_dev, now.st_ino) == (before.st_dev, before.st_ino) and (
        held.st_dev,
        held.st_ino,
    ) == (fresh.st_dev, fresh.st_ino)


def _prune_empty_parents(directory: Path, root: Path) -> str | None:
    """Remove now-empty parent directories up to, but never including, the root.

    Only ever removes a directory with nothing left in it, so a sibling version
    directory under the same subdir keeps the subdir standing, and the walk
    stops at the data root. A symlinked parent is left alone rather than
    followed out of the tree. Returns why the walk stopped at a parent that is
    a symlink or cannot be resolved (a loop, a permission error), else
    ``None``; a parent that is already gone is skipped. It never raises.
    """
    try:
        root = root.resolve(strict=True)
        while True:
            if directory.is_symlink():
                return f'{directory} is a symlink'
            try:
                resolved = directory.resolve(strict=True)
            except FileNotFoundError:
                directory = directory.parent
                continue
            if resolved == root or not resolved.is_relative_to(root):
                return None
            try:
                if any(directory.iterdir()):
                    return None
                directory.rmdir()
            except OSError:
                return None
            directory = directory.parent
    except (OSError, RuntimeError) as exc:
        return f'{directory} cannot be resolved: {exc}'
    return None


def prune_versions(
    data_root: str | Path | None = None,
    *,
    delete: bool = False,
    include_orphans: bool = False,
    allow_empty_reference_set: bool = False,
) -> PruneReport:
    """Plan a prune and, when ``delete`` is set, remove the proven-unreferenced set.

    A superseded version directory is removed by default. An orphaned one is
    removed only when ``include_orphans`` is set, because it cannot be proven
    unreferenced by manifests this environment cannot see. Deletion is refused
    outright on a platform that cannot delete safely (see
    :func:`_delete_unsupported`), when the reference set is incomplete, the
    tree cannot be fully read, a fetch lock is held or cannot be checked, or
    (for an orphan-including run) the reference set is empty and the override
    was not given.

    Parameters
    ----------
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.
    delete : bool
        Remove the target directories. When False this is a dry run.
    include_orphans : bool
        Also remove orphaned version directories. Has no effect without
        ``delete``.
    allow_empty_reference_set : bool
        Permit an orphan-including run to proceed even when no installed
        manifest declares any dataset. Has no effect without ``delete`` and
        ``include_orphans``.

    Returns
    -------
    PruneReport
        The plan, with each removed directory's entry rewritten to say so.

    Raises
    ------
    FileNotFoundError
        When the data root does not exist.
    """
    root = _existing_root(data_root)
    unsupported = _delete_unsupported() if delete else None
    build = _build(root, for_delete=delete and unsupported is None)
    report = build.report()
    if unsupported is not None:
        return replace(report, apply_refusal=unsupported)
    if not delete or report.deletion_refusal(
        include_orphans=include_orphans, allow_empty_reference_set=allow_empty_reference_set
    ):
        # A dry run, or a run the reference set will not allow: report, do nothing.
        return report
    targets = {SUPERSEDED, ORPHANED} if include_orphans else {SUPERSEDED}
    results = [
        _remove_one(
            c,
            root,
            build.referenced,
            build.referenced_link_ids,
            case_insensitive=build.case_insensitive,
        )
        if c.state in targets
        else c
        for c in build.candidates
    ]
    return replace(report, candidates=tuple(results))


def apply_prune(
    report: PruneReport,
    data_root: str | Path | None = None,
    *,
    include_orphans: bool = False,
    allow_empty_reference_set: bool = False,
) -> PruneReport:
    """Delete exactly the version directories a prior plan reported.

    The plan the caller showed and confirmed is the most that is deleted. A
    directory is removed only when the plan listed it as a target (superseded,
    or orphaned when ``include_orphans`` is set) and a fresh classification
    made by this call still does. A directory the plan showed in any other
    state is kept, even when the tree changed so that it now looks like a
    target, and so is a directory a manifest started to use between plan and
    apply. Every removal also re-checks its own guards at the moment it runs.

    Deletion is refused outright on a platform that cannot delete safely
    (see :func:`_delete_unsupported`), when the current reference-set state
    no longer permits it, or when the plan lists a directory outside this
    data root.

    Parameters
    ----------
    report : PruneReport
        The plan to act on, as returned by :func:`plan_prune`.
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree. Must
        be the same root the plan was built against.
    include_orphans : bool
        Also remove the orphaned version directories in the plan.
    allow_empty_reference_set : bool
        Permit an orphan-including apply to proceed even when the current
        reference set declares no dataset.

    Returns
    -------
    PruneReport
        The plan, with each removed directory's entry rewritten to say so, and
        the reference-set error signals recomputed at apply time. ``ok`` is
        false, and ``apply_refusal`` names why, when the tree changed under
        this call into a state the plan could no longer delete from.

    Raises
    ------
    FileNotFoundError
        When the data root does not exist.
    """
    root = _existing_root(data_root)
    unsupported = _delete_unsupported()
    if unsupported is not None:
        return replace(report, apply_refusal=unsupported)
    stray = next((c for c in report.candidates if not _inside(c.path, root)), None)
    if stray is not None:
        return replace(
            report,
            apply_refusal=(
                f'the plan lists {stray.path}, outside {root}; it was built for another root'
            ),
        )
    build = _build(root, for_delete=True)
    current = build.report()
    refusal = current.deletion_refusal(
        include_orphans=include_orphans, allow_empty_reference_set=allow_empty_reference_set
    )
    if refusal:
        # The tree changed under us into a state the plan could not delete from.
        return replace(current, candidates=report.candidates, apply_refusal=refusal)
    targets = {SUPERSEDED, ORPHANED} if include_orphans else {SUPERSEDED}
    # A candidate must be a target both in the plan the caller confirmed and
    # in this call's fresh build: the plan bounds what may go, and the fresh
    # state catches a directory that stopped being safe to remove since.
    fresh_by_dir = {fresh.path: fresh for fresh in build.candidates}
    results = []
    for c in report.candidates:
        fresh = fresh_by_dir.get(c.path)
        if fresh is None:
            fresh = next((f for f in build.candidates if _same_entry(f.path, c.path)), None)
        if fresh is None:
            if not os.path.lexists(c.path):
                results.append(replace(c, state=GONE, size=0, detail='no longer on disk'))
            elif c.state in targets:
                detail = 'no longer a version dir; not removed'
                results.append(replace(c, state=REFUSED, detail=detail))
            else:
                results.append(c)
            continue
        if c.state not in targets:
            results.append(fresh)
            continue
        if fresh.state not in targets:
            # Confirmed for deletion but no longer a target: kept, and not a clean run.
            detail = f'now {fresh.state}; not removed'
            results.append(replace(fresh, state=REFUSED, detail=detail))
            continue
        results.append(
            _remove_one(
                fresh,
                root,
                build.referenced,
                build.referenced_link_ids,
                case_insensitive=build.case_insensitive,
            )
        )
    return replace(current, candidates=tuple(results))
