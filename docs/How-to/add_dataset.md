# Add a dataset

Follow these steps to make a new dataset fetchable through fwl-io. Steps 3 to 5 need fwl-io installed ([Installation](installation.md)); none of them needs a token. The tutorial [Add a dataset, step by step](../Tutorials/add_dataset_walkthrough.md) runs the same steps on a small real record, with the output of each command.

## 1. Bring the files into the PROTEUS Framework community on Zenodo

A dataset of the framework is a record of the [PROTEUS Framework community on Zenodo](https://zenodo.org/communities/proteus_framework), accepted by a curator of that community. fwl-io creates a mirror for no other record: `fwl-io mirror` refuses a record outside the community, and a check of the shared manifest fails for one.

There are four routes into the community. Each one ends with a record in the community and its **version DOI**, of the form `10.5281/zenodo.<record-id>`; steps 2 to 5 below are the same for all of them.

| Route | For whom | Who uploads | What you do |
| --- | --- | --- | --- |
| A. A maintainer uploads on request | You have the files and no Zenodo record, or you prefer not to upload. | A maintainer | Open a [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml) with the route "A maintainer uploads my data" and a download link. |
| B. You upload a new record | You have a Zenodo account and the files. | You | Start a new upload on Zenodo from the page of the community, so that the upload is submitted to the community for review. A curator accepts it, and Zenodo publishes the record. |
| C. You upload a new version | Your record is in the community and the data changed. | You | Create the new version of your record on Zenodo and publish it. Check on its page that the version is in the community; when it is not, submit it as in route D. |
| D. Your record exists | Your record is published on Zenodo and is not in the community. | Nobody | On the page of the record, open the communities menu, choose the PROTEUS Framework community and submit the record. A curator accepts it. |

The Zenodo help describes the two ways to submit: [Submit for review](https://help.zenodo.org/docs/share/submit-for-review/) for a new upload (route B) and [Submit to community](https://help.zenodo.org/docs/share/submit-to-community/) for a published record (routes C and D). When Zenodo does not let you submit to the community, open a dataset request with the route "A maintainer uploads my data" and say so in its description.

### The dataset request

Every route has one [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml), an issue form on the fwl-io repository. For route A it starts the work. For routes B, C and D it tells the maintainers which record waits for a curator. For all of them it is also the request for the mirror of step 4. The form asks for:

| Field | What to enter |
| --- | --- |
| Route | "A maintainer uploads my data" (route A), "I upload the record myself and submit it to the community" (routes B and C), or "My record exists on Zenodo; add it to the community" (route D) |
| New dataset or new version | one of the two |
| Zenodo DOI | the version DOI of your record for routes B, C and D, as soon as it exists; empty for route A |
| Download link | for route A only: a folder on Google Drive, Dropbox or another service, shared so that anyone with the link can read it. The form cannot enforce it; a request of route A without a link is not complete. |
| Title, Short description | as the Zenodo record must show them |
| Models that read the data, Proposed dataset key | the models, by the names that `required_by` takes (step 2), and your proposal for the dotted key, which step 2 explains. A maintainer can ask for another key in the issue; write the manifest when the key is settled there. |
| Files | one line per file, the README too, with its size in any unit |
| Licence | your choice among the licences that Zenodo offers, for example CC-BY-4.0 |
| Authors, Contact, Reference to cite | each author with affiliation and ORCID; the person who answers questions; the DOI of the publication to cite |
| README | you confirm that the data holds a README, a text file among the files (for example `README.txt`), that describes each file (format, columns, units, source). A record without one needs a new version with a README before it is accepted. |

A dataset request counts as a request for a mirror. Write into the short description what else the maintainers must know: that you want no mirror, or that you ask a maintainer to do steps 2 to 5 for you; the maintainer answers in the issue.

A maintainer answers in the issue. A request is accepted when the form is complete and the data fits the community; a request that is refused gets the reason there, and you can correct the request in the same issue.

### Route A in steps

1. You open the dataset request with the download link.
2. A maintainer downloads the files, uploads them to Zenodo with the metadata of the form, brings the record into the community and publishes it.
3. The maintainer writes the version DOI into the issue.
4. You continue with step 2 below.

### Routes B, C and D in steps

1. You upload or submit on Zenodo, as the table says, and open the dataset request with the DOI.
2. A curator of the community accepts the record on Zenodo; the page of the record then shows the community. Until then the record is not in the community: `fwl-io sync` works for it, and the mirror and the check of the shared manifest refuse it. To test it yourself, run `fwl-io mirror <version DOI> --collection Proteus_Fr --dry-run`: it writes nothing, needs no token, and ends with an error that names the community for a record that is not accepted. When the curator does not answer or declines, ask in the issue.
3. You continue with step 2 below. You do not have to wait for the curator to write the manifest and the registry; the pull request of a dataset in the shared manifest passes its check only when the record is accepted, and a model manifest has no such check.

!!! warning "Version DOI, not concept DOI"

    Every Zenodo deposit has two DOIs: the version DOI of each specific deposit and a concept DOI that always resolves to the newest deposit. Manifests must pin the version DOI. `fwl-io sync` rejects concept DOIs, because data that silently updates underneath pinned code is exactly the failure mode fwl-io exists to prevent. Publishing a new version of the data means minting a new version DOI and updating the manifest deliberately: see [Update a dataset or its mirror](update_dataset.md).

## 2. Declare the dataset in a manifest

Clone the repository that holds the manifest (your fork of it when you have no write access), and choose the manifest:

- Data consumed by **several models** goes into the shared manifest in the fwl-io repository (`src/fwl_io/data/shared_manifest.toml`).
- Data consumed by **one model** goes into that model's own manifest, a `manifest.toml` in the source of the model package, in the repository of that model ([Migrate a model](migrate_model.md) shows where it is and how a model without one gets it).

Add a table for the dataset, with the key of the dataset request (the line `manifest_schema` stands once at the top of the file; add it when the manifest has none):

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

This queries the Zenodo record and writes a registry file next to the manifest (`interior.eos.wolf_bower_2018.registry.txt`, the dotted key) containing every file name and checksum. The command needs network access and no token; run it in the root of your clone. Commit the manifest change and the registry file together; the checksums are then reviewed like any other change.

## 4. Mirror to Dataverse (optional but encouraged)

The dataset request of step 1 is also the request for a mirror of the record on DataverseNL: [A mirror for my dataset](update_dataset.md#a-mirror-for-my-dataset) says what the maintainer runs. When the mirror is published, the maintainer writes its DOI into the issue. Add that DOI to the table of your dataset, below its `zenodo` line:

```toml
dataverse = "10.34894/ABCDEF"
```

The mirror must host **byte-identical** copies of the originals: the mirror workflow downloads each file, checks it against the Zenodo checksum, uploads it, and compares the uploaded file with that copy. Checksums always come from the Zenodo record. Run `fwl-io check-mirrors`, which takes no argument and reads the manifests of the installed packages (install yours with `pip install -e .` in the clone), to confirm that the pin serves the dataset ([how to read its output](update_dataset.md#the-dataset-owner-pins-the-mirror)). The registry does not change, so no new `fwl-io sync` is needed. The pull request of step 5 does not have to wait for the mirror: the `dataverse` line can follow in a second pull request.

## 5. Ship it

For the shared manifest, open a PR on fwl-io: it gets a review request to the team `proteus-maintainer` and needs the approval of a code owner. For a model manifest, open a PR on the repository of the model, from a fork when you have no write access; make sure the manifest and its registry files are included in the model's package data, or fetching fails at runtime on user machines.
