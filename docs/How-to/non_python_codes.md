# Use fwl-io data from a non-Python code

A code that cannot import fwl-io, such as AGNI (Julia) or SOCRATES (Fortran), gets its data through the `fwl-io` command. The command needs a Python environment with fwl-io installed and `FWL_DATA` set; the code itself only reads files. Run every command below with the same `FWL_DATA` (or the same `--data-root`).

When PROTEUS drives the code, PROTEUS fetches the data and passes the paths, so nothing on this page is needed.

## 1. Find the dataset key

Every dataset has a dotted key, which is also its place below `FWL_DATA`. List the keys of every installed manifest:

```bash
fwl-io list
```

AGNI reads, for example, a SOCRATES spectral file and a stellar spectrum: `atmos_clim.spectral_files.dayspring.48` and `star.spectra.solar`.

## 2. Fetch the dataset

```bash
fwl-io fetch --key atmos_clim.spectral_files.dayspring.48
fwl-io fetch --key star.spectra.solar
```

Each file is checked against the registry. When Zenodo does not answer, the file comes from the DataverseNL mirror of the dataset, for a dataset whose manifest entry has a `dataverse` pin (both examples here have one). A file already in place is not downloaded again, so the command is safe to repeat, for example at the start of every job. On a compute node without internet, run it on a login node first; see [Run on clusters](clusters.md).

## 3. Get the path

```bash
fwl-io path atmos_clim.spectral_files.dayspring.48
```

prints the version directory of the dataset, `$FWL_DATA/atmos_clim/spectral_files/dayspring/48/r15721749`. The last segment names the Zenodo record, so a newer version of the dataset goes into a new directory next to it. The command exits 1 when no completed fetch left the dataset in place; a script that checks the exit status, as in the next step, stops there instead of passing a path to missing files. The files are not hashed again. `fwl-io check <model>` covers only datasets that name the model in `required_by`, which these two do not; to check them again, run `fwl-io fetch --key` once more: it hashes every file in place and fetches any that differs from the registry.

## 4. Pass the path to the code

Give the code the absolute file paths. For AGNI, put them in the `[files]` table of its configuration:

```bash
SF_DIR="$(fwl-io path atmos_clim.spectral_files.dayspring.48)" || exit 1
STAR_DIR="$(fwl-io path star.spectra.solar)" || exit 1
SF="$SF_DIR/Dayspring.sf"
STAR="$STAR_DIR/sun.txt"
```

```toml
[files]
    input_sf   = "<value of $SF>"
    input_star = "<value of $STAR>"
```

A model that declares its datasets with `required_by` can fetch all of them at once with `fwl-io fetch <model>`; see the [CLI reference](../Reference/cli.md).
