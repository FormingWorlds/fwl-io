"""Shared data-download utilities for the PROTEUS ecosystem.

fwl-io provides manifest-driven, mirrored, offline-first fetching of the
reference data used by the FormingWorlds model ecosystem (PROTEUS and its
modules). Datasets are declared in TOML manifests, pinned to Zenodo version
DOIs with committed file registries, and downloaded into the shared FWL_DATA
directory tree with hash verification and atomic writes.
"""

from importlib.metadata import PackageNotFoundError, version

from fwl_io.fetch import DownloadError, Fetcher, OfflineDataError, create_fetcher
from fwl_io.manifest import (
    Dataset,
    ManifestSchemaError,
    discover_manifests,
    fetch_for,
    load_manifest,
)
from fwl_io.paths import MissingDataRootError, resolve_cache_root, resolve_data_root

try:
    __version__ = version('fwl-io')
except PackageNotFoundError:  # editable checkout without installed metadata
    __version__ = '0.0.0'

__all__ = [
    'Dataset',
    'DownloadError',
    'Fetcher',
    'ManifestSchemaError',
    'MissingDataRootError',
    'OfflineDataError',
    '__version__',
    'create_fetcher',
    'discover_manifests',
    'fetch_for',
    'load_manifest',
    'resolve_cache_root',
    'resolve_data_root',
]
