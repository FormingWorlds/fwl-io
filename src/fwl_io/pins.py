"""Check the Dataverse mirrors pinned in the installed manifests (``fwl-io check-mirrors``).

See :func:`pin_problem` for what a pin must satisfy. Only published data is read, so no API
token is needed. Datasets without a pin are listed, since a fallback never reaches them, and a
pin whose server or Zenodo record cannot be read is reported apart from a pin that is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from fwl_io.manifest import Dataset, _discover
from fwl_io.mirror import _ALGORITHMS, DataverseClient, DataverseError, DataverseRetryableError
from fwl_io.sync import fetch_zenodo_record


class Unreachable(Exception):
    """The Dataverse server or the Zenodo record needed for a check could not be read."""


@dataclass
class MirrorReport:
    """Outcome of :func:`check_mirrors`: passed, failed, unreachable and unpinned datasets."""

    passed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    unreachable: dict[str, str] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether at least one pin was checked and every checked pin is served by its mirror."""
        return bool(self.passed) and not (self.failed or self.unreachable)

    def summary(self) -> str:
        """Return one line per failed or unreachable pin, the counts, and the unpinned datasets."""
        lines = [f'FAIL {key}: {why}' for key, why in sorted(self.failed.items())]
        lines += [f'UNREACHABLE {key}: {why}' for key, why in sorted(self.unreachable.items())]
        lines.append(
            f'pins served by their mirror: {len(self.passed)}, wrong: {len(self.failed)}, '
            f'not checked (server unreachable): {len(self.unreachable)}, '
            f'datasets without a pin: {len(self.unpinned)}'
        )
        lines += [f'unpinned {key}' for key in sorted(self.unpinned)]
        return '\n'.join(lines)


def zenodo_sizes(doi: str) -> dict[str, int]:
    """Return the name-to-size map of the files of a Zenodo version DOI."""
    files = fetch_zenodo_record(doi).get('files')
    if isinstance(files, dict):  # InvenioRDM shape
        return {name: meta['size'] for name, meta in (files.get('entries') or {}).items()}
    return {entry['key']: entry['size'] for entry in files or []}


def _descriptions(version: dict) -> str:
    """Return the dsDescription values of a dataset version's citation block, one per line."""
    fields = ((version.get('metadataBlocks') or {}).get('citation') or {}).get('fields') or []
    return '\n'.join(
        str(((item or {}).get('dsDescriptionValue') or {}).get('value', ''))
        for f in fields
        if f.get('typeName') == 'dsDescription'
        for item in f.get('value') or []
    )


def pin_problem(dataset: Dataset, client: DataverseClient, sizes=zenodo_sizes) -> str | None:
    """Return why the pin of ``dataset`` does not serve its registry, or None when it does.

    A pin serves its dataset when the DOI names a released dataset whose description names
    the dataset's Zenodo DOI (the note every mirror carries, which tells apart two mirrors
    whose files share names and sizes) and which holds every registry file once: with the
    registry checksum where the server uses the same algorithm, otherwise with the file size
    of the Zenodo record (DataverseNL stores SHA-1, the registries MD5). A fetch still
    verifies each downloaded file against the registry.

    Parameters
    ----------
    dataset : Dataset
        A dataset with a ``dataverse`` pin and a committed registry.
    client : DataverseClient
        Client for the Dataverse server that holds the pinned DOI.
    sizes : callable
        Returns the name-to-size map of a Zenodo DOI.

    Returns
    -------
    str or None
        Why the pin is wrong, or None when it serves the registry.

    Raises
    ------
    Unreachable
        If the server answers with a transient error or the Zenodo sizes cannot be read.
    """
    registry = dataset.registry()
    if not registry:
        return 'the registry lists no files'
    try:
        body = client._request(
            'GET',
            '/api/datasets/:persistentId/',
            params={'persistentId': f'doi:{dataset.dataverse}'},
        )
    except DataverseRetryableError as exc:
        raise Unreachable(f'doi:{dataset.dataverse}: {exc}') from exc
    except DataverseError as exc:
        return f'cannot read doi:{dataset.dataverse}: {exc}'
    version = (body.get('data') or {}).get('latestVersion') or {}
    if version.get('versionState') != 'RELEASED':
        return f'doi:{dataset.dataverse} latest version is {version.get("versionState")!r}'
    if not re.search(re.escape(dataset.zenodo) + r'(?!\d)', _descriptions(version)):
        return f'doi:{dataset.dataverse} does not name Zenodo {dataset.zenodo} as its source'
    files: dict[str, dict] = {}
    for entry in version.get('files') or []:
        meta = (entry or {}).get('dataFile') or {}
        name = meta.get('filename')
        if name in files:
            return f'doi:{dataset.dataverse} holds {name} twice'
        files[name] = meta
    problems, zenodo = [], None
    for name, digest in sorted(registry.items()):
        meta = files.get(name)
        checksum = (meta or {}).get('checksum') or {}
        algorithm = _ALGORITHMS.get(checksum.get('type'))
        if meta is None:
            problems.append(f'{name} missing')
        elif algorithm == digest.partition(':')[0]:
            if f'{algorithm}:{str(checksum.get("value")).lower()}' != digest.lower():
                problems.append(f'{name} checksum differs')
        else:
            if zenodo is None:
                try:
                    zenodo = sizes(dataset.zenodo)
                except Exception as exc:  # noqa: BLE001 -- any read failure is unreachable
                    raise Unreachable(f'Zenodo {dataset.zenodo} file sizes: {exc}') from exc
            size = meta.get('filesize')
            if not isinstance(size, int) or size != zenodo.get(name):
                problems.append(f'{name} size differs from Zenodo')
    return '; '.join(problems) or None


def check_mirrors(dataverse_url: str, datasets: list[Dataset] | None = None) -> MirrorReport:
    """Check every pinned dataset against its mirror and list the unpinned ones.

    Parameters
    ----------
    dataverse_url : str
        Base URL of the Dataverse server the pins live on.
    datasets : list of Dataset, optional
        Datasets to check; defaults to those of every installed manifest, and a manifest
        that fails to load is reported as failed.

    Returns
    -------
    MirrorReport
        Passed, failed and unreachable pins (with the reason) and unpinned dataset keys.
    """
    report = MirrorReport()
    if datasets is None:
        found, errors = _discover()
        datasets = [ds for group in found.values() for ds in group]
        report.failed.update({f'manifest {name}': why for name, why in errors.items()})
    client = DataverseClient(dataverse_url, token='')
    cache: dict[str, dict[str, int]] = {}

    def cached_sizes(doi: str) -> dict[str, int]:
        if doi not in cache:
            cache[doi] = zenodo_sizes(doi)
        return cache[doi]

    for ds in datasets:
        if not ds.dataverse:
            report.unpinned.append(ds.key)
            continue
        try:
            why = pin_problem(ds, client, sizes=cached_sizes)
        except Unreachable as exc:
            report.unreachable[ds.key] = str(exc)
            continue
        except (OSError, ValueError, KeyError, TypeError) as exc:
            why = f'{type(exc).__name__}: {exc}'
        if why is None:
            report.passed.append(ds.key)
        else:
            report.failed[ds.key] = why
    return report
