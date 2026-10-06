"""Check the Dataverse mirrors pinned in the installed manifests (``fwl-io check-mirrors``).

A pin serves its dataset when the DOI names a released dataset on the Dataverse server
that names the dataset's Zenodo deposit as its source and holds every file of the
dataset's registry: with the registry checksum where the server
uses the same algorithm, otherwise with the size the Zenodo record gives (DataverseNL stores
SHA-1, the registries MD5). The fetch still verifies each downloaded file against the
registry. Only published data is read, so no API token is needed. Datasets without a pin are
listed, since a fallback never reaches them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fwl_io.manifest import Dataset, discover_manifests
from fwl_io.mirror import _ALGORITHMS, DataverseClient, DataverseError
from fwl_io.sync import fetch_zenodo_record


@dataclass
class MirrorReport:
    """Outcome of :func:`check_mirrors`: pinned datasets that pass, fail, and unpinned ones."""

    passed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every pinned dataset is served by its mirror."""
        return not self.failed

    def summary(self) -> str:
        """Return one line per failed pin, then the counts and the unpinned datasets."""
        lines = [f'FAIL {key}: {why}' for key, why in sorted(self.failed.items())]
        lines.append(
            f'{len(self.passed)} pinned datasets served by their mirror, '
            f'{len(self.failed)} not, {len(self.unpinned)} without a pin'
        )
        lines += [f'unpinned {key}' for key in sorted(self.unpinned)]
        return '\n'.join(lines)


def zenodo_sizes(doi: str) -> dict[str, int]:
    """Return the name-to-size map of the files of a Zenodo version DOI."""
    files = fetch_zenodo_record(doi).get('files')
    if isinstance(files, dict):  # InvenioRDM shape
        return {name: meta['size'] for name, meta in (files.get('entries') or {}).items()}
    return {entry['key']: entry['size'] for entry in files or []}


def pin_problem(dataset: Dataset, client: DataverseClient, sizes=zenodo_sizes) -> str | None:
    """Return why the pin of ``dataset`` does not serve its registry, or None when it does.

    Parameters
    ----------
    dataset : Dataset
        A dataset with a ``dataverse`` pin and a committed registry.
    client : DataverseClient
        Client for the Dataverse server that holds the pinned DOI.
    sizes : callable
        Returns the name-to-size map of a Zenodo DOI, used for files whose mirror
        checksum is in another algorithm than the registry.

    Returns
    -------
    str or None
        A short reason (server error, version not released, file missing, checksum or
        size differs), or None when every registry file is on the mirror.
    """
    try:
        body = client._request(
            'GET',
            '/api/datasets/:persistentId/',
            params={'persistentId': f'doi:{dataset.dataverse}'},
        )
    except DataverseError as exc:
        return f'cannot read doi:{dataset.dataverse}: {exc}'
    version = (body.get('data') or {}).get('latestVersion') or {}
    if version.get('versionState') != 'RELEASED':
        return f'doi:{dataset.dataverse} latest version is {version.get("versionState")!r}'
    # The mirror describes itself as a mirror of its Zenodo deposit; this tells apart two
    # mirrors whose files share names and sizes.
    if dataset.zenodo not in str(version.get('metadataBlocks', {}).get('citation', {})):
        return f'doi:{dataset.dataverse} does not name Zenodo {dataset.zenodo} as its source'
    files = {f['dataFile']['filename']: f['dataFile'] for f in version.get('files', [])}
    problems, zenodo = [], None
    for name, digest in sorted(dataset.registry().items()):
        meta = files.get(name)
        checksum = (meta or {}).get('checksum') or {}
        algorithm = _ALGORITHMS.get(checksum.get('type'))
        if meta is None:
            problems.append(f'{name} missing')
        elif algorithm == digest.partition(':')[0]:
            if f'{algorithm}:{checksum.get("value")}' != digest:
                problems.append(f'{name} checksum differs')
        else:
            if zenodo is None:
                try:
                    zenodo = sizes(dataset.zenodo)
                except Exception as exc:  # noqa: BLE001 -- reported as the reason
                    return f'cannot read Zenodo {dataset.zenodo} for file sizes: {exc}'
            if meta.get('filesize') != zenodo.get(name):
                problems.append(f'{name} size differs from Zenodo')
    return '; '.join(problems) or None


def check_mirrors(dataverse_url: str, datasets: list[Dataset] | None = None) -> MirrorReport:
    """Check every pinned dataset against its mirror and list the unpinned ones.

    Parameters
    ----------
    dataverse_url : str
        Base URL of the Dataverse server the pins live on.
    datasets : list of Dataset, optional
        Datasets to check; defaults to those of every installed manifest.

    Returns
    -------
    MirrorReport
        Passed, failed (with the reason) and unpinned dataset keys.
    """
    if datasets is None:
        datasets = [ds for found in discover_manifests().values() for ds in found]
    client = DataverseClient(dataverse_url, token='')
    report = MirrorReport()
    for ds in datasets:
        if not ds.dataverse:
            report.unpinned.append(ds.key)
        elif (why := pin_problem(ds, client)) is None:
            report.passed.append(ds.key)
        else:
            report.failed[ds.key] = why
    return report
