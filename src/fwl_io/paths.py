"""Resolution of the data root, the optional shared cache, and offline mode.

The data root is resolved in this order:

1. An explicit path passed by the caller.
2. The ``FWL_DATA`` environment variable.

There is deliberately no silent fallback: the FWL ecosystem treats
``FWL_DATA`` as required, and inventing a default location would hide
misconfiguration (on clusters it would quietly download gigabytes into a
per-user directory instead of the shared tree). An unset root raises
:class:`MissingDataRootError` with instructions.

``FWL_DATA_CACHE`` may point at a read-only, pre-populated copy of the data
tree (for example a group-shared directory on a cluster). It is searched
before any download is attempted and is never written to.

``FWL_IO_OFFLINE`` disables all network access when set to a truthy value
(``1``, ``true``, ``yes``, ``on``). In offline mode a missing or corrupt file
raises an error instead of triggering a download.
"""

from __future__ import annotations

import os
from pathlib import Path

_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


class MissingDataRootError(RuntimeError):
    """No data root is configured: FWL_DATA is unset and no path was given."""


def resolve_data_root(explicit: str | Path | None = None) -> Path:
    """Return the writable root of the FWL data tree, creating it if needed.

    Parameters
    ----------
    explicit : str | Path | None
        Caller-supplied override. When None, ``FWL_DATA`` must be set.

    Returns
    -------
    Path
        Absolute path to the data root directory.

    Raises
    ------
    MissingDataRootError
        When neither an explicit path nor ``FWL_DATA`` is available.
    """
    if explicit is not None:
        root = Path(explicit)
    elif os.environ.get('FWL_DATA'):
        root = Path(os.environ['FWL_DATA'])
    else:
        raise MissingDataRootError(
            'no data root configured: set the FWL_DATA environment variable '
            'or pass an explicit data_root path'
        )
    root = root.expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_cache_root() -> Path | None:
    """Return the read-only shared cache root, or None if not configured.

    The cache is configured through the ``FWL_DATA_CACHE`` environment
    variable and is only used when the directory actually exists.
    """
    raw = os.environ.get('FWL_DATA_CACHE')
    if not raw:
        return None
    cache = Path(raw).expanduser().absolute()
    if not cache.is_dir():
        return None
    return cache


def is_offline() -> bool:
    """Return True when ``FWL_IO_OFFLINE`` requests fully offline operation."""
    return os.environ.get('FWL_IO_OFFLINE', '').strip().lower() in _TRUTHY
