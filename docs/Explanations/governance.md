# Data governance

This page states who may change the data the ecosystem depends on, and how such a change is reviewed and tested. The mechanism behind it is described in [Design decisions](design.md) and [Manifests and registries](manifests.md); this page states the policy that the mechanism enforces.

## What a data change is

A dataset is pinned by two committed files: the Zenodo version DOI in a manifest, and the registry file beside it that lists the dataset's files and their checksums. A data change is a change to those. Nothing else alters the data a model receives, because a fetch reads the committed registry, not the live Zenodo record.

fwl-io provides the manifest for shared datasets. A model's own datasets are declared in that model's manifest, installed with the model through the `fwl_io.manifests` entry point. Ownership follows the manifest: a shared dataset is changed in fwl-io, a model dataset in the model that owns it.

## Who may change data, and how

A data change is a code change and takes the same path. There is no separate data-admin role: anyone who can open a pull request on the owning repository can propose one, and it is reviewed and merged under that repository's normal permissions.

To change a dataset:

1. Edit the `zenodo` version DOI in the owning manifest.
2. Run `fwl-io sync <manifest>` to regenerate the committed registry from the Zenodo record.
3. Commit the manifest and the registry together, and open a pull request.

The registry diff shows the new file list and checksums, so a reviewer sees exactly which files and hashes change. `fwl-io sync` rejects a concept DOI, so a manifest can pin only a fixed version, and the data cannot change under pinned code without a visible manifest edit.

## How data is tested

The committed registry is the contract a fetch trusts, so the test is whether that contract still matches its source. A scheduled workflow runs the slow test tier once a week. It fetches the live Zenodo registry of every dataset in the shared manifest and compares it to the committed registry, and it fails if the two have drifted. A change made to a Zenodo record outside a reviewed `fwl-io sync` is therefore caught by the scheduled run, not by a user's failing fetch.

The scheduled drift check in fwl-io covers only the shared manifest. It runs no equivalent check against a model manifest, so drift protection for a model's own datasets is the responsibility of that model's repository, where the same pull-request review already applies to every data change.
