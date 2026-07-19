"""Registry generation from the Zenodo API (``fwl-io sync``).

For every dataset in a manifest, query the pinned Zenodo record and rewrite
the committed registry file with the record's file names and checksums.
This is the only place where hashes enter the repository, so registry
changes are always visible in review.

Concept DOIs are rejected here: a concept DOI resolves to the newest
deposit of a record, which would let the data change underneath pinned
code. Only version DOIs are accepted in manifests.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

from fwl_io.manifest import Dataset, load_manifest
from fwl_io.registry import write_registry

ZENODO_API = 'https://zenodo.org/api/records'
_ZENODO_DOI = re.compile(r'10\.5281/zenodo\.(\d+)$')


def zenodo_record_id(doi: str) -> str:
    """Extract the numeric record id from a Zenodo DOI."""
    match = _ZENODO_DOI.search(doi)
    if not match:
        raise ValueError(f'{doi!r} is not a Zenodo DOI of the form 10.5281/zenodo.<id>')
    return match.group(1)


def fetch_zenodo_registry(doi: str, api_base: str = ZENODO_API) -> dict[str, str]:
    """Return the name-to-checksum mapping of a pinned Zenodo record."""
    recid = zenodo_record_id(doi)
    response = requests.get(f'{api_base}/{recid}', timeout=30)
    response.raise_for_status()
    record = response.json()

    if str(record.get('conceptrecid')) == recid:
        raise ValueError(
            f'{doi} is a concept DOI (it resolves to the newest deposit); '
            f'pin the version DOI of a specific deposit instead'
        )

    files = record.get('files') or []
    if not files:
        raise ValueError(f'Zenodo record {recid} lists no files')
    return {entry['key']: entry['checksum'] for entry in files}


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
    """Regenerate the registries of every dataset in a manifest."""
    return [sync_dataset(ds, api_base=api_base) for ds in load_manifest(manifest_path)]
