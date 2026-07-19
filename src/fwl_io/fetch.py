"""Hash-verified, mirrored, offline-first file fetching.

The fetch order for every file is:

1. The file already exists under the data root with a matching checksum.
2. The file exists in the read-only shared cache (``FWL_DATA_CACHE``) with a
   matching checksum and is copied into the data root atomically.
3. Offline mode is active: raise ``OfflineDataError``.
4. Download from each mirror in turn (Zenodo first, then Dataverse), verify
   the checksum, and move the file into place atomically.

Downloads are performed by pooch, which resolves ``doi:`` URLs for Zenodo
and Dataverse records natively. Files are never written directly to their
final location: every write lands in a temporary sibling and is moved into
place with ``os.replace``, so a crashed download can never leave a corrupt
file that later runs would trust.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import pooch

from fwl_io.paths import is_offline, resolve_cache_root, resolve_data_root
from fwl_io.registry import load_registry

log = logging.getLogger(__name__)


class OfflineDataError(RuntimeError):
    """A required file is unavailable locally while offline mode is active."""


class DownloadError(RuntimeError):
    """A file could not be obtained from any configured mirror."""


def _hash_matches(path: Path, known_hash: str) -> bool:
    algorithm = known_hash.split(':', 1)[0] if ':' in known_hash else 'sha256'
    digest = known_hash.split(':', 1)[-1]
    return pooch.file_hash(str(path), alg=algorithm) == digest


class Fetcher:
    """Fetches the files of one dataset into ``<data_root>/<subdir>``."""

    def __init__(
        self,
        subdir: str,
        registry: dict[str, str],
        zenodo: str | None = None,
        dataverse: str | None = None,
        base_urls: list[str] | None = None,
        data_root: str | Path | None = None,
    ):
        if not registry:
            raise ValueError('empty registry: run "fwl-io sync" for this dataset first')
        self.subdir = subdir
        self.registry = dict(registry)
        self.data_root = resolve_data_root(data_root)
        self.target_dir = self.data_root / subdir

        self.mirrors: list[str] = []
        if base_urls:
            self.mirrors.extend(url if url.endswith('/') else url + '/' for url in base_urls)
        if zenodo:
            self.mirrors.append(f'doi:{zenodo}/')
        if dataverse:
            self.mirrors.append(f'doi:{dataverse}/')
        if not self.mirrors:
            raise ValueError('no data source: provide zenodo, dataverse, or base_urls')

    def fetch(self, fname: str, offline: bool | None = None) -> Path:
        """Return a verified local path for ``fname``, downloading if needed."""
        if fname not in self.registry:
            raise KeyError(f'{fname!r} is not in the registry for {self.subdir!r}')
        known_hash = self.registry[fname]
        target = self.target_dir / fname

        if target.is_file() and _hash_matches(target, known_hash):
            return target
        if target.is_file():
            log.warning('checksum mismatch for %s; refetching', target)

        cached = self._fetch_from_cache(fname, known_hash, target)
        if cached is not None:
            return cached

        if is_offline() if offline is None else offline:
            raise OfflineDataError(
                f'{target} is missing or invalid and offline mode is active; '
                f'populate FWL_DATA or unset FWL_IO_OFFLINE'
            )
        return self._download(fname, known_hash, target)

    def _fetch_from_cache(self, fname: str, known_hash: str, target: Path) -> Path | None:
        cache_root = resolve_cache_root()
        if cache_root is None:
            return None
        cache_file = cache_root / self.subdir / fname
        if not cache_file.is_file() or not _hash_matches(cache_file, known_hash):
            return None
        self.target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.target_dir, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            shutil.copyfile(cache_file, tmp_path)
            os.replace(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)
        log.info('fetched %s from shared cache %s', fname, cache_root)
        return target

    def _download(self, fname: str, known_hash: str, target: Path) -> Path:
        self.target_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for mirror in self.mirrors:
            try:
                with tempfile.TemporaryDirectory(dir=self.target_dir) as tmp_dir:
                    got = pooch.retrieve(
                        url=f'{mirror}{fname}',
                        known_hash=known_hash,
                        fname=fname,
                        path=tmp_dir,
                        progressbar=False,
                    )
                    os.replace(got, target)
                return target
            except Exception as exc:  # noqa: BLE001 -- try the next mirror on any failure
                errors.append(f'{mirror}: {exc}')
                log.warning('mirror failed for %s: %s', fname, exc)
        raise DownloadError(f'could not obtain {fname!r} from any mirror:\n' + '\n'.join(errors))

    def fetch_all(self, offline: bool | None = None) -> list[Path]:
        """Fetch every file in the registry."""
        return [self.fetch(name, offline=offline) for name in sorted(self.registry)]

    def provenance(self) -> list[dict[str, str]]:
        """Return (file, source, checksum) records for run-provenance manifests."""
        return [
            {'file': f'{self.subdir}/{name}', 'source': self.mirrors[0], 'checksum': digest}
            for name, digest in sorted(self.registry.items())
        ]


def create_fetcher(
    subdir: str,
    zenodo: str | None = None,
    dataverse: str | None = None,
    registry: dict[str, str] | str | Path | None = None,
    base_urls: list[str] | None = None,
    data_root: str | Path | None = None,
) -> Fetcher:
    """Create a :class:`Fetcher` for one dataset.

    Parameters
    ----------
    subdir : str
        Location of the dataset below the data root (existing FWL_DATA layout).
    zenodo, dataverse : str | None
        Version DOIs of the primary record and its mirror.
    registry : dict | str | Path | None
        Name-to-hash mapping, or the path of a committed registry file.
    base_urls : list[str] | None
        Direct base URLs, tried before the DOI mirrors (used in tests and for
        non-DOI sources).
    data_root : str | Path | None
        Override for the data root; defaults to the resolved FWL_DATA tree.
    """
    if isinstance(registry, (str, Path)):
        registry = load_registry(registry)
    return Fetcher(
        subdir=subdir,
        registry=registry or {},
        zenodo=zenodo,
        dataverse=dataverse,
        base_urls=base_urls,
        data_root=data_root,
    )
