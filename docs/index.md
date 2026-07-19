# fwl-io

fwl-io is the shared data-download package of the [PROTEUS framework](https://proteus-framework.org/). It gives every model in the ecosystem one mechanism to declare, verify, and fetch the reference data they depend on: spectral files, equation-of-state lookup tables, stellar evolution tracks, and similar datasets hosted on Zenodo with Dataverse mirrors.

!!! note "Status"

    fwl-io is at version 0, released for inspection and feedback by the collaboration. The API is not yet frozen; suggestions are welcome as [GitHub issues](https://github.com/FormingWorlds/fwl-io/issues).

## Why fwl-io exists

Until now, each model (PROTEUS, MORS, JANUS, Aragog, Zalmoxis, SPIDER) carried its own download code with independently hardcoded record ids, differing retry behavior, and no shared verification. Two machines could silently hold different versions of the same dataset. fwl-io replaces that with:

- **Manifests**: datasets declared in TOML with their location below `FWL_DATA`, a pinned Zenodo version DOI, an optional Dataverse mirror, and the models that require them.
- **Committed registries**: file names and checksums generated from the Zenodo API by `fwl-io sync`, pinned in version control, reviewed like code.
- **Verified, mirrored, atomic fetching**: every file checked against its registry hash, tried against Zenodo first and the mirror second, and moved into place atomically.
- **Offline-first operation**: full support for cluster compute nodes without internet access, including a read-only group cache consulted before any download.

The motivation and discussion history live in [PROTEUS issue #605](https://github.com/FormingWorlds/PROTEUS/issues/605) and [discussion #592](https://github.com/orgs/FormingWorlds/discussions/592).

## Get started

<div class="grid cards" markdown>

-   :material-download: **Install fwl-io**

    Install the package, set the data root, and fetch your first
    dataset.

    [Getting started](getting_started.md){ .md-button .md-button--primary }

-   :material-swap-horizontal: **Migrating a model?**

    The step-by-step guide for maintainers replacing their model's
    download code with fwl-io.

    [Migration guide](How-to/migrate_model.md){ .md-button .md-button--primary }

-   :material-lightbulb-outline: **Reviewing the design?**

    Every design decision with its rationale, one section each, ready
    for your feedback.

    [Design decisions](Explanations/design.md){ .md-button .md-button--primary }

</div>

Adding a new dataset instead? See [Add a dataset](How-to/add_dataset.md). Running on a cluster? See [Run on clusters](How-to/clusters.md).
