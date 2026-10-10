# Add a dataset

Follow these steps to make a new dataset fetchable through fwl-io. The tutorial [Add a dataset, step by step](../Tutorials/add_dataset_walkthrough.md) runs the same steps on a small real record, with the output of each command.

## 1. Bring the files into the PROTEUS Framework community on Zenodo

A dataset of the framework is a record of the [PROTEUS Framework community on Zenodo](https://zenodo.org/communities/proteus_framework), accepted by a curator of that community. fwl-io mirrors no other record: `fwl-io mirror` refuses a record outside the community, and a check of the shared manifest fails for one.

There are four routes into the community. Each one ends with a record in the community and its **version DOI**, of the form `10.5281/zenodo.<record-id>`; steps 2 to 5 below are the same for all of them.

| Route | Who uploads | What you do |
| --- | --- | --- |
| A. A maintainer uploads on request | A maintainer | Open a [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml) with the route "A maintainer uploads my data" and a download link to a folder that the maintainers can read. |
| B. You upload a new record | You | Start a new upload on Zenodo from the community page, so that the upload is submitted to the community for review. A curator accepts it, and Zenodo publishes the record. |
| C. You upload a new version | You | Create the new version of your record on Zenodo and publish it. Check on its page that the version is in the community; when it is not, submit it as in route D. |
| D. Your record exists | Nobody | On the page of the record, open the communities menu, choose the PROTEUS Framework community and submit the record. A curator accepts it. |

The Zenodo help describes the two ways to submit: [Submit for review](https://help.zenodo.org/docs/share/submit-for-review/) for a new upload (route B) and [Submit to community](https://help.zenodo.org/docs/share/submit-to-community/) for a published record (routes C and D). When Zenodo does not let you submit to the community, open a [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml).

For routes B, C and D, also open a [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml) with the matching route: it tells the maintainers which record waits for a curator, and it is the request for the mirror of step 4. A maintainer accepts a request when the form is complete and the record can be a record of the community, and says so in the issue; a request that is refused gets the reason there.

**Route A in steps.**

1. You open the dataset request. The form asks for: the title and a short description, the models that read the data, a proposed dataset key, the files with their sizes, the licence, the authors with affiliation and ORCID, a contact, the reference to cite, whether this is a new dataset or a new version, the download link, and your confirmation that the data holds a README that describes each file (format, columns, units, source).
2. A maintainer downloads the files, uploads them to Zenodo as a record of the community with the metadata of the form, and publishes it.
3. The maintainer writes the version DOI into the issue.
4. You continue with step 2 below, or the maintainer does when the request says so.

!!! warning "Version DOI, not concept DOI"

    Every Zenodo deposit has two DOIs: the version DOI of each specific deposit and a concept DOI that always resolves to the newest deposit. Manifests must pin the version DOI. `fwl-io sync` rejects concept DOIs, because data that silently updates underneath pinned code is exactly the failure mode fwl-io exists to prevent. Publishing a new version of the data means minting a new version DOI and updating the manifest deliberately: see [Update a dataset or its mirror](update_dataset.md).

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

The root `manifest_schema` names the schema the file is written against. It is optional and worth declaring: it lets fwl-io tell a manifest written for a different schema from a misspelt field, so a load failure names the one that applies instead of offering both. Declaring it also means the manifest has to be updated when the schema number rises, which is the point, since that is when manifests written for the previous number stop loading. The current schema is in the [schema versions](../Explanations/manifests.md#schema-versions) table.

The dotted key is the location below `FWL_DATA`, so this dataset lands in `interior/eos/wolf_bower_2018/r<record-id>`, the version directory named for its Zenodo record. Choose the key to follow the [target layout](../Explanations/manifests.md#the-fwl_data-layout), using only letters, digits, `_` and `-` per segment, each starting with a letter, digit or `_`. `required_by` lists the models whose `fwl-io fetch <model>` should include this dataset.

If the deposit is a single archive that consumers expect unpacked, add `extract = "tar"` or `extract = "zip"`; the archive is downloaded, checksum-verified, and unpacked into the dataset directory. See [Archive datasets](../Explanations/manifests.md#archive-datasets).

If the deposit holds files this dataset does not need, add `files = ["name1", "name2"]` to list the ones it does. The registry then lists only those, and fetch, check and mirror handle only those. See [Partial datasets](../Explanations/manifests.md#partial-datasets).

## 3. Generate the registry

```bash
fwl-io sync path/to/manifest.toml
```

This queries the Zenodo record and writes a registry file next to the manifest (`interior.eos.wolf_bower_2018.registry.txt`, the dotted key) containing every file name and checksum. Commit the manifest change and the registry file together; the checksums are then reviewed like any other change.

## 4. Mirror to Dataverse (optional but encouraged)

The dataset request of step 1 is also the request for a mirror of the record on DataverseNL: [A mirror for my dataset](update_dataset.md#a-mirror-for-my-dataset) says what the maintainer runs. Add the DOI of the published mirror:

```toml
dataverse = "10.34894/ABCDEF"
```

The mirror must host **byte-identical** copies of the originals: the mirror workflow downloads each file, checks it against the Zenodo checksum, uploads it, and compares the uploaded file with that copy. Checksums always come from the Zenodo record. Run `fwl-io check-mirrors` to confirm that the pin serves the dataset.

## 5. Ship it

For the shared manifest, open a PR on fwl-io: it gets a review request to the team `proteus-maintainer` and needs the approval of a code owner. For a model manifest, open a PR on the model; make sure the manifest and its registry files are included in the model's package data, or fetching fails at runtime on user machines.
