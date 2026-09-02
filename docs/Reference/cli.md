# CLI reference

The `fwl-io` command has seven subcommands. Failures are reported as concise messages on stderr (never a traceback) and exit with status 1; success exits 0. `sync` and `fetch` aggregate per-dataset failures into a multi-line report, and a download failure lists every mirror attempt.

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

Checking reads and hashes every file a plain dataset declares, so for those the cost is one full pass over the data: on a multi-gigabyte tree, or a shared cluster filesystem, expect it to take as long as reading that data once. An archive dataset costs far less, since its members have no digests to check and are only tested for presence.

Nothing is downloaded and no dataset directory or file is written, which makes this safe to run against a tree another process is reading. Resolving the data root creates that root if it does not exist, as it does for every other subcommand. The equivalent Python entry point is `fwl_io.check_for`, whose `CheckReport.ok` is false when nothing was checked, so a model that matches no dataset can never read as a clean tree. `CheckReport.verified` is the stricter question, false whenever any part of the tree was checked by presence alone.

## fwl-io relocate

```bash
fwl-io relocate [--data-root PATH] [--dry-run]
```

Moves data left by the previous layout into the place it belongs now, for a tree fetched before the current layout existed. Unmigrated code still reads the old directories, so they are otherwise left alone and age out as their consumers migrate; this is for cleaning a tree up straight away instead.

A dataset moves only when every file its registry declares is present in the old location and matches its recorded digest. Anything else is reported and left exactly where it is: an incomplete tree, a file whose contents differ, or a dataset whose registry has not been generated. Verifying first is the point, since moving a stale copy would put it where the fetcher then trusts it. Once a dataset's files have moved, the emptied directories are removed, and the walk upward stops at the data root.

Two kinds of dataset cannot be verified at all and are refused rather than moved, each named with its reason. An archive dataset's registry pins the packed archive, while an old tree holds the files extracted from it, so there is nothing to hash the tree against; move such a tree by hand, or delete it and let the fetcher rebuild it at the current location. A dataset whose registry is empty offers no files to compare, so every check over it would pass for want of anything to fail; run `fwl-io sync` for it. Both are reported only when an old directory is actually there, so a machine that never had the previous layout is unaffected.

A dataset already at its current location is not a fault, and a copy still sitting at the old location beside it is named rather than deleted. Nothing here removes data: the only directories it removes are ones it has just emptied itself.

Exit is 1 when a legacy tree was found and could not be moved, when an installed manifest could not be read, since that manifest may be the one declaring the dataset a tree still holds, or when the shipped table of old locations could not be read, since without it no dataset has an old location to look at and a run that reported nothing would read like a tidy tree. A tree that was already tidy exits 0. `--dry-run` reports the same plan without moving anything. The equivalent Python entry points are `fwl_io.relocate_all` and `fwl_io.plan_relocations`.

## fwl-io mirror

```bash
DATAVERSE_TOKEN=... fwl-io mirror <zenodo-doi> --collection <alias> \
    [--dataverse-url URL] [--contact-email EMAIL] [--subject SUBJECT] \
    [--no-publish] [--dry-run]
```

Mirrors a pinned Zenodo deposit to a Dataverse collection: it downloads and checksum-verifies the deposit's files, creates a matching Dataverse dataset with citation metadata taken from the Zenodo record, uploads the files byte-identically (tabular ingest disabled), and by default publishes the dataset, then prints the Dataverse DOI to add to the consuming manifest. The API token is read from the `DATAVERSE_TOKEN` environment variable, never a command-line argument. A contact email (`--contact-email`) is required to create a dataset; only `--dry-run`, which makes no Dataverse writes, is exempt. `--subject` is validated by the server when the dataset is created, so a value outside the target installation's citation vocabulary is rejected then. `--dry-run` performs the download and metadata mapping only, making no Dataverse changes; `--no-publish` leaves the created dataset as a private draft. See [Mirror a deposit to Dataverse](../How-to/mirror_dataset.md).

## fwl-io mirror-publish

```bash
DATAVERSE_TOKEN=... fwl-io mirror-publish <persistent-id> \
    [--dataverse-url URL] [--version-type {major,minor}]
```

Publishes an existing Dataverse draft by its persistent id: it never creates a dataset, so it is the second step of a create-draft-then-publish workflow, run once a draft created by `fwl-io mirror --no-publish` has been reviewed. `<persistent-id>` must be of the form `doi:<prefix>/<suffix>`, for example `doi:10.34894/EXAMPLE`. The API token is read from the `DATAVERSE_TOKEN` environment variable, never a command-line argument. `--version-type` is `major` by default and accepts only `major` or `minor`. Fails clearly if the dataset is already published or the persistent id does not resolve to a draft. See [Mirror a deposit to Dataverse](../How-to/mirror_dataset.md).

## fwl-io --version

Prints the installed version.
