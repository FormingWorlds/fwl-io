"""Mirror a pinned Zenodo deposit to a Dataverse.nl collection.

Zenodo is the primary source of every dataset; Dataverse is a download
mirror used as the second link in the fetch fallback chain. :func:`mirror_to_dataverse`
takes a Zenodo version DOI, downloads and checksum-verifies its files, then
creates a matching Dataverse dataset, uploads the files byte-identically,
and (optionally) publishes it, printing the Dataverse DOI to add to the
consuming manifest. Called with ``publish=False``, it leaves the created
dataset as a private draft instead; :func:`publish_existing_dataverse_draft`
is the second step of that workflow, publishing an existing draft by its
persistent id without ever creating a dataset.

The Dataverse writes go through the native API
(https://guides.dataverse.org/en/latest/api/native-api.html):

- create dataset:  POST /api/dataverses/<collection>/datasets
- add file:        POST /api/datasets/:persistentId/add?persistentId=<pid>
- publish:         POST /api/datasets/:persistentId/actions/:publish
                        ?persistentId=<pid>&type=major

with the API token passed in the ``X-Dataverse-key`` header. Tabular ingest
is disabled on upload so the mirror is byte-identical to Zenodo. Each call
is isolated in :class:`DataverseClient` so the exact request shape is easy to
review and to point at a mock server in tests.
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import tempfile
import time
from collections import Counter
from pathlib import Path

import requests

from fwl_io.sync import ZENODO_API, fetch_zenodo_record, zenodo_record_id

log = logging.getLogger('fwl.' + __name__)

# Safeguard against tabular-file ingest on upload, so a mirrored tabular file
# is stored byte-for-byte rather than converted to Dataverse's tabular format.
# Dataverse only ingests recognised tabular extensions (.csv, .tsv, .xlsx,
# .sav, .por, .dta, .rdata); the PROTEUS data file types (.txt, .dat, .sf,
# .chk, .nc, .tar.gz) are not among them, so ingest is not expected to trigger.
# The exact query parameter name can vary by Dataverse version; confirm it
# against the target installation before relying on it for tabular content.
_NO_INGEST_PARAM = 'noVarDetect'


# Retry of a native-API call hit by the bot-check page, a gateway error, a 429 or a
# lost connection: 30, 60, 120, 240 s between 5 attempts, or a 429's Retry-After (<= 300 s).
MAX_ATTEMPTS = 5
BACKOFF_S = 30.0
BACKOFF_CAP_S = 300.0
_RETRY_STATUSES = (429, 502, 503, 504)
_CHALLENGE_MARKERS = ('Oh noes!', '/.within.website/')
_sleep = time.sleep


class DataverseError(RuntimeError):
    """A Dataverse native-API request failed."""

    def __init__(self, message: str, status_code: int | None = None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class DataverseRetryableError(DataverseError):
    """A Dataverse call hit the bot-check page, a gateway error, a 429 or a lost connection.

    ``unprocessed`` is True when the reply shows that Dataverse did not process
    the request (the bot-check page at a status below 500, or a 429), so a
    resend cannot repeat it.
    """

    def __init__(
        self, message: str, status_code: int | None = None, retry_after=None, unprocessed=False
    ):
        super().__init__(message, status_code)
        self.retry_after = retry_after
        self.unprocessed = unprocessed


def _wait(attempt: int, retry_after=None) -> float:
    """Return the wait after ``attempt``: 30 s doubling, or a Retry-After, at most 300 s."""
    return min(
        BACKOFF_S * 2 ** (attempt - 1) if retry_after is None else retry_after, BACKOFF_CAP_S
    )


def _cert_failure(exc: BaseException) -> bool:
    """Return whether a certificate verification failure is anywhere in the exception chain."""
    todo, seen = [exc], set()
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        links = (e.__cause__, e.__context__, getattr(e, 'reason', None), *e.args)
        todo += [x for x in links if isinstance(x, BaseException)]
    return False


class DataversePublishUnconfirmed(DataverseError):
    """A publish request was sent, but whether it took effect is not known."""


class DataverseAlreadyPublished(DataverseError):
    """The dataset was already published before the publish request."""


def _primitive(type_name: str, value: str, *, multiple: bool = False) -> dict:
    """Wrap a scalar as a Dataverse ``primitive`` field.

    The native API requires each field, and each sub-field of a compound
    field, to declare its ``typeClass`` and ``multiple`` flag next to the
    value; a field carrying only a name and value is rejected server-side.
    """
    return {'typeName': type_name, 'typeClass': 'primitive', 'multiple': multiple, 'value': value}


def _compound(type_name: str, value: list[dict], *, multiple: bool = True) -> dict:
    """Wrap compound entries (each a dict of primitive sub-fields) as a field.

    Every field builder sets ``typeClass`` and ``multiple`` so no citation
    field can be assembled without them; hand-building a raw dict is what let
    the required attributes go missing.
    """
    return {'typeName': type_name, 'typeClass': 'compound', 'multiple': multiple, 'value': value}


def _controlled(type_name: str, values: list[str], *, multiple: bool = True) -> dict:
    """Wrap controlled-vocabulary values as a Dataverse ``controlledVocabulary`` field."""
    return {
        'typeName': type_name,
        'typeClass': 'controlledVocabulary',
        'multiple': multiple,
        'value': values,
    }


def _creators_to_authors(creators: list[dict]) -> list[dict]:
    """Map Zenodo record creators onto Dataverse compound author fields."""
    authors = []
    for creator in creators or []:
        author = {'authorName': _primitive('authorName', creator.get('name', 'Unknown'))}
        affiliation = creator.get('affiliation')
        if affiliation:
            author['authorAffiliation'] = _primitive('authorAffiliation', affiliation)
        authors.append(author)
    return authors or [{'authorName': _primitive('authorName', 'Unknown')}]


def zenodo_record_to_citation(
    record: dict,
    *,
    contact_name: str,
    contact_email: str,
    subject: str,
) -> dict:
    """Build the Dataverse ``datasetVersion`` JSON from a Zenodo record.

    The title, authors, and description are taken from the Zenodo record so
    the mirror is a faithful copy; the description records the source DOI so a
    reader can trace the mirror back to its primary. ``subject`` is a Dataverse
    citation subject; the target server validates it at create time, not here.
    """
    metadata = record.get('metadata', {})
    doi = record.get('doi') or metadata.get('doi', '')
    title = metadata.get('title') or f'Zenodo record {record.get("id")}'
    description = metadata.get('description') or title
    source_note = f'Mirror of Zenodo deposit {doi}. Zenodo is the primary source.'

    contact = {
        'datasetContactName': _primitive('datasetContactName', contact_name),
        'datasetContactEmail': _primitive('datasetContactEmail', contact_email),
    }

    fields = [
        _primitive('title', title),
        _compound('author', _creators_to_authors(metadata.get('creators', []))),
        _compound('datasetContact', [contact]),
        _compound(
            'dsDescription',
            [
                {'dsDescriptionValue': _primitive('dsDescriptionValue', description)},
                {'dsDescriptionValue': _primitive('dsDescriptionValue', source_note)},
            ],
        ),
        _controlled('subject', [subject]),
    ]
    return {'datasetVersion': {'metadataBlocks': {'citation': {'fields': fields}}}}


_ALGORITHMS = {'MD5': 'md5', 'SHA-1': 'sha1', 'SHA-256': 'sha256', 'SHA-512': 'sha512'}


def zenodo_record_rights(zenodo_doi: str, *, api_base: str = ZENODO_API) -> dict:
    """Return the single license entry of a Zenodo record (InvenioRDM ``rights``).

    The InvenioRDM form gives the SPDX id and the license URL; the legacy
    record JSON gives CC0 as ``cc-zero``, which no license list knows.

    Raises
    ------
    ValueError
        If the record lists no license or more than one.
    """
    recid = zenodo_record_id(zenodo_doi)
    response = requests.get(
        f'{api_base}/{recid}',
        headers={'Accept': 'application/vnd.inveniordm.v1+json'},
        timeout=30,
    )
    response.raise_for_status()
    rights = (response.json().get('metadata') or {}).get('rights') or []
    if len(rights) != 1:
        raise ValueError(
            f'Zenodo record {recid} lists {len(rights)} licenses; a mirror copies exactly one'
        )
    return rights[0]


def _license_key(uri) -> str:
    """Return a license URL without scheme, ``www.``, trailing ``/legalcode`` or slash."""
    key = str(uri or '').lower().split('://', 1)[-1].removeprefix('www.').rstrip('/')
    return key.removesuffix('/legalcode').rstrip('/')


def dataverse_license(rights: dict, licenses: list[dict], source: str) -> dict:
    """Return the one active Dataverse license that matches a Zenodo license entry.

    A license matches on its URL or on its SPDX id (the Dataverse name or
    rightsIdentifier); there is no default.

    Raises
    ------
    ValueError
        If no active Dataverse license matches, or more than one does.
    """
    spdx = str(rights.get('id') or '').lower()
    url = _license_key((rights.get('props') or {}).get('url'))
    ids = lambda lic: {str(lic.get(k) or '').lower() for k in ('name', 'rightsIdentifier')}  # noqa: E731
    hits = [
        lic
        for lic in licenses
        if lic.get('active', True)
        and ((url and _license_key(lic.get('uri')) == url) or (spdx and spdx in ids(lic)))
    ]
    if len({lic.get('name') for lic in hits}) != 1:
        raise ValueError(
            f'{source} has license {rights.get("id")!r} ({url or "no URL"}), which matches '
            f'{sorted(lic.get("name") for lic in hits) or "no license"} on the Dataverse server; '
            'the mirror needs exactly one'
        )
    return hits[0]


def _draft_path(directory_label, filename) -> str:
    """Return the path of a draft file; the mirror uploads every file with no folder label."""
    return '/'.join(filter(None, (directory_label, filename)))


def _same_file(entry: dict, path: Path) -> bool:
    """Return whether a Dataverse file entry has the size and checksum of ``path``.

    An entry without a checksum of a known type does not count as the same file.
    """
    checksum = entry.get('checksum') or {}
    algorithm = _ALGORITHMS.get(checksum.get('type'))
    if algorithm is None or entry.get('filesize') != path.stat().st_size:
        return False
    with path.open('rb') as handle:
        return (
            hashlib.file_digest(handle, algorithm).hexdigest() == str(checksum.get('value')).lower()
        )


class DataverseClient:
    """Thin client over the Dataverse native API for the mirror operations."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 120):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {'X-Dataverse-key': self.token}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Call one Dataverse native-API endpoint and return its decoded body.

        Parameters
        ----------
        method : str
            HTTP method, e.g. ``'POST'`` or ``'DELETE'``.
        path : str
            API path relative to ``self.base_url``, e.g.
            ``'/api/datasets/:persistentId'``.
        **kwargs
            Passed through to :func:`requests.request` (``params``, ``json``,
            ``files``, and so on).

        Returns
        -------
        dict
            The decoded JSON body, or ``{}`` for a 2xx response with an empty
            body (routine for a DELETE call).

        Raises
        ------
        DataverseRetryableError
            If the response is the DataverseNL bot-check page (text/html with
            its "Oh noes!" marker), a 429 or a 502, 503 or 504 gateway error, or
            the connection fails, times out or breaks mid-body, or a TLS error
            occurs that is not a certificate verification failure.
        DataverseError
            If another transport error occurs, another status is 400 or
            higher, or a non-empty successful body fails to parse as JSON or
            parses to something other than a JSON object.
        """
        try:
            response = requests.request(
                method,
                f'{self.base_url}{path}',
                headers=self._headers,
                timeout=self.timeout,
                **kwargs,
            )
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            # The request may have reached Dataverse, like a gateway error; a
            # certificate failure is final (SSLError is a ConnectionError).
            cls = DataverseError if _cert_failure(exc) else DataverseRetryableError
            raise cls(f'Dataverse {method} {path} failed: {exc}') from exc
        except requests.RequestException as exc:
            raise DataverseError(f'Dataverse {method} {path} failed: {exc}') from exc
        if 'text/html' in response.headers.get('Content-Type', '').lower() and any(
            marker in response.text for marker in _CHALLENGE_MARKERS
        ):
            raise DataverseRetryableError(
                f'Dataverse {method} {path} returned its bot-check page '
                f'({response.status_code}, text/html, "Oh noes!") instead of an API response',
                response.status_code,
                unprocessed=response.status_code < 500,
            )
        if response.status_code in _RETRY_STATUSES:
            wait = response.headers.get('Retry-After', '') if response.status_code == 429 else ''
            raise DataverseRetryableError(
                f'Dataverse {method} {path} failed ({response.status_code}): {response.text[:500]}',
                response.status_code,
                float(wait) if wait.isdecimal() else None,
                unprocessed=response.status_code == 429,
            )
        if not response.ok:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise DataverseError(
                f'Dataverse {method} {path} failed ({response.status_code}): {response.text[:500]}',
                response.status_code,
                body,
            )
        if not response.content:
            # A 2xx with an empty body (routine for a DELETE) is a success
            # with nothing to parse.
            return {}
        try:
            body = response.json()
        except ValueError:
            raise DataverseError(
                f'Dataverse {method} {path} returned {response.status_code} with a '
                f'non-JSON body: {response.text[:500]}'
            ) from None
        if not isinstance(body, dict):
            raise DataverseError(
                f'Dataverse {method} {path} returned {response.status_code} with a '
                f'non-object JSON body: {response.text[:500]}'
            )
        return body

    def _post(self, path: str, **kwargs) -> dict:
        return self._request('POST', path, **kwargs)

    def _retry(self, call, what: str, done=None):
        """Run ``call`` up to MAX_ATTEMPTS times while it raises DataverseRetryableError.

        That error stands for the bot-check page, a 429 (its Retry-After is
        used as the wait), a gateway error, or a connection error or timeout.
        Any other error, from ``call`` or ``done``, passes through at once.

        Before each repeat, ``done()`` (when given) tells whether the earlier
        attempt took effect on the server after all; the call is then not
        repeated. The waits are 30, 60, 120 and 240 s, or a 429's Retry-After
        capped at 300 s: at most 1200 s of sleep, plus up to MAX_ATTEMPTS
        request timeouts, for one call that never succeeds. The mirror workflow
        has no job timeout below GitHub's 6 h.

        Raises
        ------
        DataverseRetryableError
            If every attempt failed that way; the message names the call, how
            many times it was sent, and the last error.
        """
        sent = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if attempt > 1 and done is not None and done():
                    return None
                sent += 1
                return call()
            except DataverseRetryableError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise DataverseRetryableError(
                        f'{what} failed: sent {sent} time(s) in {MAX_ATTEMPTS} attempts; '
                        f'last: {exc}',
                        exc.status_code,
                    ) from exc
                delay = _wait(attempt, exc.retry_after)
                log.warning(
                    '%s (attempt %d of %d): %s; retrying in %.0f s',
                    what,
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                _sleep(delay)

    def _draft_files(self, persistent_id: str) -> dict:
        """Return the draft's file metadata keyed by path (folder label, file name).

        Raises
        ------
        DataverseError
            If the draft lists a path more than once.
        """
        body = self._request(
            'GET',
            '/api/datasets/:persistentId/versions/:draft/files',
            params={'persistentId': persistent_id},
        )
        entries = body.get('data') or []
        files = [entry.get('dataFile') or {} for entry in entries]
        keys = [
            _draft_path(e.get('directoryLabel') or f.get('directoryLabel'), f.get('filename'))
            for e, f in zip(entries, files, strict=True)
        ]
        dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
        if dupes:
            raise DataverseError(f'draft {persistent_id} lists {dupes} more than once')
        return dict(zip(keys, files, strict=True))

    def _file_arrived(self, persistent_id: str, path: Path) -> bool:
        """Return whether ``path`` is already in the draft, byte for byte.

        Raises
        ------
        DataverseError
            If the draft holds a different file of the same name.
        """
        entry = self._draft_files(persistent_id).get(_draft_path(None, path.name))
        if entry is None:
            return False
        if not _same_file(entry, path):
            raise DataverseError(
                f'draft {persistent_id} holds a file named {path.name} that is not the same '
                'file (size or checksum differs, or the checksum type is unknown)'
            )
        log.info(
            '%s arrived in %s despite the failed response; not sending it again',
            path.name,
            persistent_id,
        )
        return True

    def check_draft(self, persistent_id: str, files: dict[str, Path]) -> None:
        """Check that the draft holds exactly ``files``, each with its size and checksum.

        Raises
        ------
        DataverseError
            If a file is missing or differs, or the draft holds another file.
        """
        listed = self._retry(
            lambda: self._draft_files(persistent_id), f'file listing of {persistent_id}'
        )
        wrong = sorted(n for n, p in files.items() if not _same_file(listed.get(n, {}), p))
        extra = sorted(set(listed) - set(files))
        if wrong or extra:
            kinds = sorted({str((f.get('checksum') or {}).get('type')) for f in listed.values()})
            raise DataverseError(
                f'draft {persistent_id} does not match the Zenodo files: '
                f'missing or different {wrong}, not expected {extra} '
                f'(checksum types listed: {kinds})'
            )

    def _released(self, persistent_id: str) -> bool:
        """Return whether the dataset's latest version is published."""
        body = self._request(
            'GET', '/api/datasets/:persistentId', params={'persistentId': persistent_id}
        )
        version = (body.get('data') or {}).get('latestVersion') or {}
        return version.get('versionState') == 'RELEASED'

    def _deleted(self, persistent_id: str) -> bool:
        """Return whether the dataset is gone: a 404 with a Dataverse JSON error body.

        A 404 page from a proxy or the bot-check page does not count.
        """
        try:
            self._request(
                'GET', '/api/datasets/:persistentId', params={'persistentId': persistent_id}
            )
        except DataverseError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            if exc.status_code == 404 and body.get('status') == 'ERROR':
                return True
            raise
        return False

    def create_dataset(self, collection: str, metadata: dict) -> str:
        """Create a draft dataset in ``collection``; return its persistent id."""
        body = self._post(f'/api/dataverses/{collection}/datasets', json=metadata)
        data = body.get('data', {})
        persistent_id = data.get('persistentId')
        if not persistent_id:
            raise DataverseError(f'create dataset returned no persistentId: {body}')
        return persistent_id

    def licenses(self) -> list[dict]:
        """Return the licenses the server lists (GET /api/licenses)."""
        body = self._retry(lambda: self._request('GET', '/api/licenses'), 'license list')
        return body.get('data') or []

    def set_license(self, persistent_id: str, license: dict) -> None:
        """Set a listed license on a draft and read it back.

        Raises
        ------
        DataverseError
            If the draft does not report the license afterwards.
        """

        def dataset():
            body = self._retry(
                lambda: self._request(
                    'GET', '/api/datasets/:persistentId', params={'persistentId': persistent_id}
                ),
                f'state of {persistent_id}',
            )
            return body.get('data') or {}

        dataset_id = dataset().get('id')
        self._retry(
            lambda: self._request(
                'PUT', f'/api/datasets/{dataset_id}/license', json={'name': license['name']}
            ),
            f'license of {persistent_id}',
        )
        got = (dataset().get('latestVersion') or {}).get('license') or {}
        if (got.get('name'), got.get('uri')) != (license['name'], license.get('uri')):
            raise DataverseError(
                f'draft {persistent_id} reports license {got} after {license["name"]} was set'
            )

    def add_file(self, persistent_id: str, path: Path, *, no_ingest: bool = True) -> None:
        """Upload one file to a dataset, with tabular ingest disabled by default."""
        params = {'persistentId': persistent_id}
        if no_ingest:
            params[_NO_INGEST_PARAM] = 'true'

        def send():
            with path.open('rb') as handle:
                self._post(
                    '/api/datasets/:persistentId/add',
                    params=params,
                    files={'file': (path.name, handle, 'application/octet-stream')},
                )

        self._retry(
            send,
            f'upload of {path.name} to {persistent_id}',
            done=lambda: self._file_arrived(persistent_id, path),
        )

    def publish(self, persistent_id: str, *, version_type: str = 'major') -> None:
        """Publish a dataset, making its files publicly downloadable.

        The request is sent again only after a reply that shows Dataverse did
        not process it (the bot-check page below status 500, or a 429). After
        any other outcome, including a 2xx, the dataset state is polled until
        it is RELEASED.

        Raises
        ------
        DataverseAlreadyPublished
            If the dataset is already published before the call.
        DataverseError
            If Dataverse rejects the publish request with a 4xx status.
        DataversePublishUnconfirmed
            If the dataset is not RELEASED after the wait, or every attempt got
            the bot-check page or a 429; its state is then unknown.
        """
        if self._retry(lambda: self._released(persistent_id), f'state check of {persistent_id}'):
            raise DataverseAlreadyPublished(f'{persistent_id} is already published')
        what = f'publish of {persistent_id}'
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if attempt > 1 and self._released(persistent_id):
                    return
            except Exception as exc:  # noqa: BLE001 -- a failed check is not RELEASED
                log.warning('state check of %s failed: %s', persistent_id, exc)
            try:
                reply = self._post(
                    '/api/datasets/:persistentId/actions/:publish',
                    params={'persistentId': persistent_id, 'type': version_type},
                )
            except DataverseRetryableError as exc:
                if not exc.unprocessed:
                    return self._await_release(persistent_id, attempt, exc)
                if attempt == MAX_ATTEMPTS:
                    raise DataversePublishUnconfirmed(
                        f'{what} not confirmed: attempted {attempt} time(s), each answered with '
                        f'the bot-check page or a 429; last: {exc}',
                        exc.status_code,
                    ) from exc
                delay = _wait(attempt, exc.retry_after)
                log.warning(
                    '%s (attempt %d of %d): %s; retrying in %.0f s',
                    what,
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                _sleep(delay)
            except Exception as exc:
                if isinstance(exc, DataverseError) and 400 <= (exc.status_code or 0) < 500:
                    raise
                return self._await_release(persistent_id, attempt, exc)
            else:
                return self._await_release(persistent_id, attempt, f'accepted ({reply})')

    def _await_release(self, persistent_id: str, sent: int, outcome) -> None:
        """Poll the dataset state after a publish request until it is RELEASED.

        Raises
        ------
        DataversePublishUnconfirmed
            If the dataset is not RELEASED after MAX_ATTEMPTS checks.
        """
        waited, last = 0.0, 'not RELEASED'
        for check in range(1, MAX_ATTEMPTS + 1):
            if check > 1:
                delay = _wait(check - 1)
                log.warning(
                    'publish of %s not RELEASED yet (check %d of %d); checking again in %.0f s',
                    persistent_id,
                    check - 1,
                    MAX_ATTEMPTS,
                    delay,
                )
                _sleep(delay)
                waited += delay
            try:
                if self._released(persistent_id):
                    return
                last = 'not RELEASED'
            except Exception as exc:  # noqa: BLE001 -- a failed check is not RELEASED
                log.warning('state check of %s failed: %s', persistent_id, exc)
                last = exc
        raise DataversePublishUnconfirmed(
            f'publish of {persistent_id} not confirmed (request attempted {sent} time(s)); '
            f'not RELEASED after {waited:.0f} s; reply: {outcome}; last state check: {last}',
            getattr(outcome, 'status_code', None),
        ) from (outcome if isinstance(outcome, BaseException) else None)

    def delete_draft(self, persistent_id: str) -> None:
        """Delete an unpublished draft dataset (used to roll back a failed mirror)."""
        self._retry(
            lambda: self._request(
                'DELETE',
                '/api/datasets/:persistentId',
                params={'persistentId': persistent_id},
            ),
            f'deletion of draft {persistent_id}',
            done=lambda: self._deleted(persistent_id),
        )


def _download_zenodo_files(
    zenodo_doi: str,
    registry: dict[str, str],
    dest_root: Path,
    *,
    base_urls: list[str] | None = None,
) -> dict[str, Path]:
    """Download and checksum-verify every registry file into ``dest_root``.

    Reuses the package fetcher so the download is hash-verified and atomic;
    files are selected by registry name, so the provenance stamp the fetcher
    writes is not mistaken for dataset content.
    """
    from fwl_io.fetch import create_fetcher

    fetcher = create_fetcher(
        subdir='_mirror_staging',
        zenodo=zenodo_doi,
        registry=registry,
        base_urls=base_urls,
        data_root=dest_root,
    )
    fetcher.fetch_all()
    return {name: fetcher.target_dir / name for name in registry}


def mirror_to_dataverse(
    zenodo_doi: str,
    *,
    dataverse_url: str,
    collection: str,
    token: str,
    contact_name: str,
    contact_email: str,
    subject: str = 'Astronomy and Astrophysics',
    publish: bool = True,
    dry_run: bool = False,
    api_base: str = ZENODO_API,
    base_urls: list[str] | None = None,
    files: list[str] | tuple[str, ...] | None = None,
) -> str | None:
    """Mirror a pinned Zenodo deposit to Dataverse; return the Dataverse DOI.

    Parameters
    ----------
    zenodo_doi : str
        Zenodo version DOI of the deposit to mirror (concept DOIs are rejected).
    dataverse_url : str
        Base URL of the Dataverse installation (for example
        ``https://dataverse.nl``).
    collection : str
        Alias of the target Dataverse collection.
    token : str
        Dataverse API token.
    contact_name, contact_email : str
        Dataset contact recorded in the Dataverse citation metadata. A contact
        email is required for any real create (draft or published); only a dry
        run is exempt.
    subject : str
        A Dataverse citation subject. The value is validated by the server when
        the dataset is created; a value outside the target installation's
        controlled vocabulary is rejected there, not locally.
    publish : bool
        Publish the created dataset so its files are downloadable.
    dry_run : bool
        Do everything except the Dataverse writes; return None. Lets a first
        run confirm the Zenodo side and the metadata mapping without touching
        Dataverse.
    api_base : str
        Zenodo API base (overridden in tests).
    base_urls : list[str] | None
        Direct download URLs for the Zenodo files, tried before the DOI
        resolver (used in tests).
    files : list[str] | tuple[str, ...] | None
        File names of the record to mirror, matching the ``files`` list of the
        consuming manifest dataset. ``None`` mirrors the whole record.

    Returns
    -------
    str | None
        The Dataverse persistent id (DOI) of the mirror, or None on a dry run.

    Raises
    ------
    ValueError
        If ``zenodo_doi`` is malformed or is a concept DOI (a version DOI is
        required), if a real create is requested without a contact email, if the
        Zenodo record lists no files, if ``files`` names a file the record does
        not contain or selects none of them, or if a file name nests below the
        dataset directory (Dataverse flattens on the basename, so it would
        collide), or if the record lists no license or several, or its license
        matches no license the Dataverse server lists, or more than one; all
        before the draft is created.
    DataverseError
        If a Dataverse native-API request fails: the server rejects it (for
        example an unknown subject in the citation metadata), the HTTP transport
        fails (connection error or timeout), or a 2xx response body is not a
        JSON object (a non-empty body that fails to parse, or that parses to
        something other than a JSON object), or the draft does not hold
        exactly the Zenodo files. After a failure past the dataset creation the
        rollback deletes the draft; if that fails too, the log names the draft
        to delete by hand. A failed creation leaves no draft to roll back, but a
        creation whose reply was lost can leave one in the collection.
    DataverseAlreadyPublished
        If the dataset is already published before the publish request; it is kept.
    DataversePublishUnconfirmed
        If the dataset is not RELEASED after the wait that follows a publish
        request, or every attempt got the bot-check page or a 429; the dataset
        is kept, since it may be public already.
    DownloadError
        If a Zenodo file fails its checksum or cannot be downloaded; raised by
        the fetcher (``fwl_io.fetch``) before any Dataverse write.
    requests.RequestException
        If the Zenodo record itself cannot be fetched, for example an HTTP 404
        for a valid-format but nonexistent version DOI, or a network failure;
        propagated from ``fetch_zenodo_record`` before any Dataverse write.
        (Dataverse-side request failures, including an unparseable response body,
        are wrapped as DataverseError.)
    """
    # Dataverse requires a point-of-contact email on every dataset, so any real
    # create (draft or published) needs one; a dry run writes nothing and is exempt.
    # The subject is validated by the server when the dataset is created: an
    # unknown value fails the create there rather than being checked locally, so
    # the server stays authoritative across installations and vocabulary changes.
    if not dry_run and not contact_email:
        raise ValueError(
            'a contact email is required to create a Dataverse dataset; provide one '
            '(--contact-email / contact_email=...) or preview without writing '
            '(--dry-run / dry_run=True)'
        )

    recid = zenodo_record_id(zenodo_doi)
    record = fetch_zenodo_record(zenodo_doi, api_base=api_base)
    from fwl_io.sync import _extract_files

    registry = _extract_files(record)
    if not registry:
        raise ValueError(f'Zenodo record {recid} lists no files; nothing to mirror')
    if files is not None:
        from fwl_io.sync import select_files

        registry = select_files(registry, files, source=f'Zenodo record {recid}')
        if not registry:
            raise ValueError(
                f'the "files" list for Zenodo record {recid} selects no files; nothing to mirror'
            )
    # A registry name may nest below the dataset directory. Dataverse's file API
    # keys on the basename, so a nested name would flatten and could collide;
    # refuse it loudly rather than mirror a different layout than Zenodo.
    nested = sorted(name for name in registry if '/' in name)
    if nested:
        raise ValueError(
            f'cannot mirror nested file names to Dataverse (they would flatten): {nested}'
        )

    metadata = zenodo_record_to_citation(
        record, contact_name=contact_name, contact_email=contact_email, subject=subject
    )
    rights = zenodo_record_rights(zenodo_doi, api_base=api_base)
    log.info('Zenodo record %s license: %s', recid, rights.get('id'))
    client = None if dry_run else DataverseClient(dataverse_url, token)
    if client is not None:
        license = dataverse_license(rights, client.licenses(), f'Zenodo record {recid}')

    with tempfile.TemporaryDirectory(prefix='fwl-io-mirror-') as tmp:
        files = _download_zenodo_files(zenodo_doi, registry, Path(tmp), base_urls=base_urls)
        log.info('downloaded %d file(s) from Zenodo record %s', len(files), recid)

        if dry_run:
            log.info('dry run: skipping Dataverse create/upload/publish for %s', recid)
            return None

        persistent_id = client.create_dataset(collection, metadata)
        log.warning('created Dataverse dataset %s', persistent_id)
        # From here the draft exists with a real DOI: on a failure, delete it so a
        # failed run leaves no orphaned deposit, unless a publish may have happened.
        try:
            client.set_license(persistent_id, license)
            for name in sorted(files):
                client.add_file(persistent_id, files[name])
                log.info('uploaded %s', name)
            client.check_draft(persistent_id, files)
            if publish:
                client.publish(persistent_id)
                log.info('published %s', persistent_id)
        except (DataversePublishUnconfirmed, DataverseAlreadyPublished) as exc:
            # The dataset may be published already, so it is not deleted.
            log.error('%s; %s is kept, check its state by hand', exc, persistent_id)
            raise
        except Exception:
            try:
                client.delete_draft(persistent_id)
                log.warning('rolled back the draft dataset %s after a failed mirror', persistent_id)
            except Exception as cleanup_exc:  # noqa: BLE001 -- surface, do not mask the original
                log.error(
                    'could not roll back draft %s (delete it manually): %s',
                    persistent_id,
                    cleanup_exc,
                )
            except BaseException:
                log.error('rollback of %s interrupted; check it by hand', persistent_id)
                raise
            raise
        except BaseException:
            log.error(
                'mirror of %s interrupted; the dataset is kept, check its state by hand',
                persistent_id,
            )
            raise
        return persistent_id


def publish_existing_dataverse_draft(
    persistent_id: str,
    *,
    dataverse_url: str,
    token: str,
    version_type: str = 'major',
) -> None:
    """Publish an existing Dataverse draft dataset by its persistent id.

    This never calls :meth:`DataverseClient.create_dataset`, so it cannot
    mint a duplicate dataset: it is the second step of a create-draft ->
    review -> publish workflow, run once the draft created by
    :func:`mirror_to_dataverse` (with ``publish=False``) has been reviewed.

    Parameters
    ----------
    persistent_id : str
        Persistent id (DOI) of the existing draft, for example
        ``'doi:10.34894/EXAMPLE'``.
    dataverse_url : str
        Base URL of the Dataverse installation (for example
        ``https://dataverse.nl``).
    token : str
        Dataverse API token.
    version_type : str
        Dataverse publish version bump: ``'major'`` or ``'minor'``.

    Raises
    ------
    ValueError
        If ``persistent_id`` is not of the form ``'doi:<prefix>/<suffix>'``,
        or ``version_type`` is not ``'major'`` or ``'minor'``.
    DataverseAlreadyPublished
        If the dataset is already published before the publish request.
    DataverseError
        If the dataset does not exist or Dataverse rejects the publish request
        with a 4xx status.
    DataversePublishUnconfirmed
        If the dataset is not RELEASED after the wait that follows the publish
        request, or every attempt got the bot-check page or a 429; check the
        dataset's state before trying again.
    """
    if version_type not in ('major', 'minor'):
        raise ValueError(
            f"{version_type!r} is not a valid Dataverse version type: use 'major' or 'minor'"
        )
    prefix, sep, suffix = persistent_id.removeprefix('doi:').partition('/')
    if (
        not persistent_id.startswith('doi:')
        or not sep
        or not prefix
        or not suffix
        or any(ch.isspace() for ch in persistent_id)
    ):
        raise ValueError(
            f'{persistent_id!r} is not a Dataverse persistent id of the form '
            "'doi:<prefix>/<suffix>'"
        )
    client = DataverseClient(dataverse_url, token)
    client.publish(persistent_id, version_type=version_type)
    log.info('published %s', persistent_id)
