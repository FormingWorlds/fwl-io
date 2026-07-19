# API reference

The public API is exposed at the package top level:

```python
from fwl_io import (
    create_fetcher, Fetcher,            # fetching
    load_manifest, discover_manifests,  # manifests
    fetch_for, Dataset,
    resolve_data_root, resolve_cache_root,
    DownloadError, OfflineDataError, MissingDataRootError,
)
```

Per-module reference pages:

- [Fetching](fetch.md): `Fetcher`, `create_fetcher`, error types
- [Manifests](manifest.md): `Dataset`, `load_manifest`, `discover_manifests`, `fetch_for`
- [Registries](registry.md): registry file reading and writing
- [Sync](sync.md): registry generation from the Zenodo API
- [Paths](paths.md): data root, shared cache, offline mode
