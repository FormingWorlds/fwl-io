"""Tests for safe tar/zip archive extraction (``fwl_io.archive``).

The module under test extracts a downloaded, checksum-verified archive into a
dataset directory. The security contract is that no member may write outside
that directory: absolute paths, ``..`` traversal, and link/device members are
rejected before anything is written. These tests exercise the happy path plus
each rejection, and assert that a rejected archive leaves nothing on disk.

See ``docs/How-to/testing.md`` for the fwl-io test conventions.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from fwl_io.archive import ArchiveError, extract_archive

pytestmark = pytest.mark.unit


def _make_tar(path: Path, members: list[tuple[str, bytes]], *, compression: str = '') -> None:
    mode = f'w:{compression}' if compression else 'w'
    with tarfile.open(path, mode) as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _make_tar_symlink(path: Path, link_name: str, target: str) -> None:
    with tarfile.open(path, 'w') as tf:
        info = tarfile.TarInfo(link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tf.addfile(info)


def _make_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in members:
            zf.writestr(name, data)


@pytest.mark.parametrize('compression', ['', 'gz'])
def test_extract_tar_places_all_members(tmp_path, compression):
    """A tar (plain or gzipped) yields exactly its members, including nested ones."""
    archive = tmp_path / 'data.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar(
        archive,
        [('a.txt', b'AAA\n'), ('sub/b.txt', b'BBBB\n')],
        compression=compression,
    )
    extract_archive(archive, dest, 'tar')
    assert (dest / 'a.txt').read_bytes() == b'AAA\n'
    # A nested member keeps its subdirectory, not flattened onto the basename.
    assert (dest / 'sub' / 'b.txt').read_bytes() == b'BBBB\n'
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file())
    assert files == ['a.txt', 'sub/b.txt']


def test_extract_zip_places_all_members(tmp_path):
    """A zip yields exactly its members, including nested ones."""
    archive = tmp_path / 'data.zip'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_zip(archive, [('a.txt', b'AAA\n'), ('sub/b.txt', b'BBBB\n')])
    extract_archive(archive, dest, 'zip')
    assert (dest / 'a.txt').read_bytes() == b'AAA\n'
    assert (dest / 'sub' / 'b.txt').read_bytes() == b'BBBB\n'


@pytest.mark.parametrize('evil_name', ['../evil.txt', '/tmp/evil.txt', 'sub/../../evil.txt'])
def test_tar_traversal_member_rejected_and_nothing_written(tmp_path, evil_name):
    """A tar member that escapes the destination is refused before any write.

    The archive pairs a legitimate member with a traversing one; extraction must
    raise and leave the destination empty (validation precedes any extraction),
    and must not create the escape target outside the destination.
    """
    archive = tmp_path / 'evil.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar(archive, [('ok.txt', b'ok\n'), (evil_name, b'PWNED\n')])
    with pytest.raises(ArchiveError, match='escapes the destination'):
        extract_archive(archive, dest, 'tar')
    # Nothing was written: not the legitimate member, and not the escape target.
    assert list(dest.iterdir()) == []
    assert not (tmp_path / 'evil.txt').exists()


def test_tar_symlink_member_rejected(tmp_path):
    """A symlink member (a classic escape vector) is refused, even if it points outside."""
    archive = tmp_path / 'link.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar_symlink(archive, 'link', '/etc/passwd')
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


def test_zip_traversal_member_rejected_and_nothing_written(tmp_path):
    """A zip member that escapes the destination is refused before any write."""
    archive = tmp_path / 'evil.zip'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_zip(archive, [('ok.txt', b'ok\n'), ('../evil.txt', b'PWNED\n')])
    with pytest.raises(ArchiveError, match='escapes the destination'):
        extract_archive(archive, dest, 'zip')
    assert list(dest.iterdir()) == []
    assert not (tmp_path / 'evil.txt').exists()


def test_unknown_kind_rejected(tmp_path):
    """An unsupported archive kind is a clear error, not a silent no-op."""
    archive = tmp_path / 'data.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar(archive, [('a.txt', b'AAA\n')])
    with pytest.raises(ArchiveError, match="unknown archive kind 'rar'"):
        extract_archive(archive, dest, 'rar')
    # A rejected kind writes nothing.
    assert list(dest.iterdir()) == []


def _make_tar_typed_member(path: Path, name: str, typeflag: bytes, *, linkname: str = '') -> None:
    with tarfile.open(path, 'w') as tf:
        info = tarfile.TarInfo(name)
        info.type = typeflag
        info.linkname = linkname
        tf.addfile(info)


def _make_zip_symlink(path: Path, name: str, target: str) -> None:
    import stat

    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr(info, target)


def test_tar_hardlink_member_rejected(tmp_path):
    """A hardlink member is refused (not just symlinks); the type check must cover it."""
    archive = tmp_path / 'hard.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar_typed_member(archive, 'hard', tarfile.LNKTYPE, linkname='ok.txt')
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


def test_tar_fifo_member_rejected(tmp_path):
    """A device/fifo member is refused, so the non-symlink half of the type check is live."""
    archive = tmp_path / 'fifo.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar_typed_member(archive, 'pipe', tarfile.FIFOTYPE)
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize(
    ('label', 'member_type'),
    [('chr', tarfile.CHRTYPE), ('blk', tarfile.BLKTYPE)],
)
def test_tar_device_member_rejected(tmp_path, label, member_type):
    """A character or block device is refused, like every non-file member."""
    archive = tmp_path / f'{label}.tar'
    dest = tmp_path / f'dest_{label}'
    dest.mkdir()
    _make_tar_typed_member(archive, f'{label}dev', member_type)
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


def test_truncated_compressed_tar_reports_an_archive_error(tmp_path):
    """A stream that ends early is an archive failure, not a raw EOFError."""
    archive = tmp_path / 'cut.tar.gz'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar(archive, [('m0.txt', b'0.1\n' * 500)], compression='gz')
    whole = archive.read_bytes()
    archive.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(ArchiveError, match='could not read tar archive'):
        extract_archive(archive, dest, 'tar')
    # Discrimination: the intact archive of the same shape extracts cleanly.
    _make_tar(archive, [('m0.txt', b'0.1\n' * 500)], compression='gz')
    extract_archive(archive, dest, 'tar')
    assert (dest / 'm0.txt').is_file()


def test_encrypted_zip_entry_reports_an_archive_error(tmp_path):
    """An entry this build cannot decode is an archive failure, not a RuntimeError."""
    archive = tmp_path / 'locked.zip'
    dest = tmp_path / 'dest'
    dest.mkdir()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('m0.txt', b'0.1\n')
    # Set the encrypted flag in the central directory, which is what zipfile
    # reads, so extraction fails for want of a password.
    raw = bytearray(archive.read_bytes())
    raw[raw.rfind(b'PK\x01\x02') + 8] |= 0x01
    archive.write_bytes(bytes(raw))
    with pytest.raises(ArchiveError, match='not a valid zip archive'):
        extract_archive(archive, dest, 'zip')
    assert not (dest / 'm0.txt').exists()


def test_member_colliding_with_a_written_file_reports_an_archive_error(tmp_path):
    """A member below a name already written as a file is an archive failure."""
    archive = tmp_path / 'clash.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar(archive, [('a', b'file\n'), ('a/b', b'below\n')])
    with pytest.raises(ArchiveError, match='could not read tar archive'):
        extract_archive(archive, dest, 'tar')


def test_tar_relative_symlink_member_rejected(tmp_path):
    """Even a symlink whose target stays inside the destination is refused.

    This pins the module's stricter contract (reject all links) rather than only
    the stdlib data filter's behaviour (which permits a safe relative symlink).
    """
    archive = tmp_path / 'link.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_tar_typed_member(archive, 'inside_link', tarfile.SYMTYPE, linkname='ok.txt')
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


def test_zip_symlink_member_rejected(tmp_path):
    """A zip entry flagged as a symlink is refused, matching the tar contract."""
    archive = tmp_path / 'link.zip'
    dest = tmp_path / 'dest'
    dest.mkdir()
    _make_zip_symlink(archive, 'evil_link', '/etc/passwd')
    with pytest.raises(ArchiveError, match='only regular files and directories'):
        extract_archive(archive, dest, 'zip')
    assert list(dest.iterdir()) == []


def test_garbage_tar_raises_archive_error(tmp_path):
    """Non-archive bytes on the tar path fail as ArchiveError, not a raw tarfile error."""
    archive = tmp_path / 'notreal.tar'
    dest = tmp_path / 'dest'
    dest.mkdir()
    archive.write_bytes(b'this is definitely not a tar archive\n')
    with pytest.raises(ArchiveError, match='could not read tar archive'):
        extract_archive(archive, dest, 'tar')
    assert list(dest.iterdir()) == []


def test_garbage_zip_raises_archive_error(tmp_path):
    """Non-archive bytes on the zip path fail as ArchiveError, not a raw BadZipFile."""
    archive = tmp_path / 'notreal.zip'
    dest = tmp_path / 'dest'
    dest.mkdir()
    archive.write_bytes(b'this is definitely not a zip archive\n')
    with pytest.raises(ArchiveError, match='not a valid zip archive'):
        extract_archive(archive, dest, 'zip')
    assert list(dest.iterdir()) == []
