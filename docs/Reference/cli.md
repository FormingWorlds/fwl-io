# CLI reference

The `fwl-io` command has five subcommands. Failures are reported as concise messages on stderr (never a traceback) and exit with status 1; success exits 0. `sync` and `fetch` aggregate per-dataset failures into a multi-line report, and a download failure lists every mirror attempt.

## fwl-io sync

```bash
fwl-io sync path/to/manifest.toml
```

Queries the Zenodo record of every dataset in the manifest and rewrites the registry file next to the manifest. Rejects concept DOIs. All datasets are attempted; per-dataset failures are collected and reported together at the end, and successfully synced registries are still written.

## fwl-io list

```bash
fwl-io list
```

Lists every dataset from all installed manifests with its key and consumers. Datasets without a committed registry are flagged `[NO REGISTRY]`. Providers whose manifest fails to load are reported on stderr and the exit status is 1.

## fwl-io fetch

```bash
fwl-io fetch <model> [--data-root PATH]
```

Fetches every dataset that lists `<model>` in its `required_by`. All datasets are attempted; failures are aggregated into one report. `--data-root` overrides the `FWL_DATA` tree.

## fwl-io check

```bash
fwl-io check <model> [--data-root PATH]
```

Reports whether every dataset that lists `<model>` in its `required_by` is present and matches its registry, without downloading anything. Each file is reported in one of five states: `ok` (present, checksum matches), `missing`, `mismatch` (present, contents differ), `unreadable` (present, could not be read to be checked), or `present`. The last means the file is there and nothing was available to verify it against, which is the case for the members of an archive dataset: the registry pins the checksum of the archive, not of the files extracted from it, so such a dataset is reported `presence only`. That is not counted as a fault, since presence is all that is checkable there, but it is never reported as verification.

Two kinds of failure are reported apart from the datasets, because they call for different repairs. `MANIFEST UNREADABLE` means an installed package's manifest could not be read at all, so nothing it declares was inspected. `NOT CHECKED` means the manifest was fine but one dataset could not be resolved, most often because its registry has not been generated yet; run `fwl-io sync` for it. Either is on its own enough to fail the check.

The report goes to stdout whatever the verdict, so a caller running this to find out what is wrong gets the detail and not only the exit status. Exit is 1 on any missing, corrupt or unreadable file, any unreadable manifest, any unresolvable dataset, or a model no manifest declares. The closing line says `all data present and verified` only when every file was compared against a digest; a sound tree holding a presence-only dataset closes with `all data present, N dataset(s) by presence only` instead, and still exits 0.

Checking reads and hashes every file the manifest declares, so the cost is one full pass over the model's data. On a multi-gigabyte tree, or a shared cluster filesystem, expect it to take as long as reading that data once.

Nothing is downloaded and no dataset directory or file is written, which makes this safe to run against a tree another process is reading. Resolving the data root creates that root if it does not exist, as it does for every other subcommand. The equivalent Python entry point is `fwl_io.check_for`, whose `CheckReport.ok` is false when nothing was checked, so a model that matches no dataset can never read as a clean tree. `CheckReport.verified` is the stricter question, false whenever any part of the tree was checked by presence alone.

## fwl-io mirror

```bash
DATAVERSE_TOKEN=... fwl-io mirror <zenodo-doi> --collection <alias> \
    [--dataverse-url URL] [--contact-email EMAIL] [--subject SUBJECT] \
    [--no-publish] [--dry-run]
```

Mirrors a pinned Zenodo deposit to a Dataverse collection: it downloads and checksum-verifies the deposit's files, creates a matching Dataverse dataset with citation metadata taken from the Zenodo record, uploads the files byte-identically (tabular ingest disabled), and by default publishes the dataset, then prints the Dataverse DOI to add to the consuming manifest. The API token is read from the `DATAVERSE_TOKEN` environment variable, never a command-line argument. A contact email (`--contact-email`) is required to create a dataset; only `--dry-run`, which makes no Dataverse writes, is exempt. `--subject` is validated by the server when the dataset is created, so a value outside the target installation's citation vocabulary is rejected then. `--dry-run` performs the download and metadata mapping only, making no Dataverse changes; `--no-publish` leaves the created dataset as a private draft. See [Mirror a deposit to Dataverse](../How-to/mirror_dataset.md).

## fwl-io --version

Prints the installed version.
