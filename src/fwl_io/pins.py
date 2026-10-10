"""Check the Dataverse mirrors pinned in the installed manifests (``fwl-io check-mirrors``).

See :func:`pin_problem` for what a pin must satisfy. Only published data is read, so no API
token is needed. Datasets without a pin are listed, since a fallback never reaches them, and a
pin whose server or Zenodo record cannot be read is reported apart from a pin that is wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import requests

from fwl_io.manifest import Dataset, ProviderError, _discover_all
from fwl_io.mirror import (
    DataverseClient,
    DataverseError,
    DataverseRetryableError,
    checksum_algorithm,
    names_source,
)
from fwl_io.sync import fetch_zenodo_record
from fwl_io.transient import is_cert_failure, is_transient, is_transient_status

CERT_ATTEMPTS = 3
CERT_WAIT_S = 30.0
ZENODO_HOST = 'zenodo.org'
# Dataverse server of each known DOI prefix; any other prefix is resolved through doi.org.
DATAVERSE_SERVERS = {'10.34894': 'https://dataverse.nl'}
DOI_HOST = 'doi.org'
DOI_HANDLES = f'https://{DOI_HOST}/api/handles'


class Unreachable(Exception):
    """The Dataverse server or the Zenodo record needed for a check could not be read."""


class UnknownServer(Exception):
    """doi.org names no landing page, so no Dataverse server, for a pinned DOI."""


@dataclass
class MirrorReport:
    """Outcome of :func:`check_mirrors`: passed, failed, unreachable and unpinned datasets,
    the manifests left out of discovery, and warnings for reads that needed a retry."""

    passed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    unreachable: dict[str, str] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)
    manifest_errors: dict[str, ProviderError] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether at least one pin was checked and every checked pin is served by its mirror."""
        return self.exit_code == 0

    @property
    def exit_code(self) -> int:
        """Return 1 for a wrong pin, a manifest error or nothing checked, 4 when no pin could
        be read at all, 3 when some could not be read and the rest are served, and 0 when
        every pin is served."""
        if self.failed or self.manifest_errors or not (self.passed or self.unreachable):
            return 1
        if self.unreachable:
            return 3 if self.passed else 4
        return 0

    def summary(self) -> str:
        """Return a line per problem, the counts, and the unpinned datasets."""
        lines = [
            f'FAIL {provider}: MANIFEST {error.verdict}, {error.message}'
            for provider, error in sorted(self.manifest_errors.items())
        ]
        lines += [f'FAIL {key}: {why}' for key, why in sorted(self.failed.items())]
        lines += [f'UNREACHABLE {key}: {why}' for key, why in sorted(self.unreachable.items())]
        lines += [f'WARNING {key}: {note}' for key, note in sorted(self.warnings.items())]
        lines.append(
            f'pins served by their mirror: {len(self.passed)}, wrong: {len(self.failed)}, '
            f'not checked (could not be read): {len(self.unreachable)}, '
            f'datasets without a pin: {len(self.unpinned)}, '
            f'manifests not used: {len(self.manifest_errors)}'
        )
        lines += [f'unpinned {key}' for key in sorted(self.unpinned)]
        return '\n'.join(lines)


def zenodo_sizes(doi: str) -> dict[str, int]:
    """Return the name-to-size map of the files of a Zenodo version DOI."""
    files = fetch_zenodo_record(doi).get('files')
    if isinstance(files, dict):  # InvenioRDM shape
        return {name: meta['size'] for name, meta in (files.get('entries') or {}).items()}
    return {entry['key']: entry['size'] for entry in files or []}


def _read(call, what: str, notes: list[str] | None, host: str, cert_failed: set[str] | None):
    """Return ``call()``, retrying a certificate failure up to CERT_ATTEMPTS times.

    A host in ``cert_failed`` already failed every attempt in this run, so it gets one attempt
    and no wait; a host that fails every attempt is added to it. A recovery, and a read that
    was not retried, are noted in ``notes``; any other failure is raised.
    """
    attempts = 1 if cert_failed is not None and host in cert_failed else CERT_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            value = call()
        except Exception as exc:
            if not is_cert_failure(exc):
                raise
            if attempt < attempts:
                time.sleep(CERT_WAIT_S)
                continue
            if attempts == 1 and notes is not None:
                notes.append(
                    f'{what}: certificate error, not retried since {host} failed '
                    f'{CERT_ATTEMPTS} attempts earlier in this run'
                )
            if cert_failed is not None:
                cert_failed.add(host)
            raise
        if attempt > 1 and notes is not None:
            notes.append(f'{what}: certificate error, recovered after {attempt} attempts')
        return value


def _dataverse_transient(exc: DataverseError) -> bool:
    """Return whether a Dataverse read failed in transit: the bot-check page, a lost connection,
    a timeout or a cut body, an HTTP 408, 429 or 5xx, or a 2xx whose body is not JSON (an
    HTML page in place of the API answer)."""
    status = exc.status_code
    return (
        isinstance(exc, DataverseRetryableError)
        or is_transient_status(status)
        or (isinstance(status, int) and 200 <= status < 300)
    )


def _embargo(meta: dict, today: date) -> str | None:
    """Return why a file's embargo blocks its download, or None.

    It blocks while ``dataFile.embargo.dateAvailable`` is after ``today``, and a date that
    cannot be read counts as blocking.
    """
    embargo = meta.get('embargo')
    if embargo is None:
        return None
    try:
        until = date.fromisoformat(str(embargo['dateAvailable'])[:10])
    except (TypeError, KeyError, ValueError):
        return 'unreadable embargo date'
    return f'embargoed until {until}' if until > today else None


def _descriptions(version: dict) -> str:
    """Return the dsDescription values of a dataset version's citation block, one per line."""
    fields = ((version.get('metadataBlocks') or {}).get('citation') or {}).get('fields') or []
    return '\n'.join(
        str(((item or {}).get('dsDescriptionValue') or {}).get('value', ''))
        for f in fields
        if f.get('typeName') == 'dsDescription'
        for item in f.get('value') or []
    )


def pin_problem(
    dataset: Dataset,
    client: DataverseClient,
    sizes=zenodo_sizes,
    notes: list[str] | None = None,
    cert_failed: set[str] | None = None,
) -> str | None:
    """Return why the pin of ``dataset`` does not serve its registry, or None when it does.

    A pin serves its dataset when the DOI names a released dataset whose description names
    the dataset's Zenodo DOI in the note every mirror carries ("Mirror of Zenodo deposit
    <doi>", which tells apart two mirrors whose files share names and sizes) and which holds
    every registry file once, unrestricted and with no embargo that ends after today: with
    the registry checksum where the server uses the same algorithm, otherwise with the file
    size of the Zenodo record (DataverseNL stores SHA-1, the registries MD5). Files are
    matched by name, folder label ignored, as the fetch reads them through pooch. A fetch
    still verifies each downloaded file against the registry.

    Parameters
    ----------
    dataset : Dataset
        A dataset with a ``dataverse`` pin and a committed registry.
    client : DataverseClient
        Client for the Dataverse server that holds the pinned DOI.
    sizes : callable
        Returns the name-to-size map of a Zenodo DOI.
    notes : list of str, optional
        Receives a warning when the Dataverse read needed a retry after a certificate error.
    cert_failed : set of str, optional
        Hosts that failed every certificate attempt earlier in the run; see ``_read``.

    Returns
    -------
    str or None
        Why the pin is wrong, or None when it serves the registry.

    Raises
    ------
    Unreachable
        If the server answers with a transient error, or a transient error stops the Zenodo
        size read before any other problem was found.
    """
    registry = dataset.registry()
    if not registry:
        return 'the registry lists no files'
    pin, zenodo_doi = dataset.dataverse.removeprefix('doi:'), dataset.zenodo.removeprefix('doi:')
    try:
        body = _read(
            lambda: client._request(
                'GET', '/api/datasets/:persistentId/', params={'persistentId': f'doi:{pin}'}
            ),
            f'doi:{pin}',
            notes,
            urlparse(client.base_url).hostname,
            cert_failed,
        )
    except DataverseError as exc:
        if _dataverse_transient(exc):
            raise Unreachable(f'doi:{pin}: {exc}') from exc
        return f'cannot read doi:{pin}: {exc}'
    version = (body.get('data') or {}).get('latestVersion') or {}
    if version.get('versionState') != 'RELEASED':
        return f'doi:{pin} latest version is {version.get("versionState")!r}'
    if not names_source(_descriptions(version), zenodo_doi):
        return f'doi:{pin} does not name Zenodo {zenodo_doi} as its source'
    files: dict[str, dict] = {}
    for entry in version.get('files') or []:
        meta = (entry or {}).get('dataFile') or {}
        name = meta.get('filename')
        if name in files and name in registry:
            return f'doi:{pin} holds {name} twice'
        files[name] = {**meta, 'restricted': (entry or {}).get('restricted')}
    problems, by_size, today = [], [], date.today()
    for name, digest in sorted(registry.items()):
        meta = files.get(name)
        checksum = (meta or {}).get('checksum') or {}
        algorithm = checksum_algorithm(meta or {})
        if meta is None:
            problems.append(f'{name} missing')
        elif meta['restricted']:
            problems.append(f'{name} restricted')
        elif embargo := _embargo(meta, today):
            problems.append(f'{name} {embargo}')
        elif algorithm == digest.partition(':')[0].lower():
            if f'{algorithm}:{str(checksum.get("value")).lower()}' != digest.lower():
                problems.append(f'{name} checksum differs')
        else:
            by_size.append(name)
    if by_size:
        try:
            zenodo = sizes(zenodo_doi)
        except Exception as exc:  # noqa: BLE001 -- a non-transient failure is a reason
            if problems or not is_transient(exc, cert_is_transient=False):
                return '; '.join([*problems, f'Zenodo {zenodo_doi} file sizes: {exc}'])
            raise Unreachable(f'Zenodo {zenodo_doi} file sizes: {exc}') from exc
        for name in by_size:
            size = files[name].get('filesize')
            if not isinstance(size, int) or size != zenodo.get(name):
                problems.append(f'{name} size differs from Zenodo')
    return '; '.join(problems) or None


def dataverse_server(doi: str) -> str:
    """Return the base URL of the Dataverse server that holds a DOI.

    A prefix in ``DATAVERSE_SERVERS`` names its server; any other DOI is resolved through the
    doi.org handle API, and the http(s) scheme, host and port of its landing page name the
    server.

    Raises
    ------
    Unreachable
        If doi.org fails in transit.
    UnknownServer
        If doi.org does not know the DOI or gives no landing page.
    """
    doi = doi.removeprefix('doi:')
    if server := DATAVERSE_SERVERS.get(doi.partition('/')[0]):
        return server
    try:
        response = requests.get(f'{DOI_HANDLES}/{doi}', timeout=30)
        response.raise_for_status()
        values = response.json()['values']
        url = urlparse(next(str(v['data']['value']) for v in values if v.get('type') == 'URL'))
        port = url.port  # raises ValueError for a malformed port
    except Exception as exc:
        if is_cert_failure(exc):
            raise
        if is_transient(exc):
            raise Unreachable(f'doi.org lookup of doi:{doi}: {exc}') from exc
        raise UnknownServer(f'doi.org gives no landing page for doi:{doi}: {exc!r}') from exc
    if url.scheme not in ('http', 'https') or not url.hostname or port == 0:
        raise UnknownServer(f'doi.org gives no landing page for doi:{doi}: {url.geturl()}')
    return f'{url.scheme}://{url.netloc.rpartition("@")[2]}'


def check_mirrors(
    dataverse_url: str | None = None, datasets: list[Dataset] | None = None
) -> MirrorReport:
    """Check every pinned dataset against its mirror and list the unpinned ones.

    Parameters
    ----------
    dataverse_url : str, optional
        Base URL of the Dataverse server for every pin; by default each pin is read from the
        server of its DOI (:func:`dataverse_server`), with one client per server.
    datasets : list of Dataset, optional
        Datasets to check; defaults to those of every installed manifest, and a manifest
        left out of discovery is reported in ``manifest_errors``.

    Returns
    -------
    MirrorReport
        Passed, failed and unreachable pins (with the reason), unpinned dataset keys, and a
        warning per pin whose read recovered from a certificate error.
    """
    report = MirrorReport()
    if datasets is None:
        discovery = _discover_all()
        datasets = [ds for group in discovery.found.values() for ds in group]
        report.manifest_errors.update(discovery.errors)
    clients: dict[str, DataverseClient] = {}
    outcomes: dict[str, dict[str, int] | Exception] = {}
    notes: list[str] = []
    cert_failed: set[str] = set()

    def cached_sizes(doi: str) -> dict[str, int]:
        """Return the sizes of a Zenodo DOI, or raise its failure, reading it once per run."""
        if doi not in outcomes:
            try:
                outcomes[doi] = _read(
                    lambda: zenodo_sizes(doi), f'Zenodo {doi}', notes, ZENODO_HOST, cert_failed
                )
            except Exception as exc:  # noqa: BLE001 -- a failure is read once, like a success
                outcomes[doi] = exc
        if isinstance(outcomes[doi], Exception):
            raise outcomes[doi].with_traceback(None)
        return outcomes[doi]

    for ds in datasets:
        if not ds.dataverse:
            report.unpinned.append(ds.key)
            continue
        notes = []
        try:
            server = dataverse_url or _read(
                lambda pin=ds.dataverse: dataverse_server(pin),
                f'doi.org lookup of {ds.dataverse}',
                notes,
                DOI_HOST,
                cert_failed,
            )
            if server not in clients:
                clients[server] = DataverseClient(server, token='')
            why = pin_problem(ds, clients[server], cached_sizes, notes, cert_failed)
        except Unreachable as exc:
            report.unreachable[ds.key] = str(exc)
            continue
        except UnknownServer as exc:
            why = str(exc)
        except Exception as exc:  # noqa: BLE001 -- one bad dataset must not stop the run
            why = f'{type(exc).__name__}: {exc}'
        finally:
            if notes:
                report.warnings[ds.key] = '; '.join(notes)
        if why is None:
            report.passed.append(ds.key)
        else:
            report.failed[ds.key] = why
    return report
