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
final location: every write lands in a staging directory on the same
filesystem (``<data_root>/.fwl-io-staging``) and is moved into place with
``os.replace``, so a crashed download can never leave a corrupt file that
later runs would trust. Stale staging entries are pruned opportunistically.

Mirror failures (network, checksum) and local placement failures (read-only
tree, full disk) are reported as distinct errors: a permission problem on
the data root is never disguised as a download problem.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import pooch

from fwl_io.paths import is_offline, resolve_cache_root, resolve_data_root
from fwl_io.registry import load_registry, validate_entry_name

log = logging.getLogger(__name__)

_STAGING_DIRNAME = '.fwl-io-staging'
_STAGING_MAX_AGE_S = 24 * 3600


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
        progress: bool = False,
    ):
        if not registry:
            raise ValueError('empty registry: run "fwl-io sync" for this dataset first')
        for name in registry:
            validate_entry_name(name)
        self.subdir = subdir
        self.registry = dict(registry)
        self.progress = progress
        self.data_root = resolve_data_root(data_root)
        self.target_dir = self.data_root / subdir
        if not self.target_dir.resolve().is_relative_to(self.data_root.resolve()):
            raise ValueError(
                f'subdir {subdir!r} escapes the data root {self.data_root}; '
                f'it must be a relative path without ".." components'
            )
        self._sources: dict[str, str] = {}

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
            self._sources.setdefault(fname, 'local')
            return target
        if target.is_file():
            log.warning('checksum mismatch for %s; refetching', target)

        cached = self._fetch_from_cache(fname, known_hash, target)
        if cached is not None:
            return cached

        if is_offline() if offline is None else offline:
            raise OfflineDataError(
                f'{target} is missing or invalid and offline mode is active; '
                f'populate the data tree at {self.data_root} or unset FWL_IO_OFFLINE'
            )
        return self._download(fname, known_hash, target)

    def _staging_dir(self) -> Path:
        """Return the per-tree staging directory, pruning stale leftovers."""
        staging = self.data_root / _STAGING_DIRNAME
        staging.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _STAGING_MAX_AGE_S
        for entry in staging.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()
            except OSError:  # another process may be pruning concurrently
                continue
        return staging

    def _place(self, staged: Path | str, target: Path) -> None:
        """Move a verified staged file into its final location atomically."""
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)

    def _fetch_from_cache(self, fname: str, known_hash: str, target: Path) -> Path | None:
        cache_root = resolve_cache_root()
        if cache_root is None:
            return None
        cache_file = cache_root / self.subdir / fname
        if not cache_file.is_file() or not _hash_matches(cache_file, known_hash):
            return None
        staging = self._staging_dir()
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            shutil.copyfile(cache_file, tmp_path)
            self._place(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)
        log.info('fetched %s from shared cache %s', fname, cache_root)
        self._sources[fname] = f'cache:{cache_root}'
        return target

    def _download(self, fname: str, known_hash: str, target: Path) -> Path:
        staging = self._staging_dir()
        tmp_dir = Path(tempfile.mkdtemp(dir=staging))
        try:
            errors: list[str] = []
            got: str | None = None
            used_mirror: str | None = None
            for mirror in self.mirrors:
                try:
                    got = pooch.retrieve(
                        url=f'{mirror}{fname}',
                        known_hash=known_hash,
                        fname=fname.replace('/', '_'),
                        path=tmp_dir,
                        progressbar=self.progress,
                    )
                except Exception as exc:  # noqa: BLE001 -- try the next mirror on any failure
                    errors.append(f'{mirror}: {exc}')
                    log.warning('mirror failed for %s: %s', fname, exc)
                    continue
                used_mirror = mirror
                break
            if got is None:
                raise DownloadError(
                    f'could not obtain {fname!r} from any mirror:\n' + '\n'.join(errors)
                )
            # Placement is outside the mirror loop: a local failure (read-only
            # tree, full disk) raises its own OSError, never a DownloadError.
            self._place(got, target)
            self._sources[fname] = used_mirror or self.mirrors[0]
            return target
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def fetch_all(self, offline: bool | None = None) -> list[Path]:
        """Fetch every file in the registry."""
        return [self.fetch(name, offline=offline) for name in sorted(self.registry)]

    def provenance(self) -> list[dict[str, str]]:
        """Return (file, source, checksum) records for run-provenance manifests.

        ``source`` is the actual origin of files resolved by this Fetcher
        instance (``local``, ``cache:<path>``, or the mirror that served the
        download). Files not yet fetched in this session are marked with a
        ``declared:`` prefix on the primary mirror, since their true origin
        is unknown.
        """
        return [
            {
                'file': f'{self.subdir}/{name}',
                'source': self._sources.get(name, f'declared:{self.mirrors[0]}'),
                'checksum': digest,
            }
            for name, digest in sorted(self.registry.items())
        ]


def create_fetcher(
    subdir: str,
    zenodo: str | None = None,
    dataverse: str | None = None,
    registry: dict[str, str] | str | Path | None = None,
    base_urls: list[str] | None = None,
    data_root: str | Path | None = None,
    progress: bool = False,
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
    progress : bool
        Show a download progress bar (requires tqdm; useful for large files).
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
        progress=progress,
    )
