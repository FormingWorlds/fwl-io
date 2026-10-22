"""Key of an ``actions/cache`` entry that holds an FWL data tree.

``actions/cache`` writes an entry only on an exact-key miss and never rewrites one it
hits, so the key has to move whenever the tree it holds would change, and only then. The
key of a selection of manifest datasets is a digest of, per dataset in folder order, the
folder fwl-io places it in, its extract kind and the file checksums its registry pins,
plus the fwl-io release that lays out the tree.

A restore-keys prefix seeds a new entry from an older tree. That is safe when every
dataset resolves into a version folder ``r<record-id>``: a re-pinned dataset lands in a
folder the older tree does not hold, a fetch skips a plain file only when its checksum
matches, and an archive tree only when its stamp records the same registry. A dataset
with no Zenodo pin, which only a :class:`Dataset` built in code can have, keeps its bare
location, where the files of an older pin sit at the same path for any reader that trusts
presence, so a selection holding one gets no restore-keys.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

import fwl_io
from fwl_io.manifest import Dataset

# Keys hold at most 512 characters, see the key input at
# https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching
_PREFIX = re.compile(r'[A-Za-z0-9._-]{1,100}')


@dataclass(frozen=True)
class CacheKey:
    """The cache key of a dataset selection.

    Attributes
    ----------
    key : str
        ``<prefix><sha256>``.
    restore_keys : str
        The prefix when every dataset has a version folder, else empty.
    unversioned : tuple of str
        Keys of the selected datasets without a version folder.
    """

    key: str
    restore_keys: str
    unversioned: tuple[str, ...]


def select_datasets(providers: Iterable[str] = (), keys: Iterable[str] = ()) -> list[Dataset]:
    """Return the datasets of installed manifest providers and of single dataset keys.

    Parameters
    ----------
    providers : iterable of str
        Entry-point names of ``fwl_io.manifests`` providers; each adds all its datasets,
        sorted by key.
    keys : iterable of str
        Dotted dataset keys, added after the providers in the given order.

    Returns
    -------
    list of Dataset
        The selection, in the order that the key hashes and fetch and check walk.

    Raises
    ------
    LookupError
        A provider is not installed or not usable, or no usable manifest declares a key.
    ValueError
        The selection is empty or names a dataset twice.
    """
    from fwl_io.manifest import _discover_all

    discovery = _discover_all()
    selected = []
    for name in providers:
        if name not in discovery.found:
            errors = discovery.errors.items()
            mine = sorted({e.message for k, e in errors if k == name or k.startswith(f'{name} (')})
            why = f': {"; ".join(mine)}' if mine else f'; installed: {sorted(discovery.found)}'
            raise LookupError(f'no installed manifest provider {name!r}{why}')
        selected += sorted(discovery.found[name], key=lambda ds: ds.key)
    declared = {ds.key: ds for found in discovery.found.values() for ds in found}
    for key in keys:
        if key not in declared:
            n = len(discovery.errors)
            unused = f'; {n} manifest(s) not used, see fwl-io list' if n else ''
            raise LookupError(f'no installed manifest declares the dataset {key!r}{unused}')
        selected.append(declared[key])
    _refuse_twice(selected)
    if not selected:
        raise ValueError('no dataset selected: pass --provider or --key')
    return selected


def _refuse_twice(datasets: list[Dataset]) -> None:
    """Raise ValueError when two datasets share a key."""
    names = [ds.key for ds in datasets]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f'dataset(s) selected twice: {twice}')


def _material(ds: Dataset) -> tuple[list[str], bool]:
    """Lines that pin the on-disk layout of one dataset (folder, extract kind, checksums),
    and whether it has a version folder."""
    from fwl_io.manifest import _fetcher_of

    fetcher = _fetcher_of(ds, '.')  # only the layout is read; no file is touched
    lines = [f'dir\t{fetcher.rel_dir}']
    if fetcher.extract:
        lines.append(f'extract\t{fetcher.extract}')
    lines += [f'file\t{name}\t{fetcher.registry[name]}' for name in sorted(fetcher.registry)]
    return lines, fetcher.version_dir is not None


def cache_key(datasets: Iterable[Dataset], prefix: str) -> CacheKey:
    """Return the cache key and restore-keys of a dataset selection.

    Parameters
    ----------
    datasets : iterable of Dataset
        The selection, for example from :func:`select_datasets`; its order does not matter.
    prefix : str
        Leading part of the key and the restore-keys prefix: 1 to 100 letters, digits,
        ``.``, ``_`` and ``-``, so it cannot split a ``GITHUB_OUTPUT`` line and the key
        stays far below the 512 characters ``actions/cache`` takes.

    Returns
    -------
    CacheKey

    Raises
    ------
    ValueError
        The prefix is empty, longer than 100 or holds any other character, or the
        selection is empty or names a dataset key twice.
    """
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(
            f'cache key prefix {prefix!r} must be 1 to 100 letters, digits, ".", "_" or "-"'
        )
    datasets = list(datasets)
    _refuse_twice(datasets)
    if not datasets:
        raise ValueError('no dataset selected: pass --provider or --key')
    blocks = {ds.key: _material(ds) for ds in datasets}
    material = [line for lines, _ in sorted(blocks.values()) for line in lines]
    material.append('fwl-io\t' + fwl_io.__version__.split('+')[0])
    digest = hashlib.sha256('\n'.join(material).encode('utf-8')).hexdigest()
    unversioned = tuple(key for key, (_, versioned) in blocks.items() if not versioned)
    return CacheKey(prefix + digest, '' if unversioned else prefix, unversioned)
