"""Key of an ``actions/cache`` entry that holds an FWL data tree.

``actions/cache`` writes an entry only on an exact-key miss and never rewrites one it
hits, so the key has to move whenever the tree it holds would change, and only then. The
key of a selection of manifest datasets is a digest of, per dataset, the folder fwl-io
places it in, its extract kind and the file checksums its registry pins, plus the fwl-io
release (year.month) that lays out the tree.

A restore-keys prefix seeds a new entry from an older tree. That is safe when every
dataset resolves into a version folder ``r<record-id>``: a re-pinned dataset lands in a
folder the older tree does not hold, and a fetch skips a file only when its checksum
matches. A dataset with no Zenodo pin keeps its bare location, where the files of an older
pin sit at the same path for any reader that trusts presence, so a selection holding one
gets no restore-keys.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import fwl_io
from fwl_io.manifest import Dataset

_PREFIX = re.compile(r'[A-Za-z0-9._-]+')


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
            error = discovery.errors.get(name)
            why = f': {error.message}' if error else f'; installed: {sorted(discovery.found)}'
            raise LookupError(f'no installed manifest provider {name!r}{why}')
        selected += sorted(discovery.found[name], key=lambda ds: ds.key)
    declared = {ds.key: ds for found in discovery.found.values() for ds in found}
    for key in keys:
        if key not in declared:
            raise LookupError(f'no installed manifest declares the dataset {key!r}')
        selected.append(declared[key])
    names = [ds.key for ds in selected]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise ValueError(f'dataset(s) selected twice: {twice}')
    if not selected:
        raise ValueError('no dataset selected: pass --provider or --key')
    return selected


def _material(ds: Dataset) -> list[str]:
    """Lines that pin the on-disk layout of one dataset: folder, extract kind, checksums."""
    from fwl_io.manifest import _fetcher_of

    with tempfile.TemporaryDirectory() as tmp:
        fetcher = _fetcher_of(ds, tmp)
    lines = [f'dir\t{fetcher.rel_dir}']
    if fetcher.extract:
        lines.append(f'extract\t{fetcher.extract}')
    return lines + [f'file\t{name}\t{fetcher.registry[name]}' for name in sorted(fetcher.registry)]


def cache_key(datasets: Sequence[Dataset], prefix: str) -> CacheKey:
    """Return the cache key and restore-keys of a dataset selection.

    Parameters
    ----------
    datasets : sequence of Dataset
        The selection, for example from :func:`select_datasets`; its order is hashed.
    prefix : str
        Leading part of the key and the restore-keys prefix: letters, digits, ``.``,
        ``_`` and ``-`` only, so it cannot split a ``GITHUB_OUTPUT`` line.

    Returns
    -------
    CacheKey

    Raises
    ------
    ValueError
        The prefix holds any other character or is empty, or the selection is empty.
    """
    if not _PREFIX.fullmatch(prefix):
        raise ValueError(
            f'cache key prefix {prefix!r} must be letters, digits, ".", "_" or "-" only'
        )
    if not datasets:
        raise ValueError('no dataset selected: pass --provider or --key')
    material = [line for ds in datasets for line in _material(ds)]
    material.append('fwl-io\t' + '.'.join(fwl_io.__version__.split('.')[:2]))
    digest = hashlib.sha256('\n'.join(material).encode('utf-8')).hexdigest()
    # The fetcher gives a version folder exactly when the dataset has a Zenodo pin.
    unversioned = tuple(ds.key for ds in datasets if not ds.zenodo)
    return CacheKey(prefix + digest, '' if unversioned else prefix, unversioned)
