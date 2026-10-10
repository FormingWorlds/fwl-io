"""Find the datasets whose Dataverse mirror needs work (``fwl-io mirror-status``).

A dataset needs work when it has no ``dataverse`` pin (unpinned), or when its Zenodo record
has a newer version than the one the manifest pins (stale): the manifest then needs a new
pin, and that version a mirror. A dataset without a pin whose record is not an accepted
record of a Zenodo community of the framework is listed as outside: no mirror is created
for it. Only Zenodo is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from fwl_io.doi import zenodo_record_id
from fwl_io.manifest import Dataset, ProviderError, _discover_all
from fwl_io.pins import manifest_lines
from fwl_io.sync import ZENODO_API, fetch_zenodo_record, in_community


@dataclass
class StatusReport:
    """Outcome of :func:`mirror_status`: datasets that are in order, unpinned, stale,
    outside the communities or could not be read, and the manifests left out."""

    ok: list[str] = field(default_factory=list)
    unpinned: dict[str, str] = field(default_factory=dict)
    stale: dict[str, str] = field(default_factory=dict)
    outside: dict[str, str] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    manifest_errors: dict[str, ProviderError] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        """Return 1 for a manifest error, 5 when a dataset needs a pin or a new version,
        3 when the only problem is a Zenodo record that could not be read, else 0."""
        if self.manifest_errors:
            return 1
        if self.unpinned or self.stale:
            return 5
        return 3 if self.unreadable else 0

    def summary(self) -> str:
        """Return one line per dataset that needs work or could not be read, and the counts."""
        lines = manifest_lines(self.manifest_errors)
        lines += [f'UNPINNED {key}: {doi}' for key, doi in sorted(self.unpinned.items())]
        lines += [f'STALE {key}: {why}' for key, why in sorted(self.stale.items())]
        lines += [f'OUTSIDE {key}: {why}' for key, why in sorted(self.outside.items())]
        lines += [f'UNREADABLE {key}: {why}' for key, why in sorted(self.unreadable.items())]
        lines.append(
            f'in order: {len(self.ok)}, without a pin: {len(self.unpinned)}, '
            f'with a newer Zenodo version: {len(self.stale)}, '
            f'outside the communities: {len(self.outside)}, '
            f'not checked (Zenodo could not be read): {len(self.unreadable)}'
        )
        return '\n'.join(lines)


def latest_record_id(recid: str, api_base: str = ZENODO_API) -> str:
    """Return the record id of the newest version of the Zenodo record ``recid``."""
    response = requests.get(f'{api_base}/{recid}/versions/latest', timeout=30)
    response.raise_for_status()
    return str(response.json()['id'])


def accepted(doi: str) -> bool:
    """Return whether the Zenodo record of ``doi`` is an accepted record of a community."""
    return in_community(fetch_zenodo_record(doi))


def mirror_status(
    datasets: list[Dataset] | None = None, latest=None, community=None
) -> StatusReport:
    """Sort every dataset into in order, unpinned, stale or unreadable.

    Parameters
    ----------
    datasets : list of Dataset, optional
        Datasets to check; defaults to those of every installed manifest, and a manifest
        left out of discovery is reported in ``manifest_errors``.
    latest : callable, optional
        Returns the newest record id of a Zenodo record id; :func:`latest_record_id` by
        default.
    community : callable, optional
        Returns whether a Zenodo DOI is an accepted record of a community of the
        framework; :func:`accepted` by default. It is asked for a dataset without a pin.

    Returns
    -------
    StatusReport
        A dataset can be both unpinned and stale.
    """
    report, latest, community = StatusReport(), latest or latest_record_id, community or accepted
    member: dict[str, bool | Exception] = {}
    if datasets is None:
        discovery = _discover_all()
        datasets = [ds for group in discovery.found.values() for ds in group]
        report.manifest_errors.update(discovery.errors)
    newest: dict[str, str | Exception] = {}
    for ds in datasets:
        recid = zenodo_record_id(ds.zenodo)
        if recid not in newest:
            try:
                newest[recid] = latest(recid)
            except Exception as exc:  # noqa: BLE001 -- reported per dataset, never raised
                newest[recid] = exc
        if isinstance(newest[recid], Exception):
            report.unreadable[ds.key] = f'Zenodo {recid}: {newest[recid]}'
        elif newest[recid] != recid:
            report.stale[ds.key] = f'pins Zenodo {recid}, newest version is {newest[recid]}'
        if not ds.dataverse:
            report.unpinned[ds.key] = ds.zenodo
            if recid not in member:
                try:
                    member[recid] = community(ds.zenodo)
                except Exception as exc:  # noqa: BLE001 -- reported per dataset, never raised
                    member[recid] = exc
            if isinstance(member[recid], Exception):
                report.unreadable.setdefault(ds.key, f'Zenodo {recid}: {member[recid]}')
            elif not member[recid]:
                report.outside[ds.key] = f'Zenodo {recid} is in no community of the framework'
        if ds.key not in report.unreadable | report.stale | report.unpinned:
            report.ok.append(ds.key)
    return report
