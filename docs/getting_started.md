# Getting started

## Install

```bash
pip install fwl-io
```

Until the first PyPI release lands, install from the repository instead:

```bash
pip install git+https://github.com/FormingWorlds/fwl-io.git
```

fwl-io requires Python 3.11 or newer. For development installs see [Installation](How-to/installation.md).

## Configure the data root

fwl-io writes into the shared FWL data tree, whose root is resolved from the `FWL_DATA` environment variable:

```bash
export FWL_DATA=~/fwl_data
```

There is no silent default location: if `FWL_DATA` is unset and no explicit path is passed, fwl-io raises an error. This mirrors how the rest of the ecosystem treats the variable and prevents data from quietly landing in the wrong place on shared machines.

## Fetch data

From Python:

```python
from fwl_io import create_fetcher, fetch_for

eos = create_fetcher(
    subdir='interior_lookup_tables/MgSiO3_demo',
    zenodo='10.5281/zenodo.1234567',
    registry='interior_lookup_tables.MgSiO3_demo.registry.txt',
)
path = eos.fetch('density.dat')      # verified, cached, atomic
paths = fetch_for('mors')            # everything a model requires
```

From the command line:

```bash
fwl-io list          # datasets from all installed manifests
fwl-io fetch mors    # fetch everything MORS requires
```

## Offline machines

Set `FWL_IO_OFFLINE=1` to forbid all network access; files then resolve from the local tree or fail with an actionable error. On clusters, point `FWL_DATA_CACHE` at a read-only, pre-populated group share and fwl-io copies from it instead of downloading. See [Run on clusters](How-to/clusters.md).
