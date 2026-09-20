"""Remove versioned dataset directories that no installed manifest references.

A manifest dataset lives at ``<data_root>/<subdir>/r<record-id>``, where the
record id is the Zenodo version the manifest pins. When a pin advances the
fetcher writes a new ``r<record-id>`` beside the old one, and the old one stays
on disk for as long as the tree exists. This finds those left-behind version
directories and, only when asked, deletes them.

Nothing is deleted on trust. A version directory is removed only when it can be
proven unreferenced: it must sit under a subdirectory that a currently declared
dataset uses (an older pin of a known dataset), and the reference set it is
checked against must be complete. If any installed manifest fails to load, or
any dataset's version directory cannot be computed, the reference set is a
subset of the truth, so no directory can be proven unreferenced and the run
deletes nothing.

A version directory under a subdirectory no installed manifest knows is
reported as orphaned rather than removed. On a shared data tree it may be the
pinned version for a manifest installed in another environment, which this
process cannot see, so it is never deleted without an explicit opt-in.

The default is a dry run: the plan is printed and nothing is touched. Deletion
needs an explicit request and, unless suppressed, an interactive confirmation.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from fwl_io.fetch import _LOCK_DIRNAME, _STAGING_DIRNAME
from fwl_io.paths import resolve_data_root
from fwl_io.relocate import _inside, _version_dir

if TYPE_CHECKING:
    pass

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
REMOVED = 'removed'
REMOVE_FAILED = 'remove-failed'
REFUSED = 'refused'

#: Printed before any deletion that includes a superseded version directory.
SHARED_TREE_WARNING = (
    'WARNING: FWL_DATA is often a single tree shared between several Python '
    'environments. A version listed as superseded here is superseded only for '
    'the manifests installed in THIS environment. It may still be the pinned '
    'version for another environment on this machine, and deleting it will '
    'break that environment. Continue only if you know no other environment '
    'relies on these versions.'
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

    def _in_state(self, *states: str) -> tuple[PruneCandidate, ...]:
        return tuple(c for c in self.candidates if c.state in states)

    @property
    def blocked(self) -> bool:
        """True when the reference set is incomplete, so nothing may be deleted."""
        return bool(self.manifest_errors) or self.resolve_error is not None

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
        """True when the reference set was complete and no removal failed."""
        return not self.blocked and self.scan_error is None and not self.problems

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
        sup, orph = len(self.superseded), len(self.orphaned)
        gone, bad = len(self.removed), len(self.problems)
        closing = (
            f'{kept} referenced kept; '
            f'{sup} superseded ({_human_bytes(self.reclaimable(SUPERSEDED))}); '
            f'{orph} orphaned ({_human_bytes(self.reclaimable(ORPHANED))})'
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
        lines.append(closing)
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
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


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


def _scan(root: Path) -> tuple[list[Path], str | None]:
    """Find every ``r<record-id>`` directory under ``root``, symlinks excluded.

    A reserved fwl-io directory and its contents are never entered, a symlinked
    directory is never followed or reported, and a version directory's own
    contents are not descended into: a name inside a dataset is not a version
    directory of the tree.
    """
    found: list[Path] = []
    try:
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            keep: list[str] = []
            for name in dirnames:
                child = Path(dirpath) / name
                if child.is_symlink():
                    continue
                if name in _RESERVED_DIRNAMES:
                    continue
                if _VERSION_DIR_PATTERN.fullmatch(name):
                    found.append(child)
                    continue
                keep.append(name)
            dirnames[:] = keep
    except OSError as exc:
        return found, str(exc)
    return found, None


def _classify(
    parent_subdir: str, is_referenced: bool, known_subdirs: set[str], blocked: bool
) -> str:
    """Decide a version directory's state from the reference set."""
    if is_referenced:
        return REFERENCED
    if blocked:
        # The reference set is incomplete, so "not referenced" is not "proven
        # unreferenced". Nothing may be classed superseded off a partial set.
        return ORPHANED
    if parent_subdir in known_subdirs:
        return SUPERSEDED
    return ORPHANED


def _build(
    root: Path,
) -> tuple[list[PruneCandidate], set[Path], dict[str, str], str | None, str | None]:
    """Classify every version directory under ``root`` without touching anything.

    Returns the candidates, the resolved referenced set (needed again at delete
    time for the last-moment not-referenced check), and the three error signals.
    """
    referenced, known_subdirs, manifest_errors, resolve_error = _reference_set(root)
    blocked = bool(manifest_errors) or resolve_error is not None
    version_dirs, scan_error = _scan(root)
    candidates: list[PruneCandidate] = []
    for path in version_dirs:
        rel = path.relative_to(root)
        state = _classify(
            rel.parent.as_posix(), path.resolve() in referenced, known_subdirs, blocked
        )
        candidates.append(
            PruneCandidate(path=path, rel=rel.as_posix(), state=state, size=_dir_size(path))
        )
    return candidates, referenced, manifest_errors, resolve_error, scan_error


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
    """
    candidates, _referenced, manifest_errors, resolve_error, scan_error = _build(
        resolve_data_root(data_root)
    )
    return PruneReport(tuple(candidates), manifest_errors, resolve_error, scan_error)


def _remove_one(candidate: PruneCandidate, root: Path, referenced: set[Path]) -> PruneCandidate:
    """Delete one version directory, re-checking every guard at the last moment.

    The containment, symlink, and not-referenced checks are repeated here rather
    than trusted from the plan, so the guarantee that this only ever removes an
    unreferenced directory inside the data root belongs to the code that does
    the removing.
    """
    path = candidate.path
    if path.is_symlink():
        return replace(candidate, state=REFUSED, detail='is a symlink; not removed')
    if not _inside(path, root):
        return replace(candidate, state=REFUSED, detail=f'resolves outside {root}; not removed')
    if path.resolve() in referenced:
        return replace(candidate, state=REFUSED, detail='is a referenced version; not removed')
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return replace(candidate, state=REMOVE_FAILED, detail=str(exc))
    _prune_empty_parents(path.parent, root)
    log.info('removed unreferenced version directory %s', path)
    return replace(candidate, state=REMOVED, detail='')


def _prune_empty_parents(directory: Path, root: Path) -> None:
    """Remove now-empty parent directories up to, but never including, the root.

    Only ever removes a directory with nothing left in it, so a sibling version
    directory under the same subdir keeps the subdir standing, and the walk
    stops at the data root. A symlinked parent is left alone rather than
    followed out of the tree.
    """
    root = root.resolve()
    while directory.resolve() != root and directory.resolve().is_relative_to(root):
        try:
            if directory.is_symlink() or any(directory.iterdir()):
                return
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def prune_versions(
    data_root: str | Path | None = None,
    *,
    delete: bool = False,
    include_orphans: bool = False,
) -> PruneReport:
    """Plan a prune and, when ``delete`` is set, remove the proven-unreferenced set.

    A superseded version directory is removed by default. An orphaned one is
    removed only when ``include_orphans`` is set, because it cannot be proven
    unreferenced by manifests this environment cannot see. Deletion is refused
    outright when the reference set is incomplete.

    Parameters
    ----------
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.
    delete : bool
        Remove the target directories. When False this is a dry run.
    include_orphans : bool
        Also remove orphaned version directories. Has no effect without
        ``delete``.

    Returns
    -------
    PruneReport
        The plan, with each removed directory's entry rewritten to say so.
    """
    root = resolve_data_root(data_root)
    candidates, referenced, manifest_errors, resolve_error, scan_error = _build(root)
    blocked = bool(manifest_errors) or resolve_error is not None
    if not delete or blocked or scan_error is not None:
        # A dry run, or a run the reference set will not allow: report, do nothing.
        return PruneReport(tuple(candidates), manifest_errors, resolve_error, scan_error)
    targets = {SUPERSEDED, ORPHANED} if include_orphans else {SUPERSEDED}
    results = [_remove_one(c, root, referenced) if c.state in targets else c for c in candidates]
    return PruneReport(tuple(results), manifest_errors, resolve_error, scan_error)
