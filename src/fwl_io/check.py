"""Report whether a data tree matches its manifest, without downloading.

This sits between the two behaviours the fetcher already has. Offline mode
serves what is present and raises as soon as something is not; online mode
downloads whatever is missing. Neither can answer "is this tree complete and
intact", which is what a diagnostic such as ``proteus doctor`` needs: it wants
the whole picture in one pass, and it must not repair the thing it is
inspecting.

A check therefore never reaches the network, never downloads, and never
creates or repairs a dataset. It reads the manifest, hashes what is on disk,
and returns a report. Resolving a path creates the data root when it is
absent, exactly as every other entry point does; no dataset directory and no
file is written.

The report is careful about what it did not establish, because a diagnostic
that overstates its own coverage is worse than none. A manifest that fails to
load and a dataset whose registry cannot be read are both carried in the
report and both count against the verdict, since their files were never
looked at, and they are carried apart from each other because they call for
different repairs. An archive dataset's members are reported as present
rather than as verified: the archive-only checksum policy records member
names, not per-file digests, so once the archive is gone there is nothing to
hash them against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from fwl_io.fetch import Fetcher, create_fetcher

log = logging.getLogger('fwl.' + __name__)

# A file is in exactly one of these states. ``present`` is deliberately
# distinct from ``ok``: it says the file is on disk and that nothing was
# available to check its contents against, which is a weaker statement than
# ``ok`` and must not be reported as the same thing.
OK = 'ok'
MISSING = 'missing'
MISMATCH = 'mismatch'
UNREADABLE = 'unreadable'
PRESENT = 'present'

#: States that mean the tree is not usable as the manifest describes it. A file
#: that cannot be read counts: whether its contents are right is unknown, and a
#: check reports what it could not establish rather than assuming the best.
FAULT_STATES = (MISSING, MISMATCH, UNREADABLE)


@dataclass(frozen=True)
class FileCheck:
    """The state of one file the manifest declares."""

    name: str
    path: Path
    state: str

    @property
    def faulty(self) -> bool:
        """True when this file is absent, corrupt, or could not be read."""
        return self.state in FAULT_STATES


@dataclass(frozen=True)
class DatasetCheck:
    """The state of every file in one dataset.

    ``verifiable`` says whether this dataset's registry carries a digest for
    each file it declares. It is false for an archive dataset, whose members
    are recorded by name only, and it is a property of the dataset rather than
    of what happens to be on disk, so an archive dataset with no members left
    cannot read as verifiable. It has no default: the wrong value is the one
    that lets a presence-only dataset read as verified, so every caller states
    it rather than inheriting it.
    """

    key: str
    subdir: str
    directory: Path
    files: tuple[FileCheck, ...]
    verifiable: bool

    def _in_state(self, state: str) -> tuple[FileCheck, ...]:
        return tuple(f for f in self.files if f.state == state)

    @property
    def missing(self) -> tuple[FileCheck, ...]:
        """Files the manifest declares that are not on disk."""
        return self._in_state(MISSING)

    @property
    def mismatched(self) -> tuple[FileCheck, ...]:
        """Files on disk whose contents differ from the registry."""
        return self._in_state(MISMATCH)

    @property
    def unreadable(self) -> tuple[FileCheck, ...]:
        """Files on disk that could not be read to be checked."""
        return self._in_state(UNREADABLE)

    @property
    def faults(self) -> tuple[FileCheck, ...]:
        """Every file that is not in a usable state."""
        return tuple(f for f in self.files if f.faulty)

    @property
    def complete(self) -> bool:
        """True when nothing is missing, corrupt, or unreadable."""
        return not self.faults

    def summary(self) -> str:
        """One line naming the counts, for a report a person reads."""
        parts = [f'{len(self.files)} file(s)']
        for label, group in (
            ('missing', self.missing),
            ('corrupt', self.mismatched),
            ('unreadable', self.unreadable),
        ):
            if group:
                parts.append(f'{len(group)} {label}')
        if not self.verifiable:
            parts.append('presence only')
        state = 'ok' if self.complete else 'FAILED'
        return f'{self.key}: {state}, ' + ', '.join(parts)


@dataclass(frozen=True)
class CheckReport:
    """Every dataset checked, and everything that stopped one being checked.

    The two error maps are kept apart because they call for different repairs.
    A manifest error means an installed package's manifest could not be read at
    all, so nothing it declares was inspected. A dataset error means the
    manifest was fine but that one dataset could not be resolved, most often
    because its registry has never been generated.
    """

    datasets: dict[str, DatasetCheck] = field(default_factory=dict)
    manifest_errors: dict[str, str] = field(default_factory=dict)
    dataset_errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only when something was checked and all of it was sound.

        An empty report is not ok. A caller asking about a model and being told
        nothing is wrong, when in truth nothing was looked at, is the failure
        this whole module exists to avoid; the two are indistinguishable to
        anyone reading a boolean.
        """
        if not self.datasets:
            return False
        if self.manifest_errors or self.dataset_errors:
            return False
        return all(d.complete for d in self.datasets.values())

    @property
    def verified(self) -> bool:
        """True when the tree is sound and every file in it was hashed.

        Stricter than ``ok``, which a presence-only dataset satisfies. Presence
        is all the archive-only checksum policy makes checkable, so such a
        dataset is not a fault; but a caller that needs to know the contents
        were compared against a digest must ask this and not ``ok``.
        """
        return self.ok and all(d.verifiable for d in self.datasets.values())

    @property
    def presence_only(self) -> tuple[DatasetCheck, ...]:
        """Datasets whose files carry no digest to be checked against."""
        unhashed = [d for d in self.datasets.values() if not d.verifiable]
        return tuple(sorted(unhashed, key=lambda d: d.key))

    @property
    def faults(self) -> tuple[DatasetCheck, ...]:
        """Datasets with something wrong, the worst affected named first.

        Datasets only. A report failed by an unreadable manifest or a dataset
        that could not be resolved has nothing to put here, so this being
        empty is not the same as nothing being wrong; ``ok`` is the question
        that covers every reason.
        """
        broken = [d for d in self.datasets.values() if not d.complete]
        return tuple(sorted(broken, key=lambda d: (-len(d.faults), d.key)))

    def summary(self) -> str:
        """A short human-readable report, one line per dataset plus a verdict."""
        lines = [d.summary() for d in sorted(self.datasets.values(), key=lambda d: d.key)]
        for provider, error in sorted(self.manifest_errors.items()):
            lines.append(f'{provider}: MANIFEST UNREADABLE, {error}')
        for key, error in sorted(self.dataset_errors.items()):
            lines.append(f'{key}: NOT CHECKED, {error}')
        if not lines:
            return 'nothing was checked'
        lines.append(self._verdict())
        return '\n'.join(lines)

    def _verdict(self) -> str:
        """The closing line, which must not claim more than was established."""
        if not self.ok:
            return 'data check FAILED'
        if self.verified:
            return 'all data present and verified'
        # Sound, but part of it was checked by name alone. Saying "verified"
        # here is the overstatement this module exists to avoid.
        count = len(self.presence_only)
        return f'all data present, {count} dataset(s) by presence only'


def _file_state(fetcher: Fetcher, name: str) -> str:
    """Classify one registry file: present and correct, absent, or otherwise."""
    path = fetcher.target_dir / name
    try:
        if not path.is_file():
            return MISSING
        return OK if fetcher.file_matches(name) else MISMATCH
    except OSError as exc:
        # A file the checker cannot read is not evidence of a good tree. This
        # is reported rather than raised so one unreadable file cannot abort a
        # report covering every other dataset.
        log.warning('cannot read %s: %s', path, exc)
        return UNREADABLE


def _member_state(path: Path) -> str:
    """Classify one extracted member, which has no digest to be checked against.

    Reporting rather than raising for the same reason ``_file_state`` does: a
    member the checker cannot reach, most often because a directory above it
    denies traversal, must cost that one entry and not the whole report.
    """
    try:
        return PRESENT if path.is_file() else MISSING
    except OSError as exc:
        log.warning('cannot read %s: %s', path, exc)
        return UNREADABLE


def check_dataset(fetcher: Fetcher, key: str = '') -> DatasetCheck:
    """Report the state of one dataset's files without touching the network.

    Parameters
    ----------
    fetcher : Fetcher
        Configured for the dataset to inspect. Nothing is fetched.
    key : str, optional
        Name for the dataset in the report; defaults to its subdirectory.

    Returns
    -------
    DatasetCheck
        One entry per file the manifest declares. For an archive dataset the
        entries are the extracted members recorded in the provenance stamp,
        reported as present rather than verified, since the registry pins the
        archive's checksum and not the checksums of what came out of it.
    """
    key = key or fetcher.subdir

    if fetcher.extract is not None:
        members = fetcher.recorded_members()
        if members is None:
            # No usable stamp means no extracted tree to speak of. The dataset
            # is reported as one missing item under the archive's own name,
            # rather than as zero items, which would read as complete.
            archive_name = next(iter(fetcher.registry))
            files = (FileCheck(archive_name, fetcher.target_dir / archive_name, MISSING),)
            return DatasetCheck(key, fetcher.subdir, fetcher.target_dir, files, verifiable=False)
        checks = [
            FileCheck(name, fetcher.target_dir / name, _member_state(fetcher.target_dir / name))
            for name in sorted(members)
        ]
        return DatasetCheck(
            key, fetcher.subdir, fetcher.target_dir, tuple(checks), verifiable=False
        )

    checks = [
        FileCheck(name, fetcher.target_dir / name, _file_state(fetcher, name))
        for name in sorted(fetcher.registry)
    ]
    return DatasetCheck(key, fetcher.subdir, fetcher.target_dir, tuple(checks), verifiable=True)


def check_for(model: str, data_root: str | Path | None = None) -> CheckReport:
    """Report the state of every dataset a given model requires.

    Nothing is downloaded and no dataset directory or file is written, so this
    is safe to run against a tree another process is reading.

    Nothing here raises for the state of the data or of a manifest. A caller
    running a check wants the whole picture, including the parts that could not
    be established, so every failure is carried in the report instead.

    Parameters
    ----------
    model : str
        Model name matched (case-insensitively) against ``required_by``.
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.

    Returns
    -------
    CheckReport
        Keyed by dataset, alongside the manifests that could not be read and
        the datasets that could not be resolved.
    """
    from fwl_io.manifest import _discover

    model = model.lower()
    datasets: dict[str, DatasetCheck] = {}
    dataset_errors: dict[str, str] = {}
    providers, manifest_errors = _discover()
    for provider_datasets in providers.values():
        for ds in provider_datasets:
            if model not in tuple(r.lower() for r in ds.required_by):
                continue
            try:
                fetcher = create_fetcher(
                    subdir=ds.subdir,
                    zenodo=ds.zenodo,
                    dataverse=ds.dataverse,
                    registry=ds.registry(),
                    data_root=data_root,
                    extract=ds.extract,
                )
                datasets[ds.key] = check_dataset(fetcher, key=ds.key)
            except Exception as exc:  # noqa: BLE001 -- reported, never raised
                dataset_errors[ds.key] = str(exc)
                log.warning('cannot check dataset %r: %s', ds.key, exc)
    return CheckReport(
        datasets=datasets,
        manifest_errors=dict(manifest_errors),
        dataset_errors=dataset_errors,
    )
