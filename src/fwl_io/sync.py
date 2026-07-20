"""Registry generation from the Zenodo API (``fwl-io sync``).

For every dataset in a manifest, query the pinned Zenodo record and rewrite
the committed registry file with the record's file names and checksums.
This is the only place where hashes enter the repository, so registry
changes are always visible in review.

Concept DOIs are rejected here: a concept DOI resolves to the newest
deposit of a record (the API answers with a redirect to the latest version,
so the returned record id differs from the requested one), which would let
the data change underneath pinned code. Only version DOIs are accepted.

All datasets of a manifest are attempted; per-dataset failures are
collected and raised together at the end, so one bad entry does not leave
the rest of the registry set unwritten.
"""

from __future__ import annotations

from pathlib import Path

import requests

from fwl_io.doi import zenodo_record_id
from fwl_io.manifest import Dataset, load_manifest
from fwl_io.registry import write_registry

# Re-exported for backwards compatibility; the parser now lives in manifest.
__all__ = ['fetch_zenodo_registry', 'sync_dataset', 'sync_manifest', 'zenodo_record_id']

ZENODO_API = 'https://zenodo.org/api/records'


def _extract_files(record: dict) -> dict[str, str]:
    """Return name-to-checksum entries from either Zenodo API files shape."""
    files = record.get('files')
    if isinstance(files, list):
        return {entry['key']: entry['checksum'] for entry in files}
    if isinstance(files, dict):  # InvenioRDM shape: files.entries = {name: {...}}
        entries = files.get('entries') or {}
        return {name: meta['checksum'] for name, meta in entries.items()}
    return {}


def fetch_zenodo_record(doi: str, api_base: str = ZENODO_API) -> dict:
    """Return the full Zenodo API record for a pinned version DOI.

    A concept DOI is rejected: the API resolves it to the newest deposit (the
    returned record id differs from the requested one, or equals the concept
    record id), which would let the data change underneath pinned code.
    """
    recid = zenodo_record_id(doi)
    response = requests.get(f'{api_base}/{recid}', timeout=30)
    response.raise_for_status()
    record = response.json()

    returned_id = str(record.get('id'))
    if returned_id != recid or str(record.get('conceptrecid')) == recid:
        raise ValueError(
            f'{doi} is a concept DOI (the API resolves it to the newest deposit, '
            f'record {returned_id}); pin the version DOI of a specific deposit instead'
        )
    return record


def fetch_zenodo_registry(doi: str, api_base: str = ZENODO_API) -> dict[str, str]:
    """Return the name-to-checksum mapping of a pinned Zenodo record."""
    record = fetch_zenodo_record(doi, api_base=api_base)
    entries = _extract_files(record)
    if not entries:
        raise ValueError(f'Zenodo record {zenodo_record_id(doi)} lists no files')
    return entries


def sync_dataset(dataset: Dataset, api_base: str = ZENODO_API) -> Path:
    """Regenerate the committed registry file for one dataset."""
    if not dataset.zenodo:
        raise ValueError(f'dataset {dataset.key!r} has no Zenodo DOI; cannot sync')
    if dataset.registry_path is None:
        raise ValueError(f'dataset {dataset.key!r} has no registry path')
    entries = fetch_zenodo_registry(dataset.zenodo, api_base=api_base)
    write_registry(dataset.registry_path, entries)
    return dataset.registry_path


def sync_manifest(manifest_path: str | Path, api_base: str = ZENODO_API) -> list[Path]:
    """Regenerate the registries of every dataset in a manifest.

    Every dataset is attempted; if any fail, the successful registries are
    still written and a single aggregated error is raised at the end.
    """
    written: list[Path] = []
    failures: dict[str, str] = {}
    for ds in load_manifest(manifest_path):
        try:
            written.append(sync_dataset(ds, api_base=api_base))
        except Exception as exc:  # noqa: BLE001 -- aggregate and re-raise below
            failures[ds.key] = str(exc)
    if failures:
        detail = '\n'.join(f'  {key}: {msg}' for key, msg in sorted(failures.items()))
        raise RuntimeError(
            f'{len(failures)} dataset(s) failed to sync '
            f'({len(written)} registries written):\n{detail}'
        )
    return written
