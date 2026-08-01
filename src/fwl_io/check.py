"""Report whether a data tree matches its manifest, without downloading.

This sits between the two behaviours the fetcher already has. Offline mode
serves what is present and raises as soon as something is not; online mode
downloads whatever is missing. Neither can answer "is this tree complete and
intact", which is what a diagnostic such as ``proteus doctor`` needs: it wants
the whole picture in one pass, and it must not repair the thing it is
inspecting.

A check therefore never reaches the network, never downloads, and never
creates or repairs a dataset. It reads the manifest, hashes what is on disk,
and returns a report. The one mark it leaves is the data root itself, which
resolving a path creates when it is absent, exactly as every other entry point
does; no dataset directory and no file is written.

Two things the report is careful about, because a diagnostic that overstates
what it verified is worse than no diagnostic at all. A manifest that fails to
load is carried in the report rather than dropped, so a tree cannot read as
complete when whole datasets were never looked at. And an archive dataset's
members are reported as present rather than as verified: the archive-only
checksum policy records member names, not per-file digests, so there is
nothing to hash them against once the archive itself is gone.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from fwl_io.fetch import _STAMP_FILENAME, Fetcher, _hash_matches, create_fetcher

log = logging.getLogger('fwl.' + __name__)

# A file is in exactly one of these states. ``present`` is deliberately
# distinct from ``ok``: it says the file is on disk and that nothing was
# available to check its contents against, which is a weaker statement than
# ``ok`` and must not be reported as the same thing.
OK = 'ok'
MISSING = 'missing'
MISMATCH = 'mismatch'
PRESENT = 'present'

#: States that mean the tree is not usable as the manifest describes it.
FAULT_STATES = (MISSING, MISMATCH)


@dataclass(frozen=True)
class FileCheck:
    """The state of one file the manifest declares."""

    name: str
    path: Path
    state: str

    @property
    def faulty(self) -> bool:
        """True when this file is absent or does not match its checksum."""
        return self.state in FAULT_STATES


@dataclass(frozen=True)
class DatasetCheck:
    """The state of every file in one dataset."""

    key: str
    subdir: str
    directory: Path
    files: tuple[FileCheck, ...]

    @property
    def missing(self) -> tuple[FileCheck, ...]:
        """Files the manifest declares that are not on disk."""
        return tuple(f for f in self.files if f.state == MISSING)

    @property
    def mismatched(self) -> tuple[FileCheck, ...]:
        """Files on disk whose contents differ from the registry."""
        return tuple(f for f in self.files if f.state == MISMATCH)

    @property
    def complete(self) -> bool:
        """True when nothing is missing and nothing fails its checksum."""
        return not any(f.faulty for f in self.files)

    @property
    def hashed(self) -> bool:
        """True when every file present was checked against a known digest.

        False for an archive dataset, whose members are recorded by name only,
        so a truncated member is indistinguishable from an intact one here.
        """
        return not any(f.state == PRESENT for f in self.files)

    def summary(self) -> str:
        """One line naming the counts, for a report a person reads."""
        total = len(self.files)
        parts = [f'{total} file(s)']
        if self.missing:
            parts.append(f'{len(self.missing)} missing')
        if self.mismatched:
            parts.append(f'{len(self.mismatched)} corrupt')
        if not self.hashed:
            parts.append('presence only')
        state = 'ok' if self.complete else 'FAILED'
        return f'{self.key}: {state}, ' + ', '.join(parts)


@dataclass(frozen=True)
class CheckReport:
    """Every dataset checked, and every manifest that could not be read."""

    datasets: dict[str, DatasetCheck]
    manifest_errors: dict[str, str]

    @property
    def ok(self) -> bool:
        """True only when every dataset is complete and every manifest loaded.

        A manifest that failed to load counts against the report. Its datasets
        were never inspected, so treating it as harmless would let a tree with
        an unreadable provider report exactly like a healthy one.
        """
        return not self.manifest_errors and all(d.complete for d in self.datasets.values())

    @property
    def faults(self) -> tuple[DatasetCheck, ...]:
        """Datasets with something missing or corrupt, worst named first."""
        broken = [d for d in self.datasets.values() if not d.complete]
        return tuple(sorted(broken, key=lambda d: (-len(d.missing) - len(d.mismatched), d.key)))

    def summary(self) -> str:
        """A short human-readable report, one line per dataset plus a verdict."""
        lines = [d.summary() for d in sorted(self.datasets.values(), key=lambda d: d.key)]
        for provider, error in sorted(self.manifest_errors.items()):
            lines.append(f'{provider}: MANIFEST UNREADABLE, {error}')
        if not lines:
            return 'no datasets checked'
        lines.append('all data present and verified' if self.ok else 'data check FAILED')
        return '\n'.join(lines)


def _archive_members(fetcher: Fetcher) -> list[str] | None:
    """Return the member names an archive dataset's stamp recorded.

    ``None`` when there is no usable stamp, which means the extracted tree is
    not there to be checked rather than that it is empty.
    """
    stamp = fetcher.target_dir / _STAMP_FILENAME
    try:
        record = json.loads(stamp.read_text())
    except (OSError, ValueError):
        return None
    if record.get('extract') != fetcher.extract:
        # The stamp describes a different fetch of this deposit, so its member
        # list does not describe the tree this dataset expects.
        return None
    members = record.get('members')
    if not isinstance(members, list) or not members:
        return None
    return [str(m) for m in members]


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
        members = _archive_members(fetcher)
        if members is None:
            # No usable stamp means no extracted tree to speak of. The dataset
            # is reported as one missing item under the archive's own name,
            # rather than as zero items, which would read as complete.
            archive_name = next(iter(fetcher.registry))
            files = (FileCheck(archive_name, fetcher.target_dir / archive_name, MISSING),)
            return DatasetCheck(key, fetcher.subdir, fetcher.target_dir, files)
        checks = []
        for name in sorted(members):
            path = fetcher.target_dir / name
            checks.append(FileCheck(name, path, PRESENT if path.is_file() else MISSING))
        return DatasetCheck(key, fetcher.subdir, fetcher.target_dir, tuple(checks))

    checks = []
    for name, known_hash in sorted(fetcher.registry.items()):
        path = fetcher.target_dir / name
        if not path.is_file():
            state = MISSING
        elif _hash_matches(path, known_hash):
            state = OK
        else:
            state = MISMATCH
        checks.append(FileCheck(name, path, state))
    return DatasetCheck(key, fetcher.subdir, fetcher.target_dir, tuple(checks))


def check_for(model: str, data_root: str | Path | None = None) -> CheckReport:
    """Report the state of every dataset a given model requires.

    Nothing is downloaded and no dataset directory or file is written, so this
    is safe to run against a tree another process is reading. Resolving the
    data root creates that root when it is absent, which is the only mark a
    check leaves.

    A dataset whose registry or fetcher cannot be built is reported as a
    manifest error rather than skipped, for the same reason an unreadable
    manifest is: the alternative is a report that looks clean because it
    checked less than it appears to.

    Parameters
    ----------
    model : str
        Model name matched (case-insensitively) against ``required_by``.
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.

    Returns
    -------
    CheckReport
        Keyed by dataset, alongside every manifest that could not be read.
    """
    from fwl_io.manifest import _discover

    model = model.lower()
    datasets: dict[str, DatasetCheck] = {}
    providers, errors = _discover()
    manifest_errors = dict(errors)
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
            except Exception as exc:  # noqa: BLE001 -- reported, never raised
                manifest_errors[ds.key] = str(exc)
                log.warning('cannot check dataset %r: %s', ds.key, exc)
                continue
            datasets[ds.key] = check_dataset(fetcher, key=ds.key)
    return CheckReport(datasets=datasets, manifest_errors=manifest_errors)
