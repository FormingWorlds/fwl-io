# Add a dataset

Follow these steps to make a new dataset fetchable through fwl-io. The commands of steps 1, 3 and 4 need fwl-io installed ([Installation](installation.md)); none of them needs a token. The tutorial [Add a dataset, step by step](../Tutorials/add_dataset_walkthrough.md) runs the same steps on a small real record, with the output of each command.

## 1. Bring the files into the PROTEUS Framework community on Zenodo

A dataset of the framework is an accepted record of one of two Zenodo communities: the [PROTEUS Framework community](https://zenodo.org/communities/proteus_framework) (`proteus_framework`) or the PALEOS community (`paleos`). "Accepted" means that a maintainer of the community has accepted the record into it; a record that is submitted and waits is not accepted. fwl-io creates a mirror for no other record, publishes a mirror draft only for such a record, and checks each record of the shared manifest against this rule. A record of the PALEOS community needs no route of this section: open the dataset request with its mirror route when you want a mirror, and go to step 2.

The maintainers of the framework are the curators of the PROTEUS Framework community: a maintainer accepts a record that is submitted to it.

There are four routes into the community. Each one ends with an accepted record and its **version DOI**, of the form `10.5281/zenodo.<record-id>`; steps 2 to 5 below are the same for all of them, except that route B waits for the acceptance before step 3.

| Route | For whom | Who uploads | What you do on Zenodo |
| --- | --- | --- | --- |
| A. A maintainer uploads on request | You have the files and no Zenodo record, or you prefer not to upload. | A maintainer | Nothing. |
| B. You upload a new record | You have a Zenodo account and the files. | You | Open the page of the community and select **New upload** there, so that the form shows the community. Add the files and the metadata and select **Submit for review**. The upload stays a draft, which nobody else can read, until a maintainer accepts it; Zenodo then publishes it. |
| C. You make a new version | Your record is in a community of the framework and the data changed. | You | On the page of the record, select **New version**, add the files and publish. Only a person with edit access to the record sees that button. Check on the page of the new version that it shows the community; when it does not, submit it as in route D. |
| D. Your record exists | Your record is published on Zenodo and is not in a community of the framework. | Nobody | On the page of the record, open the communities menu (the cog-wheel icon), select **Submit to community**, and select the PROTEUS Framework community. The record stays public; a maintainer accepts it into the community. |

The Zenodo help has the details: [Submit for review](https://help.zenodo.org/docs/share/submit-for-review/) (route B), [Manage versions](https://help.zenodo.org/docs/deposit/manage-versions/) (route C) and [Submit to community](https://help.zenodo.org/docs/share/submit-to-community/) (route D). When Zenodo does not let you submit to the community, open a dataset request with the route "A maintainer uploads my data" and say so in its notes.

A new version of a record can only be made by a person with edit access to it. A record that a maintainer uploaded for you (route A) is shared with you with that access, so you or a maintainer can make its next version. For a record of your own, you make the version (route C), or you give a maintainer edit access with the **Share** button of the record ([User sharing](https://help.zenodo.org/docs/share/user-sharing/)) and open a dataset request with route A.

### The dataset request

Every route has one [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml), an issue form on the fwl-io repository; it needs a GitHub account. For route A it starts the work. For routes B, C and D it tells the maintainers which record waits for them. For all of them it is also the request for the mirror of step 4, and the form has a route for a record that needs a mirror alone. The form says for each field which route needs it. Have this ready:

| Route | Have ready |
| --- | --- |
| Every route | a proposal for the dataset key (step 2 explains it; a maintainer confirms it, or asks for another one, in a comment, so write the manifest after that), the models that read the data, the repository that holds the manifest, a contact (GitHub handle or email address) |
| A, in the form "A maintainer uploads my data" | a folder on Google Drive, Dropbox or another service, shared so that anyone with the link can read it, with the files and a README (a text file, for example `README.txt`, that describes each file: format, columns, units, source); a list of the files with their sizes, the README too; the title and the description of the record, the licence, the authors with affiliation and ORCID, the DOI of the publication to cite. For a new version, also the version DOI of the version that it replaces. |
| B, in the form "I upload the record myself and submit it to the community" | the version DOI, in a comment of the issue, when the record is published |
| C, the same route in the form | the version DOI of the new version |
| D, in the form "My record exists on Zenodo; add it to the community", or a mirror alone, "My record is an accepted record; I ask for a mirror" | the version DOI of the record |

For routes B, C and D the record itself must hold the README. A published record without one needs a new version with a README (**New version** on its page); you submit that version, and its version DOI is the one for the request and the manifest. An upload in review (route B) has no such button: add the README to the upload, or ask the maintainer in the issue to add it, before it is accepted.

A dataset request counts as a request for a mirror unless its notes say otherwise. The notes are also the place for what is not part of the record: that you ask a maintainer to do steps 2 to 5 for you, that you are not a developer of the model, or that Zenodo did not let you submit. A maintainer answers in the issue. A request is accepted when the fields of its route are complete and the data fits the community; a request that is refused gets the reason there, and you can correct the request in the same issue.

### Route A in steps, for the requester

1. You put the files and the README into one shared folder, and open the dataset request with the download link. The field Zenodo DOI stays empty for a new dataset; for a new version it takes the DOI of the version that it replaces.
2. A maintainer uploads the files to Zenodo as a record of the community (next section) and writes the version DOI into the issue. The record is then public and accepted.
3. When you have a Zenodo account with a public profile, the maintainer shares the record with you, with edit access. Without a Zenodo account you can still do the steps below.
4. You continue with step 2 below.

### Route A in steps, for the maintainer

You have an issue with a download link. The names in bold are those of the Zenodo pages ([Create new upload](https://help.zenodo.org/docs/deposit/create-new-upload/), [Describe records](https://help.zenodo.org/docs/deposit/describe-records/)).

1. Check the request: the fields of route A are complete, and the key follows the [target layout](../Explanations/manifests.md#the-fwl_data-layout). Confirm the key in a comment, or ask for another one. Download the files of the link (unpack them when the service gives one archive), and compare their names and approximate sizes with the **Files** field of the request. Check that the README is there and describes each file. When something differs, ask in the issue and stop.
2. Sign in to Zenodo with your own account. Open the [page of the community](https://zenodo.org/communities/proteus_framework) and select **New upload**; the header of the form then shows the community.
3. Select **Upload files** and add every file.
4. Fill in the form from the request: **Resource type** Dataset; the title from Title; **Creators** from Authors, each with family name, given names, the ORCID as name identifier and the affiliation; the description from Description, with a last line "Reference: https://doi.org/" and the DOI of Reference to cite; the license, under **Licenses and rights**, from Licence (CC-BY-4.0 is "Creative Commons Attribution 4.0 International"). Leave the other fields as the form sets them.
5. Select **Submit for review**. A maintainer accepts the submission, you or another one: on the page of the community open the **Requests** tab and select **Accept and publish** ([Review submissions](https://help.zenodo.org/docs/communities/review-submissions/)). Zenodo then publishes the record. For a new version of a record that you can edit, open the record, select **New version**, select **Import files** or add the files, and select **Publish**.
6. Give edit access, when the record is published: on its page select **Share**, then **Add people**; search the requester (ask in the issue for the Zenodo user name) and every other member of the team `proteus-maintainer` in the **User** field, select **Can edit** under **Access**, and select **Add**. Only a user with a public Zenodo profile can be found; note in the issue who could not be added.
7. Write the version DOI of the record, the DOI that its page shows, into a comment of the issue. Continue with [the mirror](update_dataset.md#a-maintainer-creates-the-mirror) unless the notes say that none is wanted.

### Routes B, C and D in steps

1. You upload or submit on Zenodo, as the table of routes says, and open the dataset request with the version DOI (route B: add it in a comment when the record is published).
2. A maintainer accepts the record on Zenodo. The page of the record then shows the community: that is the test for "accepted". A second test is `fwl-io mirror <version DOI> --collection Proteus_Fr --dry-run`, which writes nothing and needs no token, but downloads every file of the record: it ends with an error that names the communities for a record that is not accepted. When no maintainer answers, or the submission is declined, ask in the issue.
3. You continue with step 2 below. For routes C and D the record is public before it is accepted, so `fwl-io sync` works at once and you can write the manifest and the registry while you wait. For route B the upload is not public before it is accepted, so `fwl-io sync` cannot read it: wait for the acceptance. The mirror, and the check of a pull request that changes the shared manifest, need the accepted record; a model manifest has no such check.

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

`name` is a label that `fwl-io list` prints below the key. The dotted key is the location below `FWL_DATA`, so this dataset lands in `interior/eos/wolf_bower_2018/r<record-id>`, the version directory named for its Zenodo record. Choose the key to follow the [target layout](../Explanations/manifests.md#the-fwl_data-layout), using only letters, digits, `_` and `-` per segment, each starting with a letter, digit or `_`. `required_by` lists the models whose `fwl-io fetch <model>` should include this dataset.

If the deposit is a single archive that consumers expect unpacked, add `extract = "tar"` or `extract = "zip"`; the archive is downloaded, checksum-verified, and unpacked into the dataset directory. See [Archive datasets](../Explanations/manifests.md#archive-datasets).

If the deposit holds files this dataset does not need, add `files = ["name1", "name2"]` to list the ones it does; without the line the fetch takes every file, the README too. The registry then lists only those, and fetch, check and mirror handle only those. See [Partial datasets](../Explanations/manifests.md#partial-datasets).

## 3. Generate the registry

```bash
fwl-io sync path/to/manifest.toml
```

This queries the Zenodo record and writes a registry file next to the manifest (`interior.eos.wolf_bower_2018.registry.txt`, the dotted key) containing every file name and checksum. The command needs network access and no token; run it in the root of your clone. Commit the manifest change and the registry file together; the checksums are then reviewed like any other change.

## 4. Mirror to Dataverse (optional but encouraged)

The dataset request of step 1 is also the request for a mirror of the record on DataverseNL: [A mirror for my dataset](update_dataset.md#a-mirror-for-my-dataset) says what the maintainer runs. The mirror is optional: say in the notes of the request when you want none. When the mirror is published, the maintainer writes its DOI into the issue. Add that DOI to the table of your dataset, below its `zenodo` line:

```toml
dataverse = "10.34894/ABCDEF"
```

The mirror must host **byte-identical** copies of the originals: the mirror workflow downloads each file, checks it against the Zenodo checksum, uploads it, and compares the uploaded file with that copy. Checksums always come from the Zenodo record. Run `fwl-io check-mirrors`, which takes no argument and reads the manifests of the installed packages (install yours with `pip install -e .` in the clone), to confirm that the mirror of the `dataverse` line serves the dataset ([how to read its output](../Reference/cli.md#fwl-io-check-mirrors)). The registry does not change, so no new `fwl-io sync` is needed. The pull request of step 5 does not have to wait for the mirror: the `dataverse` line can follow in a second pull request.

## 5. Ship it

For the shared manifest, open a PR on fwl-io: it gets a review request to the team `proteus-maintainer` and needs the approval of a code owner. The code of the model that reads the files is changed by a developer of that model ([Migrate a model](migrate_model.md) for a Python model, [Use fwl-io data from a non-Python code](non_python_codes.md) for another one); when you are not that developer, say so in the notes of the dataset request, or in a comment of the issue. For a model manifest, open a PR on the repository of the model, from a fork when you have no write access; make sure the manifest and its registry files are included in the model's package data, or fetching fails at runtime on user machines.
