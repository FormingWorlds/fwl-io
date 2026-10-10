# Update a dataset or its mirror

This page is for the person who owns a dataset: you uploaded the files to Zenodo, and a model reads them through fwl-io.

A dataset is pinned to one Zenodo version DOI in a manifest and to the registry file committed beside it, which lists the files and their checksums. Nothing follows Zenodo by itself: a new version on Zenodo changes neither the pin nor the DataverseNL mirror, and a fetch keeps downloading the pinned version. A new version reaches users only through a pull request that changes the pin and the registry. [Data governance](../Explanations/governance.md) gives the reason for this rule.

## Who does what

| Who | Does | Needs |
| --- | --- | --- |
| User | Runs `fwl-io fetch` and gets the versions that the installed packages pin. Gets a new version by upgrading the package whose manifest declares the dataset. | Nothing |
| Dataset owner | Publishes the version on Zenodo. Changes the pin and the registry in a pull request on the repository that holds the manifest. Asks for a mirror. | The right to open a pull request |
| fwl-io maintainer | Reviews and merges the pull request. Runs the two mirror workflows, which hold the DataverseNL token. | Write access to the fwl-io repository |
| Weekly job | Compares the committed registries of the shared manifest with Zenodo, and checks every `dataverse` pin. Reports a difference; changes nothing. | Nothing |

The shared manifest is `src/fwl_io/data/shared_manifest.toml` in the fwl-io repository. A dataset that one model reads is declared in that model's own manifest, and its pull request goes to that model's repository. The mirror workflows are in the fwl-io repository for both.

## A record has a new version

The example is the Zenodo record "atmos_clim/thermo". Its version `10.5281/zenodo.21390786` holds 6 files. Its next version, `10.5281/zenodo.23278603`, holds a changed `gases.zip`, a new `plt.zip` and the same 5 other files. The mirror `doi:10.34894/EHV2DU` on DataverseNL holds the 6 files of the first one.

1. Publish the new version of the record on Zenodo. Zenodo gives the version its own DOI. Note the **version DOI**, `10.5281/zenodo.<record-id>`, not the concept DOI of the record, which always points to its newest version.
2. Open the manifest that declares the dataset and replace the DOI in its `zenodo` line with the new version DOI.
3. Remove the `dataverse` line of that dataset. The mirror it names holds the previous version (see [What happens to the mirror](#what-happens-to-the-mirror)).
4. Regenerate the registry:

    ```bash
    fwl-io sync path/to/manifest.toml
    ```

    The command prints one line per registry file, `wrote path/to/<dataset key>.registry.txt`. It reads the Zenodo record of every dataset in the manifest, so for the shared manifest (36 datasets) it takes some minutes. When Zenodo does not answer for a record, the command lists that dataset under `dataset(s) failed to sync` and exits 1; the other registries are written. Run it again when your dataset is in that list.
5. Read the change with `git diff`. The registry of your dataset shows the files that changed and their new checksums; no other registry must change.
6. Install the package that holds the manifest in editable mode (`pip install -e .` in its repository), then fetch and check the dataset:

    ```bash
    fwl-io fetch --key <dataset key>
    fwl-io path <dataset key>
    ```

    The fetch prints `<dataset key>: N file(s)`. The path ends in `r<record-id>` with the id of the new version: each version has its own directory, and the directory of the previous version stays on disk. When the dataset has a `required_by` entry, `fwl-io check <model>` must end with `all data present and verified` (for an archive dataset, `all data present, N dataset(s) by presence only`).
7. Commit the manifest and the registry file together and open a pull request. It must contain:
    - the new `zenodo` DOI,
    - the regenerated registry file of that dataset, and of no other dataset,
    - no `dataverse` line for that dataset, or the pin of a mirror of the new version,
    - the change in the model code, when a file has a new name or a new layout.
8. A maintainer of the repository reviews and merges the pull request. Users get the version with the next release of that package.

### What happens to the mirror

A mirror dataset on DataverseNL is a copy of one Zenodo version. It stays a correct mirror of that version, and it does not become a mirror of the next one. The new version needs a new mirror dataset and a new `dataverse` pin ([next section](#a-mirror-for-my-dataset)). Until that pin is merged, the dataset has no fallback: a fetch downloads from Zenodo, and fails when Zenodo does not answer.

Do not keep the old `dataverse` pin beside the new `zenodo` DOI. `fwl-io check-mirrors` reports such a pin as wrong and exits 1, which fails the weekly job:

```text
FAIL <dataset key>: doi:10.34894/EHV2DU does not name Zenodo 10.5281/zenodo.23278603 as its source
```

A fetch with such a pin still downloads from Zenodo first. When Zenodo does not answer, it asks the old mirror, and each file is checked against the registry of the new version. With the pins of the example and Zenodo not reachable:

- a file that is the same in both versions (`DirEOS2019.tar.gz`) is downloaded from the old mirror and accepted;
- a file that changed (`gases.zip`) is downloaded from the old mirror in each of the 4 rounds of the fetch, rejected each time because its MD5 is not the one in the registry, and deleted;
- a file that is new (`plt.zip`) is not in the old mirror.

For the last two the fetch ends with `could not obtain '<file>' from any mirror` and exits 1. No file of the previous version is ever placed in the directory of the new one.

## A mirror for my dataset

A mirror is a second download source, used when Zenodo does not answer. Ask for one when the pull request with the Zenodo pin is merged, or together with it.

1. Open an issue on the [fwl-io repository](https://github.com/FormingWorlds/fwl-io/issues) with the version DOI, the dataset key and, when the dataset sets `files`, the file names.

The steps below are for a maintainer with write access to the fwl-io repository; the workflows are on its **Actions** tab. [Mirror a deposit to Dataverse](mirror_dataset.md) has the details of each one and what to do when a run fails.

2. Run the workflow **Mirror a Zenodo deposit to Dataverse**. The form shows a description for each input; the table lists the inputs in the order of the form:

    | Input | Value |
    | --- | --- |
    | **zenodo_doi** | the version DOI, `10.5281/zenodo.<record-id>` |
    | **collection** | `Proteus_Fr` (the default) |
    | **files** | empty for the whole record; the names from the manifest, separated by spaces, when the dataset sets `files` |
    | **licence** | empty |
    | **into** | empty |
    | **dry_run** | unchecked |
    | **publish** | unchecked |

    The run downloads each file from Zenodo, checks it, uploads it to a new draft dataset and checks it there. Its log ends with `add this to the manifest:  dataverse = "10.34894/<id>"`. The draft is private.
3. Open the draft on DataverseNL (`https://dataverse.nl/dataset.xhtml?persistentId=doi:10.34894/<id>`, signed in with access to the collection). Check the title, the license and the list of files against the Zenodo record. The decision to publish is the maintainer's: a published dataset is public and has a permanent DOI.
4. Run the workflow **Publish an existing Dataverse draft** with **persistent_id** `doi:10.34894/<id>` and **version_type** `major`.
5. Add the pin to the manifest entry, below its `zenodo` line:

    ```toml
    dataverse = "10.34894/<id>"
    ```

6. Check the pin, with the package that holds the manifest installed:

    ```bash
    fwl-io check-mirrors
    ```

    The command prints one line of counts, and above it one `FAIL` line per wrong pin. It exits 0 when every pin is served.
7. Open a pull request with the manifest change. The registry does not change.

## Data that a code also downloads with its own script

A code can hold Zenodo record ids outside any manifest. AGNI's `src/get_data.sh` downloads its data with record ids written in the script; `gases.zip` of the example comes from record 21390786 there, and no fwl-io manifest declares that record. A new version of such a record needs the id changed in that script too, in a pull request on that code. fwl-io does not read these ids: `fwl-io check`, `fwl-io check-mirrors` and the weekly job do not see them.

[Use fwl-io data from a non-Python code](non_python_codes.md) shows how such a code reads a dataset through the `fwl-io` command.

## What the weekly job checks

The **Nightly** workflow of the fwl-io repository runs every Monday. It has two steps, and it changes nothing.

- **Run slow tier (live Zenodo checks)** reads the Zenodo record of every dataset in the shared manifest and compares its file names and checksums with the committed registry. A difference means that the record changed after the registry was written, or that the registry was edited by hand. Run `fwl-io sync` on the manifest, read the diff, and open a pull request, or restore the registry.
- **Check the DataverseNL mirror pins** runs `fwl-io check-mirrors`. A `FAIL` line names a pin that does not serve its dataset: remove or replace the pin in a pull request. An `UNREACHABLE` line names a pin that could not be read; the job passes with a warning, and the next run reads it again.

The job does not look for new versions of a record. A new version stays unused until its owner follows [A record has a new version](#a-record-has-a-new-version). The job reads the manifests installed in its run, which is the shared manifest alone: a model that ships its own manifest runs its own checks.
