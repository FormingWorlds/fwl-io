# Migrate a model to fwl-io

This guide is for maintainers of ecosystem models (MORS, Aragog, JANUS, Zalmoxis, SPIDER, PROTEUS) replacing their own download code with fwl-io.

## 1. Add the dependency

In the model's `pyproject.toml`:

```toml
dependencies = [
    "fwl-io",
]
```

The model depends on fwl-io, never on PROTEUS. `zenodo-get` and `osfclient` can be dropped once migration is complete.

## 2. Write the model's manifest

Add a manifest file inside the package, for example `src/mors/data/manifest.toml`, declaring every dataset the model downloads today. Migration is the moment a model's data paths change: declare each dataset at its place in the [FWL_DATA target layout](../Explanations/manifests.md#the-fwl_data-layout), not at the legacy location the model used before:

```toml
[star.tracks.spada_2013]
name = "Spada et al. (2013) stellar evolution tracks"
zenodo = "10.5281/zenodo.1234567"
required_by = ["mors"]
```

The dotted key is the dataset location below `FWL_DATA`, so this table places the tracks in `star/tracks/spada_2013`.

## 3. Generate and commit the registries

```bash
fwl-io sync src/mors/data/manifest.toml
```

Commit the registry files next to the manifest.

## 4. Register the manifest entry point

In the model's `pyproject.toml`:

```toml
[project.entry-points."fwl_io.manifests"]
mors = "mors.data:manifest_path"
```

where `mors/data/__init__.py` provides:

```python
from pathlib import Path

def manifest_path() -> Path:
    return Path(__file__).parent / 'manifest.toml'
```

## 5. Ship the data files with the package

The manifest and registries must be included in the package's data, or fetching fails at runtime on user machines:

```toml
[tool.setuptools.package-data]
mors = ["data/*.toml", "data/*.registry.txt"]
```

## 6. Replace the download calls

Where the model previously ran its own download logic:

```python
from fwl_io import fetch_for

fetch_for('mors')                      # everything the model requires
```

or, for one dataset with direct control:

```python
import importlib.resources

from fwl_io import create_fetcher

tracks = create_fetcher(
    subdir='star/tracks/spada_2013',
    zenodo='10.5281/zenodo.1234567',
    registry=importlib.resources.files('mors.data') / 'star.tracks.spada_2013.registry.txt',
)
tracks.fetch('track_0100.dat')
```

Then delete the model's own download module.

## 7. Keep tests offline

PR-tier tests must not hit the network. Point the fetcher at local fixtures with `base_urls` and a temporary `data_root`, or pre-populate a temporary tree and run with `FWL_IO_OFFLINE=1`. See the fwl-io test suite for the local-server pattern.
