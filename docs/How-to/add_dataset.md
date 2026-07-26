# Add a dataset

Follow these steps to make a new dataset fetchable through fwl-io.

## 1. Deposit the files on Zenodo

Upload the files to Zenodo and note the **version DOI** of the deposit, the DOI of the specific version you just created, of the form `10.5281/zenodo.<record-id>`.

!!! warning "Version DOI, not concept DOI"

    Every Zenodo deposit has two DOIs: the version DOI of each specific deposit and a concept DOI that always resolves to the newest deposit. Manifests must pin the version DOI. `fwl-io sync` rejects concept DOIs, because data that silently updates underneath pinned code is exactly the failure mode fwl-io exists to prevent. Publishing a new version of the data means minting a new version DOI and updating the manifest deliberately.

## 2. Declare the dataset in a manifest

Choose the manifest:

- Data consumed by **several models** goes into the shared manifest in the fwl-io repository (`src/fwl_io/data/shared_manifest.toml`).
- Data consumed by **one model** goes into that model's own manifest, shipped with the model package (see [Migrate a model](migrate_model.md)).

Add a table for the dataset:

```toml
manifest_schema = 1

[interior.eos.wolf_bower_2018]
name = "Wolf & Bower (2018) MgSiO3 equation of state"
zenodo = "10.5281/zenodo.1234567"
required_by = ["aragog", "zalmoxis", "spider"]
```

The root `manifest_schema` names the schema the file is written against. It is optional and worth declaring: it lets fwl-io tell a manifest written for a newer schema from a misspelt field, so a load failure names the one that applies instead of offering both. The current schema is in the [schema versions](../Explanations/manifests.md#schema-versions) table.

The dotted key is the location below `FWL_DATA`, so this dataset lands in `interior/eos/wolf_bower_2018/r<record-id>`, the version directory named for its Zenodo record. Choose the key to follow the [target layout](../Explanations/manifests.md#the-fwl_data-layout), using only letters, digits, `_` and `-` per segment, each starting with a letter, digit or `_`. `required_by` lists the models whose `fwl-io fetch <model>` should include this dataset.

If the deposit is a single archive that consumers expect unpacked, add `extract = "tar"` or `extract = "zip"`; the archive is downloaded, checksum-verified, and unpacked into the dataset directory. See [Archive datasets](../Explanations/manifests.md#archive-datasets).

## 3. Generate the registry

```bash
fwl-io sync path/to/manifest.toml
```

This queries the Zenodo record and writes a registry file next to the manifest (`interior.eos.wolf_bower_2018.registry.txt`, the dotted key) containing every file name and checksum. Commit the manifest change and the registry file together; the checksums are then reviewed like any other change.

## 4. Mirror to Dataverse (optional but encouraged)

Mirror the deposit to Dataverse and add its DOI:

```toml
dataverse = "10.34894/ABCDEF"
```

The mirror must host **byte-identical** copies of the originals; disable Dataverse's tabular ingest for mirrored deposits, since ingest re-encodes tabular files and changes their bytes. Checksums always come from the Zenodo record.

## 5. Ship it

For the shared manifest, open a PR on fwl-io. For a model manifest, open a PR on the model; make sure the manifest and its registry files are included in the model's package data, or fetching fails at runtime on user machines.
