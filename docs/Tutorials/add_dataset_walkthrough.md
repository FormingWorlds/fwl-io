# Add a dataset, step by step

This tutorial takes one small dataset from a Zenodo record to a pinned, fetched, checked and mirrored dataset. You run every command yourself, in a scratch directory, and nothing you do here changes a repository or a server. It takes about 10 minutes.

The dataset is hypothetical: a model named `demo` needs two melting-curve files. The files are real. They are in the public Zenodo record `10.5281/zenodo.15728072` (2 files of 50 kB), which stands in for the record that you would upload yourself. The shared manifest of fwl-io declares the same record as `interior.melting_curves.wolf_bower_2018`, so you see it a second time in the lists below; the two datasets do not disturb each other.

You need Python 3.11 or newer, an internet connection, and a Python environment with fwl-io installed ([Installation](../How-to/installation.md) says how). Activate that environment in the shell that you use, and check it: `fwl-io --help` must print a text that starts with `usage: fwl-io`. Create and edit the files below with a text editor.

The steps that you cannot run here are marked **Not run here**: the upload to Zenodo, the two mirror workflows and the pull request.

## Terms

| Term | Meaning |
| --- | --- |
| Manifest | A TOML file in a Python package that declares datasets, one table per dataset. |
| Dataset key | The name of that table, for example `demo.melting_curves`. It is also the place of the files below `FWL_DATA`, with a directory for each part: `demo/melting_curves`. |
| Registry | The file `<dataset key>.registry.txt` beside the manifest, with the name and the checksum of each file. `fwl-io sync` writes it. |
| Pin | A DOI written in the manifest. The `zenodo` line pins one version of a Zenodo record; the `dataverse` line pins one mirror. |
| Entry point | The line in the `pyproject.toml` of a package, in the group `fwl_io.manifests`, through which fwl-io finds the manifest of that package once the package is installed. |
| DataverseNL | The data repository at dataverse.nl that holds the mirrors, the second download source. |
| Collection | The place on DataverseNL that holds the mirror datasets. For the Proteus Framework its name is `Proteus_Fr`. |
| Draft | A dataset on DataverseNL that is not published: only people with access to the collection see it. |

## 1. Make a scratch directory

Start in a directory that is not inside a git clone, your home directory for example. Print the value that your shell has for `FWL_DATA`, the directory that fwl-io downloads into:

```bash
echo "$FWL_DATA"
```

Note the value when the line is not empty: step 11 removes the variable. Make the scratch directory and go into it:

```bash
mkdir fwl-tutorial
cd fwl-tutorial
```

Point `FWL_DATA` at a new directory below it:

```bash
export FWL_DATA="$PWD/data"
```

Every command below must run in this shell, in `fwl-tutorial`.

## 2. Deposit the files on Zenodo

**Not run here.** A dataset of the framework must be an accepted record of the PROTEUS Framework community on Zenodo (or of the PALEOS community). For your own dataset, either open a dataset request, and a maintainer uploads the files for you, or upload them yourself and submit the upload to the community; [Add a dataset](../How-to/add_dataset.md#1-bring-the-files-into-the-proteus-framework-community-on-zenodo) gives the routes. For an own upload, add the files and fill in the title, the authors, the description and the license. The mirror of step 8 copies these four from the record, so write them for a reader who finds the dataset without context. When the record is published and in the community, note its **version DOI**, of the form `10.5281/zenodo.<record-id>`.

For this tutorial the record exists, and it is in the community: `10.5281/zenodo.15728072`.

## 3. Create the package that declares the dataset

A dataset is declared in a manifest, a TOML file that a Python package ships. A dataset that several models read goes into the shared manifest of fwl-io itself; a dataset that one model reads goes into the manifest of that model. Here the model is a package of three files, in this layout:

```text
fwl-tutorial/
  demo_model/              the project: pyproject.toml
    demo_model/            the Python package: __init__.py, manifest.toml
```

Create the two directories:

```bash
mkdir -p demo_model/demo_model
```

Create `demo_model/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "demo-model"
version = "0.1"
dependencies = ["fwl-io"]

[project.entry-points."fwl_io.manifests"]
demo = "demo_model:manifest_path"

[tool.setuptools]
packages = ["demo_model"]

[tool.setuptools.package-data]
demo_model = ["*.toml", "*.registry.txt"]
```

The `fwl_io.manifests` entry point is how fwl-io finds the manifest of an installed package.

Create `demo_model/demo_model/__init__.py`:

```python
from pathlib import Path


def manifest_path() -> Path:
    return Path(__file__).parent / 'manifest.toml'
```

Create `demo_model/demo_model/manifest.toml`:

```toml
manifest_schema = 1

[demo.melting_curves]
name = "Demo melting curves"
zenodo = "10.5281/zenodo.15728072"
required_by = ["demo"]
```

The table name `demo.melting_curves` is the dataset key and also the place of the files below `FWL_DATA`. `required_by` names the models whose `fwl-io fetch <model>` includes the dataset.

Install the package:

```bash
pip install -e demo_model
```

The output holds the line `Successfully installed demo-model-0.1`.

## 4. Generate the registry

```bash
fwl-io sync demo_model/demo_model/manifest.toml
```

```text
wrote demo_model/demo_model/demo.melting_curves.registry.txt
```

The command read the Zenodo record and wrote the registry, the list of files and checksums that every later fetch trusts:

```bash
cat demo_model/demo_model/demo.melting_curves.registry.txt
```

```text
liquidus.dat md5:65efbd9f5b2de54109806c3b7f743708
solidus.dat md5:66b297a120c79e7e58de5761ac40f4c4
```

When Zenodo does not answer, the command prints `1 dataset(s) failed to sync` with the reason and exits 1. Run it again.

A manifest must pin a version DOI. Zenodo also gives every record a concept DOI, which always points to the newest version; for this record it is `10.5281/zenodo.15728071`. You do not need to try it: with that DOI in the `zenodo` line, the same command refuses and exits 1:

```text
fwl-io: 1 dataset(s) failed to sync (0 registries written):
  demo.melting_curves: 10.5281/zenodo.15728071 is a concept DOI (the API resolves it to the newest deposit, record 15728072); pin the version DOI of a specific deposit instead
```

## 5. See the dataset in the list

```bash
fwl-io list
```

The output lists the datasets of every installed manifest, one block per manifest, sorted by the name of its entry point. The block of the demo package is:

```text
[demo]
  demo.melting_curves                                required_by: demo
    Demo melting curves
```

The block `[fwl-io-shared]` holds the datasets of the shared manifest, two lines each. `required_by: -` marks a dataset that no model fetches by its name.

When it fails:

- No `[demo]` block: the package is not installed in this environment. Run `pip install -e demo_model` again, in `fwl-tutorial`.
- A line `[demo] FAILED TO LOAD: ...` in place of the block: the text after the colon names the cause. `module 'demo_model' has no attribute ...` is a wrong function name in the entry point of `pyproject.toml` or in `__init__.py`; correct it and install the package again. `table 'demo.melting_curves' has no "zenodo" key ...` is a misspelt line in `manifest.toml`; correct it.

## 6. Fetch and check the files

```bash
fwl-io fetch demo
```

```text
Downloading data from 'doi:10.5281/zenodo.15728072/liquidus.dat' to file '<FWL_DATA>/.fwl-io-staging/<random name>/liquidus.dat'.
Downloading data from 'doi:10.5281/zenodo.15728072/solidus.dat' to file '<FWL_DATA>/.fwl-io-staging/<random name>/solidus.dat'.
demo.melting_curves: 2 file(s)
```

In your output, `<FWL_DATA>` is the full path of your `data` directory and `<random name>` is a temporary directory, another one for each file. Each file is downloaded there, compared with its checksum in the registry, and moved into place.

When Zenodo is slow, lines that start with `mirror failed for` and `retrying` come between these lines: the fetch tries each file up to 4 times, and it passes when its last line is `demo.melting_curves: 2 file(s)`. When the fetch prints `no data root configured: set the FWL_DATA environment variable or pass an explicit data_root path`, your shell has no `FWL_DATA`: run the `export` line of step 1 in `fwl-tutorial`, then fetch again.

The files are in a directory named for the Zenodo record:

```bash
ls "$FWL_DATA/demo/melting_curves/r15728072"
```

```text
liquidus.dat
solidus.dat
```

Run the fetch again:

```bash
fwl-io fetch demo
```

It downloads nothing, because the files are in place:

```text
demo.melting_curves: 2 file(s)
```

Check the files on disk against the registry, without a download:

```bash
fwl-io check demo
```

```text
demo.melting_curves: ok, 2 file(s)
all data present and verified
```

The dataset works with Zenodo as its one source. Steps 7 to 9 give it a second source.

## 7. Try the mirror without a write

A mirror is a copy of the record on DataverseNL, which a fetch uses when Zenodo does not answer. `Proteus_Fr` is the collection of the Proteus Framework there. The command that creates one has a dry run, which downloads the files and builds the metadata and stops before it writes to DataverseNL. It needs no token:

```bash
fwl-io mirror 10.5281/zenodo.15728072 --collection Proteus_Fr --dry-run
```

In your output, `<temporary directory>` stands for a long path of several directories below the temporary directory of your system, another one for each file:

```text
Downloading data from 'doi:10.5281/zenodo.15728072/liquidus.dat' to file '<temporary directory>/liquidus.dat'.
Downloading data from 'doi:10.5281/zenodo.15728072/solidus.dat' to file '<temporary directory>/solidus.dat'.
dry run complete for 10.5281/zenodo.15728072 (no Dataverse changes)
```

A dry run that ends with this line shows that the record can be mirrored.

## 8. Ask for the mirror

**Not run here.** The real mirror is created by a maintainer of fwl-io, because the DataverseNL token is in the fwl-io repository and nowhere else. For your own dataset:

1. Use the dataset request that brought the record into the community, or open a [dataset request](https://github.com/FormingWorlds/fwl-io/issues/new?template=dataset_request.yml) with the route "My record is in the community; I ask for a mirror", the version DOI and the dataset key.
2. A maintainer runs the workflow **Mirror a Zenodo deposit to Dataverse** with the version DOI, and a maintainer approves the run. It creates a private draft and prints its DOI.
3. The maintainer checks the draft and runs the workflow **Publish an existing Dataverse draft**.
4. The maintainer gives you the DOI of the mirror, of the form `10.34894/<id>`.

[Update a dataset or its mirror](../How-to/update_dataset.md#a-mirror-for-my-dataset) lists the inputs of both workflows.

For this tutorial the mirror exists: `10.34894/6VJ51M` is the mirror of record 15728072.

## 9. Pin the mirror

Edit `demo_model/demo_model/manifest.toml`: add the `dataverse` line below the `zenodo` line, so that the file reads:

```toml
manifest_schema = 1

[demo.melting_curves]
name = "Demo melting curves"
zenodo = "10.5281/zenodo.15728072"
dataverse = "10.34894/6VJ51M"
required_by = ["demo"]
```

Check that the mirror serves the dataset:

```bash
fwl-io check-mirrors
```

The command reads every pin of every installed manifest from DataverseNL, and the file sizes from Zenodo. It takes 1 to 3 minutes, and about 10 minutes when Zenodo is slow. On a day with no failed read it prints one line of counts:

```text
pins served by their mirror: <N>, wrong: 0, not checked (could not be read): 0, datasets without a pin: 0, manifests left out: 0
```

`<N>` is the number of pinned datasets of the shared manifest plus 1, for the demo dataset.

Look for the demo dataset, not at the counts. The pin of the demo dataset is served when all of these hold: no line that starts with `FAIL` or `UNREACHABLE` names `demo.melting_curves`, no line starts with `FAIL demo: MANIFEST`, and no line `unpinned demo.melting_curves` stands below the counts (such a line means that the `dataverse` line is missing). Lines for other datasets are not caused by this tutorial: an `UNREACHABLE` line means that a server did not answer for that dataset, and on a day when Zenodo is slow the shared manifest can have some; the command then exits 3, which says nothing about the demo dataset. When `demo.melting_curves` itself is `UNREACHABLE`, run the command again later. The [CLI reference](../Reference/cli.md#fwl-io-check-mirrors) lists every line and exit code of the command.

## 10. Open the pull request

**Not run here.** In a real model, the manifest and the registry are files of the model's repository. Commit them and open a pull request on that repository. For a dataset in the shared manifest, the pull request goes to fwl-io.

Checklist for the pull request:

- The manifest has the dataset table, with a **version** DOI in `zenodo`.
- The registry file `<dataset key>.registry.txt` is committed beside the manifest, as `fwl-io sync` wrote it.
- The package data of the package includes the manifest and the registry files (the `package-data` lines of step 3).
- `fwl-io fetch <model>` and `fwl-io check <model>` pass on your machine.
- The `dataverse` line is there when the mirror is published, and `fwl-io check-mirrors` prints no `FAIL`, `UNREACHABLE` or `unpinned` line for the dataset. A dataset without the line works, with Zenodo as its one source; the line can follow in a second pull request.
- The model code reads the files from the directory that fwl-io returns, not from a fixed path.

After the merge, the dataset reaches users with the next release of the package.

## 11. Clean up

```bash
pip uninstall -y demo-model
cd ..
rm -r fwl-tutorial
unset FWL_DATA
```

Set `FWL_DATA` back to the value that you noted in step 1, if there was one.

## Where to go next

- [Add a dataset](../How-to/add_dataset.md): the same steps as a short reference.
- [Update a dataset or its mirror](../How-to/update_dataset.md): what to do when the record gets a new version, and who does what.
- [Migrate a model to fwl-io](../How-to/migrate_model.md): the entry point and the package data in a real model.
