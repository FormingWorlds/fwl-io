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

A transient transport failure (a read or connect timeout, a dropped or reset
connection, or a 429 or transient 5xx server response) is retried: every mirror is tried
once per round, and the whole set is retried on a short backoff schedule when a
round ends with no success and at least one transient failure. A round whose
failures are all permanent (a 404 or a checksum mismatch) stops the retries at
once. Every request carries an explicit connect and read timeout, so a stalled
mirror socket fails in bounded time instead of hanging the fetch.

Concurrency: a burst of processes (for example many PROTEUS instances started
together) that all miss the same file would otherwise each download it,
hammering the mirrors and risking rate-limiting for the whole collaboration.
:meth:`Fetcher.fetch` serialises fetchers of the *same* file with a per-target
inter-process lock and re-checks the target under it, so only the first process
downloads and the rest reuse the completed file; distinct files still fetch in
parallel. The lock lives on the shared data root and is coherent across nodes
where the filesystem supports it (verified on Kapteyn NFS and Hábrók Lustre).
It is best-effort: if the filesystem has no usable lock manager or a holder
stalls past ``lock_timeout``, waiters log a warning and fetch unguarded rather
than failing or blocking the batch.

A dataset pinned to a Zenodo version DOI resolves into a per-version
directory ``<subdir>/r<record-id>`` so successive deposits coexist, and
``fetch_all`` writes a ``.fwl-io.json`` stamp (DOI, checksums, fetch date)
there so a completed dataset directory or shared cache is self-describing.

A dataset given ``extract="tar"`` or ``"zip"`` ships as a single archive: the
archive is downloaded and checksum-verified like any file, then extracted
(safely, rejecting members that escape the directory) into the version
directory, with the tree moved into place atomically. The archive is not kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import pooch
import requests
from filelock import FileLock, Timeout

from fwl_io.archive import ARCHIVE_KINDS, extract_archive
from fwl_io.doi import zenodo_record_id
from fwl_io.paths import is_offline, resolve_cache_root, resolve_data_root
from fwl_io.registry import load_registry, validate_entry_name

log = logging.getLogger('fwl.' + __name__)

_LOCK_DIRNAME = '.fwl-io-locks'
# How long a fetcher waits for the per-target download lock before giving up and
# fetching unguarded. Bounds the worst case where one process wins the lock and
# then stalls on a slow mirror: waiters degrade to their own fetch rather than
# blocking a whole batch indefinitely.
_LOCK_TIMEOUT_S = 300.0
_STAGING_DIRNAME = '.fwl-io-staging'
_STAGING_MAX_AGE_S = 24 * 3600
_STAMP_FILENAME = '.fwl-io.json'
_STAMP_SCHEMA = 1

# Bounded retry for transient transport failures. Each entry is the wait in
# seconds before the corresponding retry round, so the tuple length is the
# number of retry rounds after the first. Every mirror is tried once per round;
# the set is retried only when a round has at least one transient failure and no
# success. A read timeout, a dropped connection, or a 429/5xx response is retried
# on this schedule; a 404 or a checksum mismatch is permanent and is not retried.
_RETRY_BACKOFF_S: tuple[float, ...] = (10.0, 30.0, 60.0)

# HTTP status codes worth retrying: request timeout, rate limiting, and the
# transient server and gateway errors. Other 4xx (a 404) and 501/505 are
# permanent and are not retried.
_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# requests exception types that signal a transport-level failure worth retrying:
# a connect or read timeout, a connection refused or reset before the body, a
# connection dropped or the stream truncated mid-download, a corrupt compressed
# body, and a malformed Zenodo-metadata response (pooch reads the record through
# an API call whose non-JSON error body raises JSONDecodeError). A Dataverse
# resolution error instead surfaces as a plain ValueError and stays permanent.
_TRANSIENT_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.JSONDecodeError,
)

# Explicit (connect, read) timeout handed to pooch's downloaders, so a mirror
# that will not connect fails fast and a stalled transfer fails in bounded time,
# instead of inheriting pooch's scalar default. Applied to the DOI and
# direct-URL paths alike.
_DOWNLOAD_TIMEOUT_S: tuple[float, float] = (10.0, 60.0)


def _fwl_io_version() -> str:
    """Return the installed fwl-io version, or a fallback for editable trees."""
    try:
        return _pkg_version('fwl-io')
    except PackageNotFoundError:
        return '0.0.0'


class OfflineDataError(RuntimeError):
    """A required file is unavailable locally while offline mode is active."""


class DownloadError(RuntimeError):
    """A file could not be obtained from any configured mirror."""


def _hash_matches(path: Path, known_hash: str) -> bool:
    algorithm = known_hash.split(':', 1)[0] if ':' in known_hash else 'sha256'
    digest = known_hash.split(':', 1)[-1]
    return pooch.file_hash(str(path), alg=algorithm) == digest


def _is_transient(exc: BaseException) -> bool:
    """Return whether a failed download is worth retrying.

    An HTTP error is decided by its status: a request timeout, rate limiting, or
    a transient server or gateway error (``_RETRYABLE_STATUS``) is retried, while
    a 404 or any other status is permanent. A non-HTTP failure is retried when it
    is a transport-level error (``_TRANSIENT_EXC``): a timeout, a refused or reset
    connection, a stream dropped or truncated mid-download, or a malformed
    response from resolving a Zenodo DOI. A checksum mismatch (pooch raises a
    plain ``ValueError``, which is not one of those types) is permanent.
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        return status in _RETRYABLE_STATUS
    return isinstance(exc, _TRANSIENT_EXC)


class Fetcher:
    """Fetches the files of one dataset below the data root.

    A dataset pinned to a Zenodo version DOI lands in a per-version directory
    ``<data_root>/<subdir>/r<record-id>``, so an updated deposit is placed
    beside its predecessor rather than overwriting it, and :meth:`fetch_all`
    writes a ``.fwl-io.json`` provenance stamp there so the completed
    directory is self-describing. A source given only as ``base_urls`` (no
    Zenodo pin) keeps the bare ``<data_root>/<subdir>``.
    """

    def __init__(
        self,
        subdir: str,
        registry: dict[str, str],
        zenodo: str | None = None,
        dataverse: str | None = None,
        base_urls: list[str] | None = None,
        data_root: str | Path | None = None,
        progress: bool = False,
        extract: str | None = None,
        lock_timeout: float = _LOCK_TIMEOUT_S,
    ):
        if not registry:
            raise ValueError('empty registry: run "fwl-io sync" for this dataset first')
        for name in registry:
            validate_entry_name(name)
        if extract is not None:
            if extract not in ARCHIVE_KINDS:
                raise ValueError(
                    f'unknown extract kind {extract!r}; expected one of {ARCHIVE_KINDS}'
                )
            if len(registry) != 1:
                raise ValueError(
                    f'an archive dataset lists exactly one archive file in its registry, '
                    f'got {len(registry)}: {sorted(registry)}'
                )
            if not zenodo:
                raise ValueError(
                    'an archive dataset requires a Zenodo version DOI: extraction needs a '
                    'version directory to stamp and to detect a deleted member on refetch'
                )
        # A DOI is used both as a mirror URL and as the version-directory key,
        # so surrounding whitespace is trimmed once here rather than reaching a
        # request URL through the string interpolation below.
        zenodo = zenodo.strip() if zenodo else zenodo
        dataverse = dataverse.strip() if dataverse else dataverse
        self.subdir = subdir
        self.zenodo = zenodo
        self.registry = dict(registry)
        self.progress = progress
        self.extract = extract
        self.lock_timeout = lock_timeout
        # A dataset pinned to a Zenodo version DOI resolves into a per-version
        # directory r<record-id> below its subdir, so an updated deposit lands
        # beside its predecessor instead of overwriting it. Sources without a
        # Zenodo pin (direct base_urls) keep the bare subdir.
        self.record_id = zenodo_record_id(zenodo) if zenodo else None
        self.version_dir = f'r{self.record_id}' if self.record_id else None
        self.rel_dir = f'{subdir}/{self.version_dir}' if self.version_dir else subdir
        self.data_root = resolve_data_root(data_root)
        self.target_dir = self.data_root / self.rel_dir
        if not self.target_dir.resolve().is_relative_to(self.data_root.resolve()):
            raise ValueError(
                f'subdir {subdir!r} escapes the data root {self.data_root}; '
                f'it must be a relative path without ".." components'
            )
        self._sources: dict[str, str] = {}
        self._archive_members: list[str] = []

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

        # Fast path: an already-present, valid file needs no work and no lock,
        # so the common case (data already on disk) pays nothing for the guard.
        if target.is_file() and _hash_matches(target, known_hash):
            self._sources.setdefault(fname, 'local')
            return target

        # Serialise concurrent fetchers of the *same* file across processes: a
        # burst of instances that all miss it would otherwise each hit the
        # mirrors at once and risk getting the collaboration rate-limited. The
        # lock is per-target, so unrelated files still fetch in parallel.
        # Double-checked: re-test the target under the lock, because another
        # process may have completed the download while we waited.
        with self._fetch_lock(fname, target):
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

    @contextmanager
    def _fetch_lock(self, fname: str, target: Path):
        """Hold a best-effort inter-process lock guarding one target's download.

        The lock only suppresses the thundering herd; it is never required for
        correctness, since the caller re-checks the target under it and the
        download is atomic. So it degrades instead of failing in the two cases
        that would otherwise turn a working fetch into a stalled or failed one:

        * the filesystem has no usable lock manager (e.g. an NFS mount whose
          lock daemon is down, or a Lustre mount without ``flock``), so
          acquiring raises ``OSError``/``ENOLCK``; or
        * a process that holds the lock stalls on a slow mirror past
          ``lock_timeout``.

        In both cases we log and proceed unlocked -- no worse than having no
        lock at all, which is the pre-lock behaviour. Where the lock works
        (verified cross-node on Kapteyn NFS and Hábrók Lustre) exactly one
        process fetches and the rest reuse its result.
        """
        lock = None
        try:
            lock = FileLock(str(self._lock_path(fname)), timeout=self.lock_timeout)
            lock.acquire()
        except Timeout:
            log.warning(
                'timed out after %ss waiting for the download lock on %s; fetching without it',
                self.lock_timeout,
                target,
            )
            lock = None
        except OSError as exc:
            log.warning(
                'download lock unavailable on this filesystem for %s (%s); fetching without it',
                target,
                exc,
            )
            lock = None
        try:
            yield
        finally:
            if lock is not None and lock.is_locked:
                lock.release()

    def _lock_path(self, fname: str) -> Path:
        """Return the lock file guarding one target, keyed on its on-disk path.

        Locks live in a dedicated directory under the (always writable) data
        root, not beside the target, whose parent may not exist yet or may
        itself be read-only. The name is a hash of the versioned relative path,
        so it is unique per file and free of path separators or length limits.
        """
        lock_dir = self.data_root / _LOCK_DIRNAME
        lock_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f'{self.rel_dir}/{fname}'.encode()).hexdigest()[:32]
        return lock_dir / f'{key}.lock'

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
        cache_file = cache_root / self.rel_dir / fname
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

    def _downloader(self, mirror: str):
        """Build a pooch downloader for one mirror with a bounded request timeout.

        A ``doi:`` mirror is resolved through pooch's DOI downloader; a direct
        base URL uses the plain HTTP downloader. Both carry the same explicit
        per-request timeout (``_DOWNLOAD_TIMEOUT_S``), so a stalled socket fails
        in bounded time instead of inheriting pooch's downloader-specific
        default.
        """
        if mirror.startswith('doi:'):
            return pooch.DOIDownloader(progressbar=self.progress, timeout=_DOWNLOAD_TIMEOUT_S)
        return pooch.HTTPDownloader(progressbar=self.progress, timeout=_DOWNLOAD_TIMEOUT_S)

    def _retrieve_once(self, mirror: str, fname: str, known_hash: str, into_dir: Path) -> str:
        """Download ``fname`` from a single mirror in one attempt.

        Returns the local path pooch wrote on success and lets pooch's exception
        (a transport error, an HTTP error, or a checksum ``ValueError``)
        propagate so the caller can classify it for retry.
        """
        return pooch.retrieve(
            url=f'{mirror}{fname}',
            known_hash=known_hash,
            fname=fname.replace('/', '_'),
            path=into_dir,
            downloader=self._downloader(mirror),
        )

    def _retrieve_from_mirrors(
        self, fname: str, known_hash: str, into_dir: Path
    ) -> tuple[str, str]:
        """Download and checksum-verify ``fname`` into ``into_dir``.

        Each round tries every mirror once (Zenodo first, then Dataverse), so a
        transient failure on one mirror falls over to the next at once rather
        than waiting out the backoff. When a round ends with no success and at
        least one transient failure, the loop waits ``_RETRY_BACKOFF_S`` and
        tries the whole set again; a round whose failures are all permanent (a
        404 or a checksum mismatch on every mirror) stops immediately. Returns
        ``(local_path, mirror)`` on success and raises :class:`DownloadError`
        when no mirror serves the file.
        """
        errors: list[str] = []
        rounds = (0.0, *_RETRY_BACKOFF_S)
        for round_idx, delay in enumerate(rounds):
            if delay:
                log.warning(
                    'retrying %s after transient mirror failures; waiting %gs (round %d/%d)',
                    fname,
                    delay,
                    round_idx + 1,
                    len(rounds),
                )
                time.sleep(delay)
            errors = []
            retriable = False
            for mirror in self.mirrors:
                try:
                    got = self._retrieve_once(mirror, fname, known_hash, into_dir)
                except Exception as exc:  # noqa: BLE001 -- classified for retry, tried per mirror
                    errors.append(f'{mirror}: {exc}')
                    retriable = retriable or _is_transient(exc)
                    log.warning('mirror failed for %s: %s', fname, exc)
                    continue
                return got, mirror
            if not retriable:
                break
        raise DownloadError(f'could not obtain {fname!r} from any mirror:\n' + '\n'.join(errors))

    def _download(self, fname: str, known_hash: str, target: Path) -> Path:
        staging = self._staging_dir()
        tmp_dir = Path(tempfile.mkdtemp(dir=staging))
        try:
            got, used_mirror = self._retrieve_from_mirrors(fname, known_hash, tmp_dir)
            # Placement is outside the retrieval: a local failure (read-only
            # tree, full disk) raises its own OSError, never a DownloadError.
            self._place(got, target)
            self._sources[fname] = used_mirror
            return target
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def fetch_all(self, offline: bool | None = None) -> list[Path]:
        """Fetch every registry file, then stamp the version directory.

        The stamp is written here (the whole-dataset operation), not by an
        individual :meth:`fetch`, so a version directory populated one file at
        a time is not stamped until a ``fetch_all`` completes it. A stamp-write
        failure never fails the fetch: the data is already in place. An archive
        dataset is downloaded, verified, and extracted as one operation.
        """
        if self.extract is not None:
            return self._fetch_archive(offline=offline)
        paths = [self.fetch(name, offline=offline) for name in sorted(self.registry)]
        self._write_stamp()
        return paths

    def _extracted_files(self) -> list[Path]:
        """Return the extracted data files under the version directory.

        Only the stamp at the top of the directory is excluded. A deposit is
        free to ship a file of that name further down, and it belongs to the
        dataset like any other member.
        """
        own_stamp = self.target_dir / _STAMP_FILENAME
        return sorted(p for p in self.target_dir.rglob('*') if p.is_file() and p != own_stamp)

    def _place_tree(self, src_dir: Path, target_dir: Path) -> None:
        """Move a fully extracted tree into its final location atomically.

        Any existing tree is removed first; because the staged tree is complete
        before this runs, a crash between the removal and the rename leaves the
        dataset absent (a re-fetch restores it) rather than half-populated.
        """
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.is_dir():
            shutil.rmtree(target_dir)
        elif target_dir.exists():  # a stray file where the dataset directory belongs
            target_dir.unlink()
        os.replace(src_dir, target_dir)

    def _archive_tree_intact(self, stamp: Path) -> bool:
        """True when every member the stamp recorded is still present on disk.

        This detects a member deleted after extraction, so the tree is
        re-fetched rather than served incomplete. It does not re-hash contents:
        the archive-only checksum policy records member names, not per-file
        digests, so a truncated member is not detected here.
        """
        try:
            record = json.loads(stamp.read_text())
        except (OSError, ValueError):
            return False
        if record.get('extract') != self.extract:
            # The stamp describes a different fetch of this deposit, a plain
            # one or a different archive kind, so its tree is not this dataset.
            return False
        members = record.get('members')
        if not isinstance(members, list) or not members:
            # An absent or empty member list describes no tree at all, and must
            # never read as a complete one.
            return False
        return all((self.target_dir / m).is_file() for m in members)

    def _fetch_archive(self, offline: bool | None = None) -> list[Path]:
        """Download the single archive, verify it, and extract it into place.

        The archive itself is not kept: the version directory holds the
        extracted tree. A current provenance stamp whose recorded members are
        all still present short-circuits re-download and re-extraction; unlike
        the per-file path it does not re-hash contents (the archive-only
        checksum policy records member names, not per-file digests). Extraction
        is staged and the tree is moved into place atomically.
        """
        archive_name, known_hash = next(iter(self.registry.items()))
        stamp = self.target_dir / _STAMP_FILENAME
        if self._stamp_is_current(stamp) and self._archive_tree_intact(stamp):
            self._sources.setdefault(archive_name, 'local')
            return self._extracted_files()

        cached_archive = self._cached_archive(archive_name, known_hash)
        if cached_archive is None and self._copy_cached_tree():
            self._sources[archive_name] = f'cache:{resolve_cache_root()}'
            self._archive_members = [
                p.relative_to(self.target_dir).as_posix() for p in self._extracted_files()
            ]
            self._write_stamp()
            return self._extracted_files()

        if cached_archive is None and (is_offline() if offline is None else offline):
            raise OfflineDataError(
                f'{self.target_dir} is missing or incomplete and offline mode is active; '
                f'populate the data tree at {self.data_root} or unset FWL_IO_OFFLINE'
            )

        staging = self._staging_dir()
        work = Path(tempfile.mkdtemp(dir=staging))
        try:
            if cached_archive is not None:
                got, used_mirror = str(cached_archive), f'cache:{resolve_cache_root()}'
            else:
                got, used_mirror = self._retrieve_from_mirrors(archive_name, known_hash, work)
            extract_dir = work / 'extracted'
            extract_dir.mkdir()
            extract_archive(Path(got), extract_dir, self.extract)
            # Placement is outside the retrieval/extraction: a local failure
            # (read-only tree, full disk) raises its own OSError, never a
            # DownloadError or ArchiveError.
            self._place_tree(extract_dir, self.target_dir)
            self._sources[archive_name] = used_mirror
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # Record the extracted member names so a later fetch can detect a member
        # deleted from the tree and re-extract instead of serving it incomplete.
        self._archive_members = [
            p.relative_to(self.target_dir).as_posix() for p in self._extracted_files()
        ]
        self._write_stamp()
        return self._extracted_files()

    def _cached_archive(self, fname: str, known_hash: str) -> Path | None:
        """Return the archive in the shared cache when its checksum matches."""
        cache_root = resolve_cache_root()
        if cache_root is None:
            return None
        cache_file = cache_root / self.rel_dir / fname
        if not cache_file.is_file() or not _hash_matches(cache_file, known_hash):
            return None
        log.info('extracting %s from shared cache %s', fname, cache_root)
        return cache_file

    def _copy_cached_tree(self) -> bool:
        """Copy an already-extracted dataset out of the shared cache.

        A cache populated by fwl-io holds the extracted tree rather than the
        archive, since the archive is dropped after extraction. The cached
        stamp has to describe this deposit and the same archive kind, and
        every member it names has to be present, which is the same standard
        the local tree is held to.
        """
        cache_root = resolve_cache_root()
        if cache_root is None:
            return False
        cached_dir = cache_root / self.rel_dir
        cached_stamp = cached_dir / _STAMP_FILENAME
        try:
            record = json.loads(cached_stamp.read_text())
        except (OSError, ValueError):
            return False
        if record.get('record_id') != self.record_id or record.get('zenodo') != self.zenodo:
            return False
        if record.get('extract') != self.extract:
            return False
        members = record.get('members')
        if not isinstance(members, list) or not members:
            return False
        if not all((cached_dir / m).is_file() for m in members):
            return False
        staging = self._staging_dir()
        work = Path(tempfile.mkdtemp(dir=staging))
        try:
            copied = work / 'tree'
            shutil.copytree(cached_dir, copied)
            (copied / _STAMP_FILENAME).unlink(missing_ok=True)
            self._place_tree(copied, self.target_dir)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        log.info('copied dataset from shared cache %s', cache_root)
        return True

    def _stamp_is_current(self, stamp: Path) -> bool:
        """True when a valid stamp for this exact record id already exists.

        A missing, unreadable, non-JSON, or mismatched stamp is not current,
        so it is rewritten (healed) rather than trusted forever.
        """
        try:
            existing = json.loads(stamp.read_text())
        except (OSError, ValueError):
            return False
        return existing.get('record_id') == self.record_id and existing.get('zenodo') == self.zenodo

    def _write_stamp(self) -> None:
        """Write a self-describing provenance stamp into the version directory.

        The stamp (``.fwl-io.json``) records the pinned DOI, the registry
        checksums, and the fetch date, so a version directory or shared cache
        carries its own provenance. It is written only for datasets that
        resolve into a version directory (those with a Zenodo pin). A valid
        stamp for the same record id is left untouched, so the recorded fetch
        date stays the first-fetch date and a repeat fetch causes no churn; a
        corrupt or mismatched stamp is rewritten.

        A provenance write is metadata, not data: any filesystem error (a
        read-only or full tree) is logged and swallowed, never raised, so it
        cannot fail an otherwise successful fetch.
        """
        if self.version_dir is None:
            return
        stamp = self.target_dir / _STAMP_FILENAME
        if self._stamp_is_current(stamp):
            return
        record = {
            'schema': _STAMP_SCHEMA,
            'subdir': self.subdir,
            'zenodo': self.zenodo,
            'record_id': self.record_id,
            'extract': self.extract,
            'fetched': datetime.now(UTC).isoformat(timespec='seconds'),
            'fwl_io_version': _fwl_io_version(),
            'files': dict(self.registry),
        }
        if self.extract is not None:
            # The extracted member names, so a later fetch can tell whether the
            # tree is still complete without re-hashing every file.
            record['members'] = sorted(self._archive_members)
        payload = json.dumps(record, indent=2, sort_keys=True) + '\n'
        tmp_path: Path | None = None
        try:
            self.target_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                'w', dir=self.target_dir, prefix='.fwl-io-stamp-', suffix='.tmp', delete=False
            ) as fh:
                fh.write(payload)
                tmp_path = Path(fh.name)
            os.replace(tmp_path, stamp)
            tmp_path = None
        except OSError as exc:
            log.warning('could not write provenance stamp %s: %s', stamp, exc)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

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
                'file': f'{self.rel_dir}/{name}',
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
    extract: str | None = None,
    lock_timeout: float = _LOCK_TIMEOUT_S,
) -> Fetcher:
    """Create a :class:`Fetcher` for one dataset.

    Parameters
    ----------
    subdir : str
        Location of the dataset below the data root. When a Zenodo pin is
        given the files land in a version directory ``<subdir>/r<record-id>``
        below it.
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
    extract : str | None
        When set (``"tar"`` or ``"zip"``), the single registry entry is a
        downloadable archive; it is verified, then its members are extracted
        into the dataset directory and the archive itself is discarded.
    lock_timeout : float
        Seconds a fetcher waits for the per-target download lock before giving
        up and fetching unguarded (default five minutes). The lock only
        suppresses duplicate concurrent downloads; a waiter that times out (or
        a filesystem without a working lock manager) falls back to its own
        fetch rather than blocking or failing.
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
        extract=extract,
        lock_timeout=lock_timeout,
    )
