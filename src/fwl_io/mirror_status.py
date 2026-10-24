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
from fwl_io.paths import require_online
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
    gone: dict[str, str] = field(default_factory=dict)
    manifest_errors: dict[str, ProviderError] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        """Return 1 for a manifest error, 5 when a dataset needs a pin or a new version,
        4 for a Zenodo record that is gone, 3 when the only problem is a Zenodo record that
        could not be read, else 0."""
        if self.manifest_errors:
            return 1
        if self.unpinned or self.stale:
            return 5
        if self.gone:
            return 4
        return 3 if self.unreadable else 0

    def summary(self) -> str:
        """Return one line per dataset that needs work or could not be read, and the counts."""
        lines = manifest_lines(self.manifest_errors)
        lines += [f'UNPINNED {key}: {doi}' for key, doi in sorted(self.unpinned.items())]
        lines += [f'STALE {key}: {why}' for key, why in sorted(self.stale.items())]
        lines += [f'OUTSIDE {key}: {why}' for key, why in sorted(self.outside.items())]
        lines += [f'GONE {key}: {why}' for key, why in sorted(self.gone.items())]
        lines += [f'UNREADABLE {key}: {why}' for key, why in sorted(self.unreadable.items())]
        lines.append(
            f'in order: {len(self.ok)}, without a pin: {len(self.unpinned)}, '
            f'with a newer Zenodo version: {len(self.stale)}, '
            f'outside the communities: {len(self.outside)}, '
            f'not checked (Zenodo could not be read): {len(self.unreadable) + len(self.gone)}'
        )
        return '\n'.join(lines)


class RecordWithoutId(LookupError):
    """Zenodo answered for a record, but the reply holds no record id."""


def latest_record_id(recid: str, api_base: str = ZENODO_API) -> str:
    """Return the record id of the newest version of the Zenodo record ``recid``.

    Raises
    ------
    RecordWithoutId
        If the reply is no JSON object, or its ``id`` is not an integer or a string of digits.
    OfflineModeError
        If ``FWL_IO_OFFLINE`` is set.
    """
    require_online(f'reading the newest version of Zenodo record {recid}')
    response = requests.get(f'{api_base}/{recid}/versions/latest', timeout=30)
    response.raise_for_status()
    body = response.json()
    rid = body.get('id') if isinstance(body, dict) else None
    if not str(rid).isdigit():
        raise RecordWithoutId(f'no record id in the reply for Zenodo {recid}')
    return str(rid)


def accepted(doi: str) -> bool:
    """Return whether the Zenodo record of ``doi`` is an accepted record of a community."""
    return in_community(fetch_zenodo_record(doi))


def mirror_status(
    datasets: list[Dataset] | None = None, latest=None, community=None
) -> StatusReport:
    """Sort every dataset into in order, unpinned, stale, outside or unreadable.

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

    Raises
    ------
    OfflineModeError
        If ``FWL_IO_OFFLINE`` is set.
    """
    require_online('mirror-status')
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


def newer_versions(
    prefix: str = '', datasets: list[Dataset] | None = None, latest=None
) -> StatusReport:
    """Sort the datasets whose key starts with ``prefix`` into in order, stale or unreadable.

    Each Zenodo record is read once, with no retry, so a scheduled check costs one API call
    per pinned record. A record that Zenodo does not serve (HTTP 4xx other than 401, 403, 408
    and 429, or a reply without a record id) is gone; any other read failure is unreadable.
    A key declared by more than one dataset is named ``'<key> (Zenodo <record>)'`` for each.

    Parameters
    ----------
    prefix : str
        Start of the dataset keys to check; the empty string checks every dataset.
    datasets : list of Dataset, optional
        Datasets to choose from; defaults to those of every installed manifest, and a
        manifest left out of discovery is reported in ``manifest_errors``.
    latest : callable, optional
        Returns the newest record id of a Zenodo record id; :func:`latest_record_id` by
        default.

    Returns
    -------
    StatusReport
        Only ``ok``, ``stale``, ``unreadable``, ``gone`` and ``manifest_errors`` are filled;
        :func:`latest_exit_code` gives its exit status.

    Raises
    ------
    OfflineModeError
        If ``FWL_IO_OFFLINE`` is set.
    """
    require_online('latest')
    report, latest = StatusReport(), latest or latest_record_id
    if datasets is None:
        discovery = _discover_all()
        datasets = [ds for group in discovery.found.values() for ds in group]
        report.manifest_errors.update(discovery.errors)
    newest: dict[str, str | Exception] = {}
    chosen = [ds for ds in datasets if ds.key.startswith(prefix)]
    twice = {ds.key for ds in chosen if sum(d.key == ds.key for d in chosen) > 1}
    for ds in chosen:
        recid = zenodo_record_id(ds.zenodo)
        name = f'{ds.key} (Zenodo {recid})' if ds.key in twice else ds.key
        if recid not in newest:
            try:
                newest[recid] = latest(recid)
            except Exception as exc:  # noqa: BLE001 -- reported per dataset, never raised
                newest[recid] = exc
        if isinstance(newest[recid], Exception):
            group = report.gone if _gone(newest[recid]) else report.unreadable
            group[name] = f'Zenodo {recid}: {newest[recid]}'
        elif newest[recid] != recid:
            report.stale[name] = f'pins Zenodo {recid}, newest version is {newest[recid]}'
        else:
            report.ok.append(name)
    return report


def _gone(exc: Exception) -> bool:
    """Whether a read failure says Zenodo does not serve the record: a reply without a
    record id, or HTTP 4xx other than 401, 403, 408 and 429. Every other failure can clear."""
    if isinstance(exc, RecordWithoutId):
        return True
    code = getattr(getattr(exc, 'response', None), 'status_code', None)
    return (
        isinstance(exc, requests.HTTPError)
        and isinstance(code, int)
        and 400 <= code < 500
        and code not in (401, 403, 408, 429)
    )


def latest_exit_code(report: StatusReport) -> int:
    """Return the exit status of ``fwl-io latest`` for a :func:`newer_versions` report.

    1 for a manifest left out, 5 for a newer version, 4 for a record that is gone or when
    no record was read, 3 when only a passing read failure is left, else 0.
    """
    if report.manifest_errors:
        return 1
    if report.stale:
        return 5
    if report.gone or (report.unreadable and not report.ok):
        return 4
    return 3 if report.unreadable else 0
