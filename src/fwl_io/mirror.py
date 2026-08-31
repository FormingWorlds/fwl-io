"""Mirror a pinned Zenodo deposit to a Dataverse.nl collection.

Zenodo is the primary source of every dataset; Dataverse is a download
mirror used as the second link in the fetch fallback chain. This module
takes a Zenodo version DOI, downloads and checksum-verifies its files, then
creates a matching Dataverse dataset, uploads the files byte-identically,
and (optionally) publishes it, printing the Dataverse DOI to add to the
consuming manifest.

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

import logging
import tempfile
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


class DataverseError(RuntimeError):
    """A Dataverse native-API request failed."""


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
        try:
            response = requests.request(
                method,
                f'{self.base_url}{path}',
                headers=self._headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            # A transport failure (connection error, timeout, DNS) is a failed
            # native-API request too; surface it as a DataverseError so every
            # Dataverse-side failure is one error type for callers to catch.
            raise DataverseError(f'Dataverse {method} {path} failed: {exc}') from exc
        if not response.ok:
            raise DataverseError(
                f'Dataverse {method} {path} failed ({response.status_code}): {response.text[:500]}'
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

    def create_dataset(self, collection: str, metadata: dict) -> str:
        """Create a draft dataset in ``collection``; return its persistent id."""
        body = self._post(f'/api/dataverses/{collection}/datasets', json=metadata)
        data = body.get('data', {})
        persistent_id = data.get('persistentId')
        if not persistent_id:
            raise DataverseError(f'create dataset returned no persistentId: {body}')
        return persistent_id

    def add_file(self, persistent_id: str, path: Path, *, no_ingest: bool = True) -> None:
        """Upload one file to a dataset, with tabular ingest disabled by default."""
        params = {'persistentId': persistent_id}
        if no_ingest:
            params[_NO_INGEST_PARAM] = 'true'
        with path.open('rb') as handle:
            self._post(
                '/api/datasets/:persistentId/add',
                params=params,
                files={'file': (path.name, handle, 'application/octet-stream')},
            )

    def publish(self, persistent_id: str, *, version_type: str = 'major') -> None:
        """Publish a dataset, making its files publicly downloadable."""
        self._post(
            '/api/datasets/:persistentId/actions/:publish',
            params={'persistentId': persistent_id, 'type': version_type},
        )

    def delete_draft(self, persistent_id: str) -> None:
        """Delete an unpublished draft dataset (used to roll back a failed mirror)."""
        self._request(
            'DELETE',
            '/api/datasets/:persistentId',
            params={'persistentId': persistent_id},
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

    Returns
    -------
    str | None
        The Dataverse persistent id (DOI) of the mirror, or None on a dry run.

    Raises
    ------
    ValueError
        If ``zenodo_doi`` is malformed or is a concept DOI (a version DOI is
        required), if a real create is requested without a contact email, if the
        Zenodo record lists no files, or if a file name nests below the dataset
        directory (Dataverse flattens on the basename, so it would collide).
    DataverseError
        If a Dataverse native-API request fails: the server rejects it (for
        example an unknown subject in the citation metadata), the HTTP transport
        fails (connection error or timeout), or a 2xx response body is not a
        JSON object (a non-empty body that fails to parse, or that parses to
        something other than a JSON object). A failure during upload or publish
        can leave a draft that the rollback then tries to delete.
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

    with tempfile.TemporaryDirectory(prefix='fwl-io-mirror-') as tmp:
        files = _download_zenodo_files(zenodo_doi, registry, Path(tmp), base_urls=base_urls)
        log.info('downloaded %d file(s) from Zenodo record %s', len(files), recid)

        if dry_run:
            log.info('dry run: skipping Dataverse create/upload/publish for %s', recid)
            return None

        client = DataverseClient(dataverse_url, token)
        persistent_id = client.create_dataset(collection, metadata)
        log.info('created Dataverse dataset %s', persistent_id)
        # From here the draft exists with a real DOI: on any failure before
        # publish, delete it so a failed run leaves no orphaned deposit behind.
        try:
            for name in sorted(files):
                client.add_file(persistent_id, files[name])
                log.info('uploaded %s', name)
            if publish:
                client.publish(persistent_id)
                log.info('published %s', persistent_id)
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
            raise
        return persistent_id
