"""Safe extraction of tar and zip archive datasets.

A dataset declared with ``extract = "tar"`` or ``"zip"`` in its manifest ships
as a single archive on Zenodo. The fetcher downloads and checksum-verifies the
archive, then extracts its members into the dataset directory; the archive
itself is not kept, so consumers see the extracted tree they expect.

Extraction is hardened against hostile archives: every member is checked before
anything is written, and a member that would escape the destination (an
absolute path, a ``..`` component, or a symlink/hardlink/device) is rejected, so
an untrusted archive cannot place a file outside the dataset directory.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

ARCHIVE_KINDS = ('tar', 'zip')


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
    with tarfile.open(archive, 'r:*') as tf:
        members = tf.getmembers()
        for m in members:
            # Datasets are plain files and directories; a link or device node in
            # a data archive is either a mistake or an attack, so reject it.
            if m.issym() or m.islnk() or m.isdev() or m.ischr() or m.isblk() or m.isfifo():
                raise ArchiveError(
                    f'unsafe archive member {m.name!r}: only regular files and '
                    f'directories are allowed'
                )
            if _escapes(dest_resolved, m.name):
                raise ArchiveError(f'unsafe archive member {m.name!r}: escapes the destination')
        try:
            # The 'data' filter (Python 3.12, backported to 3.11.4+) is a second
            # line of defence over the explicit checks above.
            tf.extractall(dest, filter='data')
        except TypeError:  # Python build without the 'filter' keyword
            tf.extractall(dest, members=members)


def _extract_zip(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if _escapes(dest_resolved, name):
                raise ArchiveError(f'unsafe archive member {name!r}: escapes the destination')
        zf.extractall(dest)


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
