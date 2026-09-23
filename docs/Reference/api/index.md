# API reference

The public API is exposed at the package top level:

```python
from fwl_io import (
    create_fetcher, Fetcher,            # fetching
    load_manifest, discover_manifests,  # manifests
    fetch_for, Dataset,
    check_for, check_dataset,           # validate-only checking
    relocate_all, plan_relocations,         # moving a legacy tree into the current layout
    plan_prune, apply_prune, prune_versions,  # removing unreferenced version directories
    CheckReport, DatasetCheck, FileCheck,
    resolve_data_root, resolve_cache_root,
    DownloadError, OfflineDataError, MissingDataRootError,
    ManifestSchemaError,
)
```

Per-module reference pages:

- [Fetching](fetch.md): `Fetcher`, `create_fetcher`, error types
- [Checking](check.md): `check_for`, `check_dataset`, `CheckReport`, `DatasetCheck`, `FileCheck`
- [Relocating](relocate.md): `relocate_all`, `plan_relocations`, `RelocationReport`, `Relocation`
- [Pruning](prune.md): `plan_prune`, `apply_prune`, `prune_versions`, `PruneReport`, `PruneCandidate`
- [Manifests](manifest.md): `Dataset`, `load_manifest`, `discover_manifests`, `fetch_for`, `ManifestSchemaError`
- [Registries](registry.md): registry file reading and writing
- [Sync](sync.md): registry generation from the Zenodo API
- [Paths](paths.md): data root, shared cache, offline mode
