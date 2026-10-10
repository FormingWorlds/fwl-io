# Update a dataset or its mirror

This page is for the person who owns a dataset: you uploaded the files to Zenodo, and a model reads them through fwl-io.

A dataset is pinned to one version of a Zenodo record and to a committed list of its files and checksums. Nothing follows Zenodo by itself: a new version on Zenodo changes neither the pin nor the DataverseNL mirror, and a fetch keeps downloading the pinned version. A new version reaches users only through a pull request that changes the pin and the registry. [Data governance](../Explanations/governance.md) gives the reason for this rule.

## Terms

| Term | Meaning |
| --- | --- |
| Manifest | A TOML file in a Python package that declares datasets, one table per dataset. |
| Dataset key | The name of that table, for example `demo.melting_curves`. It is also the place of the files below `FWL_DATA`. |
| Registry | The file `<dataset key>.registry.txt` beside the manifest, with the name and the checksum of each file. `fwl-io sync` writes it. |
| Pin | A DOI written in the manifest. The `zenodo` line pins one version of a Zenodo record; the `dataverse` line pins one mirror. |
| Entry point | The line in the `pyproject.toml` of a package, in the group `fwl_io.manifests`, through which fwl-io finds the manifest of that package once the package is installed. |
| DataverseNL | The data repository at dataverse.nl that holds the mirrors, the second download source. |
| Collection | The place on DataverseNL that holds the mirror datasets. For the Proteus Framework its name is `Proteus_Fr`. |
| Draft | A dataset on DataverseNL that is not published: only people with access to the collection see it. |

## Who does what

| Who | Does | Needs |
| --- | --- | --- |
| User | Runs `fwl-io fetch` and gets the versions that the installed packages pin. Gets a new version by upgrading the package whose manifest declares the dataset. | Nothing |
| Dataset owner | Publishes the version on Zenodo. Changes the pin and the registry in a pull request on the repository that holds the manifest. Asks for a mirror. | The right to open a pull request |
| Maintainer of the repository that holds the manifest | Reviews and merges the pull request, and makes the release that carries it. | Write access to that repository |
| fwl-io maintainer | Runs the two mirror workflows, which hold the DataverseNL token. For the shared manifest, also the row above. | Write access to the fwl-io repository |
| Weekly job | Compares the committed registries of the shared manifest with Zenodo, and checks every `dataverse` pin of the shared manifest. Reports a difference; changes nothing. | Nothing |

The shared manifest is `src/fwl_io/data/shared_manifest.toml` in the fwl-io repository; its package is fwl-io. A dataset that one model reads is declared in that model's own manifest, and its pull request goes to that model's repository, where the maintainers of the model review it.

## A record has a new version

The example is the real Zenodo record "atmos_clim/thermo". Its version `10.5281/zenodo.21390786` holds 6 files. Its next version, `10.5281/zenodo.23278603`, holds a changed `gases.zip`, a new `plt.zip` and the same 5 other files. The dataset `doi:10.34894/EHV2DU` on DataverseNL is a mirror of the first of the two. The manifest entry is hypothetical: no fwl-io manifest declares this record (see [Data that a code also downloads with its own script](#data-that-a-code-also-downloads-with-its-own-script)). Assume that a manifest holds this table:

```toml
[atmos_clim.thermo]
zenodo = "10.5281/zenodo.21390786"
dataverse = "10.34894/EHV2DU"
```

After the steps it holds:

```toml
[atmos_clim.thermo]
zenodo = "10.5281/zenodo.23278603"
```

1. Publish the new version of the record on Zenodo (the Zenodo help describes how; this page starts where the version is public). Open the page of that version. Its address ends in the record id, `https://zenodo.org/records/<record-id>`, and the page shows the same number in its DOI, `10.5281/zenodo.<record-id>`: that is the **version DOI**. The concept DOI of the record has another number, and its address leads to the page of the newest version; a manifest cannot pin it.
2. Clone the repository that holds the manifest, and install its package in editable mode, in a Python environment with fwl-io ([Installation](installation.md)). For the shared manifest the repository is fwl-io itself:

    ```bash
    pip install -e .
    ```

    Print the manifest file of every installed package:

    ```bash
    python -c "from importlib.metadata import entry_points; [print(e.name, e.load()()) for e in entry_points(group='fwl_io.manifests')]"
    ```

    Each line holds the name of an entry point and the path of its manifest; `fwl-io-shared` is the shared manifest. `fwl-io list` prints the datasets of each manifest under the same names, so it shows which manifest declares your dataset key.
3. Open that manifest and replace the DOI in the `zenodo` line of your dataset with the new version DOI. When the table has a `files` line, it lists the files of the record that the dataset uses: add the name of a new file that the model needs, and remove the name of a file that the new version does not hold.
4. Remove the `dataverse` line of that dataset ([What happens to the mirror](#what-happens-to-the-mirror) gives the reason).
5. Regenerate the registry. Run the command in the root of the clone, with the path of the manifest from step 2:

    ```bash
    fwl-io sync path/to/manifest.toml
    ```

    The command prints one line per registry file, `wrote path/to/<dataset key>.registry.txt`. It reads the Zenodo record of every dataset in the manifest, so for the shared manifest it takes some minutes.

    When it fails, it prints no `wrote` line and exits 1:

    - `N dataset(s) failed to sync`, with one line per dataset and its reason. The registries of the other datasets are written (`git status` shows them). When your dataset is in the list because Zenodo did not answer, run the command again. For a 404, check the record id. For a concept DOI, use the version DOI of step 1. For a name in `files` that is not in the record, correct the `files` line. A dataset of another owner in the list does not block you: its registry stays as it was.
    - One line that names your dataset and says that its `zenodo` value is not a Zenodo DOI of the form `10.5281/zenodo.<record-id>`: correct the line. No registry is written.
6. Read the change with `git diff`. The registry of your dataset shows the files that changed and their new checksums. No other registry must change: when one does, its Zenodo record differs from what is committed, which is not part of your change. Restore that file with `git checkout -- <file>` and open an issue on the repository that holds the manifest.
7. Fetch the dataset. `FWL_DATA` must name the directory that fwl-io downloads into ([Getting started](../getting_started.md)):

    ```bash
    fwl-io fetch --key <dataset key>
    fwl-io path <dataset key>
    ```

    The fetch prints `<dataset key>: N file(s)`. The path ends in `r<record-id>` with the id of the new version: each version has its own directory, and the directory of the previous version stays on disk until `fwl-io prune --delete` removes it ([CLI reference](../Reference/cli.md#fwl-io-prune)).

    When Zenodo is slow, the fetch prints lines that start with `mirror failed for` and `retrying`; it tries each file up to 4 times. When it ends with `could not obtain '<file>' from any mirror`, run it again later.
8. Check the files on disk against the registry, when the table of your dataset has a `required_by` line. Use one of the models that the line names:

    ```bash
    fwl-io check <model>
    ```

    Read the line of your dataset. It must say `<dataset key>: ok, N file(s)`, or `<dataset key>: ok, N file(s), presence only` for a dataset with an `extract` line, whose unpacked files are tested for presence and not hashed.

    The command covers every dataset of the model. The other datasets are `FAILED` and the last line is `data check FAILED` unless you fetched them too, with `fwl-io fetch <model>`, which can download many gigabytes. A dataset without a `required_by` line has no check command; the fetch of step 7 has compared each file with the registry.
9. Commit the manifest and the registry file together and open a pull request. It must contain:
    - the new `zenodo` DOI,
    - the regenerated registry file of that dataset, and of no other dataset,
    - the change in the model code, when a file has a new name or a new layout.
10. A maintainer of that repository reviews and merges the pull request. Users get the version with the next release of that package, which its maintainers make.

### What happens to the mirror

A mirror dataset on DataverseNL is a copy of one Zenodo version. It stays a correct mirror of that version, and it does not become a mirror of the next one. The new version needs a new mirror dataset and a new `dataverse` pin ([next section](#a-mirror-for-my-dataset)). With the old `dataverse` line removed (step 4) and until the new pin is merged, the dataset has no fallback: a fetch downloads from Zenodo, and fails when Zenodo does not answer.

Do not keep the old `dataverse` pin beside the new `zenodo` DOI. `fwl-io check-mirrors` reports such a pin as wrong and exits 1, which fails the weekly job when the dataset is in the shared manifest:

```text
FAIL <dataset key>: doi:10.34894/EHV2DU does not name Zenodo 10.5281/zenodo.23278603 as its source
```

A fetch with such a pin still downloads from Zenodo first. When Zenodo does not answer, it asks the old mirror, and a file is placed only when its checksum is the one in the registry of the new version. With the pins of the example and Zenodo not reachable, the fetch takes the files in the order of their names:

- A file that is the same in both versions (`DirEOS2019.tar.gz`) is downloaded from the old mirror and placed.
- The file that changed (`gases.zip`) is downloaded from the old mirror in each of the 4 attempts that a fetch makes (it waits 10, 30 and 60 s between them), and rejected and deleted each time, because its MD5 is not the one in the registry. The fetch stops there with `could not obtain 'gases.zip' from any mirror` and exits 1.
- The file that is new (`plt.zip`) comes later in the order, so that run does not ask for it. The old mirror does not hold it: a fetch that reaches it ends the same way.

## A mirror for my dataset

A mirror is a second download source, used when Zenodo does not answer. The mirror workflow needs only the version DOI, so you can ask for a mirror as soon as the version is public on Zenodo. The usual order is two pull requests: the first with the `zenodo` pin and no `dataverse` line, the second with the `dataverse` pin when the mirror is published. One pull request with both pins is as good when the mirror is published before the review.

### The dataset owner asks

1. Open an issue on the [fwl-io repository](https://github.com/FormingWorlds/fwl-io/issues) with the version DOI, the dataset key, the repository that holds the manifest and, when the dataset sets `files`, the file names.

### A maintainer creates the mirror

These steps need write access to the fwl-io repository; the workflows are on its **Actions** tab. [Mirror a deposit to Dataverse](mirror_dataset.md) has the details of each input and says what to do when a run fails.

1. Run the workflow **Mirror a Zenodo deposit to Dataverse**. The table gives the name of each input, the description that the workflow file gives it, and the value:

    | Input | Description in the workflow | Value |
    | --- | --- | --- |
    | **zenodo_doi** | Zenodo version DOI to mirror (10.5281/zenodo.&lt;record-id&gt;) | the version DOI |
    | **collection** | Dataverse collection alias | `Proteus_Fr` (the default of the form) |
    | **files** | Only these files of the deposit, separated by spaces (empty: the whole deposit) | empty for the whole record; the names from the manifest when the dataset sets `files` (a name must not contain a space) |
    | **licence** | Dataverse license name for a new draft in place of the Zenodo one, only where the author licenses it so; never published (empty: from Zenodo) | empty ([What the mirror does](mirror_dataset.md#what-the-mirror-does) gives the one case for a value) |
    | **into** | Persistent id of an existing draft of this record to complete, never published (empty: create one) | empty |
    | **dry_run** | Download and map metadata only; make no Dataverse changes | unchecked |
    | **publish** | Publish the created dataset (unchecked: leave it a draft to review) | unchecked |

    The workflow runs in the `dataverse` environment of the repository and takes the DataverseNL token and the contact email that every mirror dataset carries from its secrets; the form has no input for them. The run downloads each file from Zenodo, checks it, uploads it to a new draft dataset and checks it there. Its log ends with `draft doi:10.34894/<id> created, not published; verify its files, then run fwl-io mirror-publish doi:10.34894/<id>`; the workflow of step 3 runs that command. The draft is private.
2. Open the draft on DataverseNL (`https://dataverse.nl/dataset.xhtml?persistentId=doi:10.34894/<id>`, signed in with access to the collection). Compare the title, the authors, the description, the license and the names of the files with the Zenodo record. The maintainer decides: when all of them agree, publish; when one differs, do not publish, and say in the issue what differs. A published dataset is public and has a permanent DOI.
3. Run the workflow **Publish an existing Dataverse draft** with **persistent_id** `doi:10.34894/<id>` and **version_type** `major` (the default of the form).
4. Write the DOI of the mirror, `10.34894/<id>`, into the issue.

### The dataset owner pins the mirror

1. Add the pin to the manifest entry, below its `zenodo` line:

    ```toml
    dataverse = "10.34894/<id>"
    ```

2. Check the pin, with the package that holds the manifest installed in editable mode:

    ```bash
    fwl-io check-mirrors
    ```

    The command checks the pins of every installed manifest: it reads each pin from its Dataverse server and the file sizes from Zenodo. It prints one `FAIL` line per wrong pin and one `UNREACHABLE` line per pin that could not be read, then one line of counts, then the datasets without a pin; the [CLI reference](../Reference/cli.md#fwl-io-check-mirrors) lists the other lines (a manifest left out, a warning). Your pin is served when no `FAIL` and no `UNREACHABLE` line names your dataset key. When your pin is `UNREACHABLE`, a server did not answer: run the command again later. It exits 0 when every pin is served, and 1 when a pin is wrong, a manifest was left out or no pin was checked. A line that names another dataset is not caused by your change.
3. Open a pull request with the manifest change on the repository that holds the manifest. The registry does not change.

## Data that a code also downloads with its own script

A code can hold Zenodo record ids outside any manifest. AGNI's `src/get_data.sh` downloads its data with record ids written in the script; `gases.zip` of the example comes from record 21390786 there, and no fwl-io manifest declares that record. A new version of such a record needs the id changed in that script too, in a pull request on that code. fwl-io does not read these ids: `fwl-io check`, `fwl-io check-mirrors` and the weekly job do not see them.

[Use fwl-io data from a non-Python code](non_python_codes.md) shows how such a code reads a dataset through the `fwl-io` command.

## What the weekly job checks

The workflow named **Nightly** in the fwl-io repository runs once a week, every Monday. It runs two checks, and it changes nothing.

- **Run slow tier (live Zenodo checks)** reads the Zenodo record of every dataset in the shared manifest and compares its file names and checksums with the committed registry. A difference means that the record changed after the registry was written, or that the registry was edited by hand. Run `fwl-io sync` on the manifest, read the diff, and open a pull request, or restore the registry.
- **Check the DataverseNL mirror pins** runs `fwl-io check-mirrors`. A `FAIL` line names a pin that does not serve its dataset: remove or replace the pin in a pull request. An `UNREACHABLE` line names a pin that could not be read: when the other pins are served, the job passes with a warning, and the next run reads the pin again.

The job does not look for new versions of a record. A new version stays unused until its owner follows [A record has a new version](#a-record-has-a-new-version). The job reads the manifests installed in its run, which is the shared manifest alone. It does not read the manifest of a model; the repository of that model is the place for such a check.
