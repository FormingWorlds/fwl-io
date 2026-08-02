# API reference

The public API is exposed at the package top level:

```python
from fwl_io import (
    create_fetcher, Fetcher,            # fetching
    load_manifest, discover_manifests,  # manifests
    fetch_for, Dataset,
    check_for, check_dataset,           # validate-only checking
    CheckReport, DatasetCheck, FileCheck,
    resolve_data_root, resolve_cache_root,
    DownloadError, OfflineDataError, MissingDataRootError,
    ManifestSchemaError,
)
```

Per-module reference pages:

- [Fetching](fetch.md): `Fetcher`, `create_fetcher`, error types
- [Checking](check.md): `check_for`, `check_dataset`, `CheckReport`, `DatasetCheck`, `FileCheck`
- [Manifests](manifest.md): `Dataset`, `load_manifest`, `discover_manifests`, `fetch_for`, `ManifestSchemaError`
- [Registries](registry.md): registry file reading and writing
- [Sync](sync.md): registry generation from the Zenodo API
- [Paths](paths.md): data root, shared cache, offline mode
