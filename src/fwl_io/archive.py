"""Safe extraction of tar and zip archive datasets.

A dataset declared with ``extract = "tar"`` or ``"zip"`` in its manifest ships
as a single archive on Zenodo. The fetcher downloads and checksum-verifies the
archive, then extracts its members into the dataset directory; the archive
itself is not kept, so consumers see the extracted tree they expect.

Extraction is hardened against hostile archives: every member is checked before
anything is written. A member that would escape the destination (an absolute
path or a ``..`` component) is rejected, and a member that is not a plain file
or directory (a symlink, hardlink, or device node) is refused by type, so an
untrusted archive can neither place a file outside the dataset directory nor
smuggle in a link.
"""

from __future__ import annotations

import stat
import tarfile
import zipfile
from pathlib import Path

ARCHIVE_KINDS = ('tar', 'zip')

# Only regular files and directories belong in a data archive; a link, device,
# or fifo member is either a mistake or an attack, so it is refused.
_MEMBER_TYPE_MSG = 'only regular files and directories are allowed'
# Zip stores a unix mode in the high 16 bits of external_attr; 0 means no mode
# was recorded (a Windows-created entry), which is treated as a regular file.
_ALLOWED_ZIP_MODES = frozenset({0, stat.S_IFREG, stat.S_IFDIR})


class ArchiveError(RuntimeError):
    """An archive could not be extracted safely."""


def _escapes(dest_resolved: Path, member_name: str) -> bool:
    """True when ``member_name`` would resolve outside ``dest_resolved``."""
    if member_name.startswith('/') or member_name.startswith('\\'):
        return True
    target = (dest_resolved / member_name).resolve()
    return target != dest_resolved and not target.is_relative_to(dest_resolved)


def _extract_tar(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    try:
        with tarfile.open(archive, 'r:*') as tf:
            members = tf.getmembers()
            for m in members:
                if m.issym() or m.islnk() or m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                    raise ArchiveError(f'unsafe archive member {m.name!r}: {_MEMBER_TYPE_MSG}')
                if _escapes(dest_resolved, m.name):
                    raise ArchiveError(f'unsafe archive member {m.name!r}: escapes the destination')
            try:
                # The 'data' filter (Python 3.12, backported to 3.11.4+) is a
                # second line of defence over the explicit checks above.
                tf.extractall(dest, filter='data')
            except TypeError:  # Python build without the 'filter' keyword
                tf.extractall(dest, members=members)
    except tarfile.TarError as exc:  # unreadable/corrupt archive, or a filter rejection
        raise ArchiveError(f'could not read tar archive {archive.name!r}: {exc}') from exc


def _extract_zip(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in _ALLOWED_ZIP_MODES:
                    raise ArchiveError(
                        f'unsafe archive member {info.filename!r}: {_MEMBER_TYPE_MSG}'
                    )
                if _escapes(dest_resolved, info.filename):
                    raise ArchiveError(
                        f'unsafe archive member {info.filename!r}: escapes the destination'
                    )
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:  # unreadable/corrupt zip, at open or during read
        raise ArchiveError(f'not a valid zip archive {archive.name!r}: {exc}') from exc


def extract_archive(archive: Path, dest: Path, kind: str) -> None:
    """Extract ``archive`` (a ``tar`` or ``zip`` file) into ``dest`` safely.

    Every member is validated before extraction; on any unsafe member nothing
    is written and :class:`ArchiveError` is raised. ``dest`` must already exist.

    Parameters
    ----------
    archive : Path
        The downloaded, checksum-verified archive file.
    dest : Path
        The directory the members are extracted into.
    kind : str
        ``"tar"`` (any tar compression) or ``"zip"``.

    Raises
    ------
    ArchiveError
        If ``kind`` is unknown, or any member would escape ``dest`` or is not a
        regular file or directory.
    """
    if kind == 'tar':
        _extract_tar(archive, dest)
    elif kind == 'zip':
        _extract_zip(archive, dest)
    else:
        raise ArchiveError(f'unknown archive kind {kind!r}; expected one of {ARCHIVE_KINDS}')
