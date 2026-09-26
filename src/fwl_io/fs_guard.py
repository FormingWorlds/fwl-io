"""Filesystem-safety primitives shared by :mod:`fwl_io.prune` and :mod:`fwl_io.relocate`.

Nothing here knows about manifests, datasets, or the version-directory naming
scheme. Every function answers a question about what is actually on disk right
now: is this a plain directory, does it sit inside another one, is a fetch
lock held, does a symlink chain pass through it, can this platform delete or
move things safely at all. The domain logic that decides *which* directories
to touch, and why, lives in the modules that call these.
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
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no flock, so locks cannot be probed and deletion is refused
    fcntl = None

log = logging.getLogger('fwl.' + __name__)

#: Whether this Python's rmtree resists symlink swaps, read once at import.
_RMTREE_IS_SAFE = shutil.rmtree.avoids_symlink_attacks

#: Whether directory-relative open, rename and stat exist here, read once at import.
_DIR_FD_OK = {os.open, os.rename, os.stat, os.mkdir} <= os.supports_dir_fd

#: Whether ``os.stat`` can leave a final symlink unfollowed, read once at import.
_NOFOLLOW_STAT_OK = os.stat in os.supports_follow_symlinks


def _is_plain_dir(path: Path) -> bool:
    """True when ``path`` is a directory and not a symlink; False when it cannot be read."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_regular_file(path: Path) -> bool:
    """True when ``path`` is a plain file; False when a component of it is absent.

    Raises
    ------
    OSError
        When ``path`` cannot be stat'd for any other reason, most often a
        permission error on a directory above it. ``Path.stat()`` always
        raises for that rather than swallowing the error, unlike
        ``Path.is_file()``, whose own error handling has changed between
        Python versions; going through ``stat()`` here keeps "absent" and
        "present but unreadable" told apart the same way on every version.
    """
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(st.st_mode)


def _is_directory(path: Path) -> bool:
    """True when ``path`` is a directory, symlinks followed; False when a component is absent.

    Raises
    ------
    OSError
        When ``path`` cannot be stat'd for any other reason, most often a
        permission error on a directory above it, for the same reason as
        :func:`_is_regular_file`.
    """
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISDIR(st.st_mode)


def _dir_size(directory: Path) -> int:
    """Sum the sizes of the regular files under ``directory``, symlinks excluded.

    A symlink is measured as the link, not its target, so a link pointing out
    of the tree cannot inflate the figure or reach a file a caller would not
    touch. Anything unreadable is skipped rather than raised: this is a report
    figure, not a decision anything depends on.
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


def _same_entry(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are the same directory entry, symlinks not followed."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


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


def _fs_is_case_insensitive(root: Path, *, may_write: bool = False) -> bool:
    """True when the filesystem holding ``root``'s entries treats case as insignificant.

    The probe looks inside ``root``, since ``root``'s own name lives on its
    parent's filesystem. A caller allowed to write (``may_write``) always
    creates a fresh probe file in ``root``, looks it up under the flipped
    spelling and removes it again. A read-only caller touches nothing: it
    looks up a directory whose name has cased letters under the flipped
    spelling (only a directory, since a file could be a hard link) and
    answers False when there is none. Any failure answers False, which only
    makes subdir matching stricter.
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


def _leaf_problem(
    path: Path, *, stamp_filename: str, version_pattern: re.Pattern[str]
) -> str | None:
    """Why ``path`` is not a plain leaf directory, or ``None`` if it is.

    A leaf holds nothing a caller would need to descend into further: no
    stamp file below its own top level (``stamp_filename``), no nested entry
    matching ``version_pattern`` (a further version directory this call did
    not expect), and no subdirectory on another filesystem, which ``rmtree``
    or a move would otherwise empty. Names are checked before descending, and
    anything unreadable fails closed.
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
        if current != path and stamp_filename in filenames:
            return 'contains a nested stamp'
        if any(version_pattern.fullmatch(d) for d in dirnames):
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


def _missing_access(path: Path) -> str | None:
    """Name the permission this user lacks on the directory ``path``, or ``None``."""
    writable, searchable = os.access(path, os.W_OK), os.access(path, os.X_OK)
    if writable and searchable:
        return None
    if not writable and not searchable:
        return 'writable or searchable'
    return 'searchable' if writable else 'writable'


def _lock_scan(
    root: Path, *, lock_dirname: str, operation: str = 'deletion', with_warnings: bool = True
) -> tuple[str | None, tuple[str, ...]]:
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
    each removal, which only needs the reason. ``operation`` names the caller
    in the warnings that a fetch runs without a lock the caller cannot see.

    Returns
    -------
    tuple
        ``(problem, warnings)``: the reason deletion must wait, or ``None``,
        and the warning lines for the report.
    """
    lock_dir = root / lock_dirname
    unlocked = f'runs without a lock, which {operation} cannot see; do not fetch while {operation} runs'
    warnings: list[str] = []
    unwritable = 0

    def _result(problem: str | None) -> tuple[str | None, tuple[str, ...]]:
        if unwritable:
            warnings.append(
                f'{unwritable} lock file(s) are not writable by this user; '
                f'a fetch by this user through them {unlocked}'
            )
        return problem, tuple(warnings)

    try:
        st = os.lstat(lock_dir)
    except FileNotFoundError:
        if with_warnings and not os.access(root, os.W_OK | os.X_OK):
            warnings.append(
                f'{root} is not writable by this user, so a fetch by this user cannot create '
                f'{lock_dirname} and {unlocked}'
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
            f'a fetch by this user whose lock file does not exist yet {unlocked}'
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
            return _result(
                f'lock file {path} is not a regular file; fetch locks cannot be checked'
            )
        try:
            fd = _open_lock_fd(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return _result(f'cannot test lock file {path}: {exc}')
        try:
            _try_lock_shared(fd)
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


def _open_lock_fd(path: Path) -> int:
    """Open ``path`` read-only, without following a symlink, for a lock probe."""
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


def _try_lock_shared(fd: int) -> None:
    """Probe ``fd`` with a shared, non-blocking flock; raises when it is held exclusively."""
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)


def _lock_problem(root: Path, *, lock_dirname: str) -> str | None:
    """Why deletion must wait for a fetch lock, or ``None``; the reason only, no warnings."""
    return _lock_scan(root, lock_dirname=lock_dirname, with_warnings=False)[0]


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
    """Every hop of every symlink found inside one of ``referenced``.

    Collected once so a caller can refuse a candidate that a current pin's own
    links pass through, rather than walking every referenced directory again
    for each candidate. The second value is the first error met, if any: an
    unreadable part of a referenced directory, or a link that cannot be
    traced, may lead into any candidate, so the caller must not delete or move.
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


def _inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves within ``root``, symlinks followed.

    Walks up from the resolved path by filesystem identity (device and
    inode), not by comparing path strings, so a case-insensitive filesystem's
    alternate spelling of ``root`` or one of its ancestors still matches. A
    component that does not exist yet (the target side of a move that has
    not happened) is skipped up to the nearest ancestor that does exist.
    """
    try:
        current = path.resolve()
        root_stat = os.stat(root.resolve())
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python < 3.13
        return False
    while True:
        try:
            current_stat = os.stat(current)
        except OSError:
            parent = current.parent
            if parent == current:
                return False
            current = parent
            continue
        if (current_stat.st_dev, current_stat.st_ino) == (root_stat.st_dev, root_stat.st_ino):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _open_dir_below(root: Path, parts: tuple[str, ...]) -> int:
    """Open ``root/parts...`` as a directory, following no symlink below ``root``."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _probe_dir_below(root: Path, parts: tuple[str, ...]) -> None:
    """Walk ``root/parts...`` as far as it exists, following no symlink below ``root``.

    Raises
    ------
    OSError
        When ``root`` is absent, or a component that exists is a symlink, is
        not a directory or cannot be opened. A component below ``root`` that
        is absent ends the walk: the move creates it.
    """
    os.close(os.open(root, os.O_RDONLY | os.O_DIRECTORY))
    with contextlib.suppress(FileNotFoundError):
        os.close(_open_dir_below(root, parts))


def _open_or_make_dir_below(root: Path, parts: tuple[str, ...]) -> int:
    """Open ``root/parts...``, creating any missing directory, following no symlink below ``root``.

    Each level is created only when absent, so an existing symlink at that
    name is never overwritten; the following no-follow open then refuses it
    the same way :func:`_open_dir_below` does.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _platform_gap(*, operation: str, needs_locks: bool) -> str | None:
    """Why this platform cannot do ``operation`` safely, or ``None`` when it can.

    Both deletion and relocation move directories through handles opened
    without following symlinks, which needs ``O_DIRECTORY``, ``O_NOFOLLOW`` and
    directory-relative open, stat, mkdir and rename, and a stat that does not
    follow a final symlink (``os.replace`` shares rename's ``dir_fd`` support
    but is not listed by ``os.supports_dir_fd``).
    Deletion also needs a symlink-safe ``shutil.rmtree`` and flock for the
    fetch-lock probe (``needs_locks``); a move uses neither. Checked by feature,
    not by platform name.
    """
    missing = [name for name in ('O_DIRECTORY', 'O_NOFOLLOW') if not hasattr(os, name)]
    if not _DIR_FD_OK:
        missing.append('dir_fd')
    if not _NOFOLLOW_STAT_OK:
        missing.append('a no-follow stat')
    if needs_locks and not _RMTREE_IS_SAFE:
        missing.append('a symlink-safe rmtree')
    if needs_locks and fcntl is None:
        missing.append('flock')
    if missing:
        return f'{operation} is not supported on this platform (missing {", ".join(missing)})'
    return None


def _delete_unsupported() -> str | None:
    """Why this platform cannot delete safely, or ``None`` when it can."""
    return _platform_gap(operation='deletion', needs_locks=True)
