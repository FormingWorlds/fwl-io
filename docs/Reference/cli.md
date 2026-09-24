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

Lists every dataset from all installed manifests with its key and consumers. When a manifest gives a dataset a human-readable `name` that differs from its key, that label is printed on the next line below the key. Datasets without a committed registry are flagged `[NO REGISTRY]`. Providers whose manifest fails to load are reported on stderr as `FAILED TO LOAD`, and providers left out because they conflict with another installed manifest as `NOT USED`; either sets the exit status to 1.

## fwl-io fetch

```bash
fwl-io fetch <model> [--data-root PATH] [--progress | --no-progress]
```

Fetches every dataset that lists `<model>` in its `required_by`. All datasets are attempted; failures are aggregated into one report. `--data-root` overrides the `FWL_DATA` tree.

A download progress bar shows by default when stderr is a terminal; `--progress` forces it on and `--no-progress` off. The bar needs the optional `tqdm` dependency (`pip install fwl-io[progress]`); without it, or when there is no stderr to draw on, the fetch runs with no bar.

## fwl-io check

```bash
fwl-io check <model> [--data-root PATH]
```

Reports whether every dataset that lists `<model>` in its `required_by` is present and matches its registry, without downloading anything. Each file is reported in one of five states: `ok` (present, checksum matches), `missing`, `mismatch` (present, contents differ), `unreadable` (present, could not be read to be checked), or `present`. The last means the file is there and nothing was available to verify it against, which is the case for the members of an archive dataset: the registry pins the checksum of the archive, not of the files extracted from it, so such a dataset is reported `presence only`. That is not counted as a fault, since presence is all that is checkable there, but it is never reported as verification.

Two kinds of failure are reported apart from the datasets, because they call for different repairs. `MANIFEST NOT USED` means an installed package's manifest was left out, so nothing it declares was inspected. A manifest that could not be read is always reported, since its datasets are unknown. A manifest that conflicts with another installed manifest is reported only when the conflict drops a dataset `<model>` reads, the same condition under which `fwl-io fetch <model>` fails; `fwl-io list` reports every conflict. `NOT CHECKED` means the manifest was fine but one dataset could not be resolved, most often because its registry has not been generated yet; run `fwl-io sync` for it. Either is on its own enough to fail the check.

The report goes to stdout whatever the verdict, so a caller running this to find out what is wrong gets the detail and not only the exit status. Exit is 1 on any missing, corrupt or unreadable file, any unreadable manifest, any unresolvable dataset, or a model no manifest declares. The closing line says `all data present and verified` only when every file was compared against a digest; a sound tree holding a presence-only dataset closes with `all data present, N dataset(s) by presence only` instead, and still exits 0.

Checking reads and hashes every file a plain dataset declares, so for those the cost is one full pass over the data: on a multi-gigabyte tree, or a shared cluster filesystem, expect it to take as long as reading that data once. An archive dataset costs far less, since its members have no digests to check and are only tested for presence.

Nothing is downloaded and no dataset directory or file is written, which makes this safe to run against a tree another process is reading. Resolving the data root creates that root if it does not exist, as it does for every other subcommand. The equivalent Python entry point is `fwl_io.check_for`, whose `CheckReport.ok` is false when nothing was checked, so a model that matches no dataset can never read as a clean tree. `CheckReport.verified` is the stricter question, false whenever any part of the tree was checked by presence alone.

## fwl-io relocate

```bash
fwl-io relocate [--data-root PATH] [--dry-run]
```

Moves data left by the previous layout into the place it belongs now, for a tree fetched before the current layout existed. Unmigrated code still reads the old directories, so they are otherwise left alone and age out as their consumers migrate; this is for cleaning a tree up straight away instead.

Every present file in the old location that matches its recorded digest moves; an absent file is reported for a later fetch to fill in at the new location. A present file whose contents differ, a legacy tree holding none of the registry's files, or a dataset whose registry has not been generated is reported and left exactly where it is. Verifying first is the point, since moving a stale copy would put it where the fetcher then trusts it. A file already at the new location is never overwritten: one that matches the registry is skipped and its old copy stays where it is, and one that does not blocks the dataset as a mismatch. Run again after a partial move, a dataset whose old directory holds none of the registry's files but whose new location holds verified ones is reported as absent, with the count already at the new location, and is not a fault. Once a dataset's files have moved, the emptied directories are removed, and the walk upward stops at the data root.

Some datasets cannot be verified or moved and are refused rather than touched, each named with its reason. An archive dataset's registry pins the packed archive, while an old tree holds the files extracted from it, so there is nothing to hash the tree against; move such a tree by hand, or delete it and let the fetcher rebuild it at the current location. A dataset whose registry is empty offers no files to compare, so every check over it would pass for want of anything to fail; run `fwl-io sync` for it. Both are reported only when an old directory is actually there, so a machine that never had the previous layout is unaffected.

The move opens every directory without following a symlink, so a dataset is also refused when the old directory is a symlink, when a symlink lies anywhere between the data root and the old directory or the part of the new location that exists, when a registry file or a directory inside a registry name is a symlink, when a path resolves outside the data root, or when a directory on the way cannot be read. A platform without the directory-relative open, rename and mkdir the move needs, such as Windows, is refused the same way. `--dry-run` reports every one of these refusals, so it never promises a move the real run cannot make.

A run assumes that no other process renames or replaces directories under the data root while it moves files. Unlike a deletion, a move does not check the fetch lock, so a fetch running at the same time is not detected: do not relocate while a fetch could be running. A move that fails part way puts back the files it already moved, and the datasets after it are still attempted. A dataset whose files cannot be put back is reported as split between the two locations, which needs a person to look at both directories.

A dataset already at its current location is not a fault, and a copy still sitting at the old location beside it is named rather than deleted. Nothing here removes data: the only directories it removes are ones it has just emptied itself.

A manifest that could not be included is reported apart from the datasets, as either `MANIFEST FAILED TO LOAD` or `MANIFEST NOT USED`, the same split `fwl-io check` and `fwl-io list` report. The first means the manifest could not be read at all; the second means it read fine but was dropped for conflicting with another installed manifest. Either way, any dataset that manifest would have declared is left out of the plan and cannot be relocated.

Exit is 1 when a legacy tree was found and could not be moved, when an installed manifest failed to load or was dropped as a conflict, since that manifest may be the one declaring the dataset a tree still holds, or when the shipped table of old locations could not be read, since without it no dataset has an old location to look at and a run that reported nothing would read like a tidy tree. A tree that was already tidy exits 0. `--dry-run` reports the same plan without moving anything. The equivalent Python entry points are `fwl_io.relocate_all` and `fwl_io.plan_relocations`; `Relocation.files` names the files a move would move, and is empty for every state but ready.

## fwl-io prune

```bash
fwl-io prune [--data-root PATH] [--delete] [--include-orphans] [--yes] \
    [--allow-empty-reference-set]
```

Removes versioned dataset directories no installed manifest references. A dataset pinned to a Zenodo version lives at `<data-root>/<subdir>/r<record-id>`; when a pin advances, the fetcher writes the new version beside the old one and the old one stays on disk. This finds those left-behind version directories and, only when asked, deletes them.

Each version directory is classified against the manifests installed in this environment. One a current pin uses is *referenced* and always kept. One under a subdirectory a current dataset uses, but which no current pin names, is *superseded*: an older version of a known dataset, the default delete target. One under a subdirectory no installed manifest knows is *orphaned*; on a shared data tree it may be the pinned version for a manifest installed in another environment, which this process cannot see, so it is removed only under an explicit opt-in. One that matches the version-directory name shape but carries no stamp naming its own record id and the subdir it sits under is *unrecognised*: the name alone is not proof of what the directory holds, and a fetched version copied elsewhere keeps a stamp that names its original subdir, so it is reported but never a delete target. A directory that contains a further version directory, a stamp or a subdirectory on another filesystem, or that cannot be fully read, is unrecognised too.

Nothing is deleted unless the reference set is complete. If any installed manifest fails to load, or any dataset's version directory cannot be computed, the set of referenced directories is a subset of the truth, so no directory can be proven unreferenced: the run reports the failure and deletes nothing. The same holds when part of the data root, or of a referenced version, cannot be read; a referenced version is only read through when deletion starts, so a dry run can print a clean plan that `--delete` then refuses. Deletion also refuses outright while a fetch lock is held anywhere on the data root. Each lock file is probed read-only with a shared, non-blocking flock, so the check writes nothing. A lock entry that is not a regular file (a symlink, a directory, a FIFO, a socket, a device), a lock directory that is a symlink or not a directory, and a lock file that cannot be probed each block deletion under their own reason, since prune does not probe them and cannot tell whether a fetch holds them. A lock file that this user cannot write, a lock directory this user cannot write or search (which matters for a lock file that does not exist yet), or, with no lock directory yet, a data root this user cannot write does not block, so a tree shared with other users stays usable, but the plan and the delete run print a warning: a fetch by this user through it runs without a lock, which prune cannot see, so do not fetch while prune runs. On a filesystem without flock, a fetch can leave a lock file that no probe can test; remove the files in `.fwl-io-locks` by hand when no fetch is running. Locks are only seen from the host prune runs on: on a mount whose locks are local to each host (NFS with `nolock` or `local_lock=flock`, Lustre with `localflock`), run prune only when no host is fetching. Do not run prune in a process that is also fetching, since on Linux NFS its probe can release that process's own lock. A fetch holds a lock only while it downloads one file (an archive dataset holds it for the whole download and extraction), a file already on disk is served without one, and a fetch that waits too long for a lock, or runs on a filesystem that cannot lock, proceeds without one, so this sees a fetch in the middle of a download, not one between two files: run prune when no fetch is running.

The reference set is computed once when deletion starts. Just before each directory is removed, it is checked again: it must not be a symlink, a mount point, or on another filesystem, it must resolve inside the data root, it must not be or contain a referenced version or be passed through by a symlink inside one (directly or along a chain of links), it must still carry its matching stamp and hold no nested version or subdirectory on another filesystem (a bind mount of the same filesystem is not detected), and no fetch lock may be held on the data root; every lock file is probed again right before each removal, so the cost of a run grows with the number of removals times the number of lock files, and there is one lock file for each file ever fetched into the tree. A fetch that takes its lock after that probe is not seen. Every environment that fetches into a shared tree must run filelock 3.29.5 or newer: older releases either remove a lock file when they release it or do not notice that the file they locked has been removed, and a mix of the two can hold a lock on a file that no longer has a name, which no probe can see. A directory that passes is first moved into the `.fwl-io-staging` directory under the data root and then deleted there, so it never appears half deleted at its own path; right before the move, the directory and its parent are checked once more to be the entries the checks read, by device and inode, and the entry moved is compared again after the move and put back if it differs. A directory that changed in between is reported as changed during prune and is not deleted. If the deletion fails part way, the run reports the failure and the staging path of what is left, and every later plan lists what is still there; a later download also clears staging entries last modified more than a day ago.

Prune assumes that no other process renames or replaces directories under the data root while it runs. An fwl-io fetch is kept out only while it holds the fetch lock, within the limits described above, so run prune when no fetch is running. The checks guard against a wrong classification (a missing stamp, a nested version, a reference, a symlink, a mount), and the re-checks around the move narrow, but cannot close, the window in which another program that moves directories around the tree could make prune act on a directory other than the one it checked. Do not run prune while another tool reorganises the data tree.

The default is a dry run: the plan is printed, with reclaimable bytes per category, and nothing is touched, not even a probe file; when no directory name in the data root has cased letters to show whether the filesystem ignores case, a dry run matches subdirectories case-sensitively, which only keeps more, so on such a root its counts of superseded directories can be lower than what the delete run finds. `--delete` removes the superseded directories; `--include-orphans` adds the orphaned ones. Deletion is confirmed interactively, after a warning that a superseded version may still be referenced by another environment on a shared tree, unless `--yes` skips the prompt for non-interactive use.

An orphaned directory is refused by default whenever no installed manifest declares any dataset at all, since an empty reference set is as likely to mean "nothing to keep" as "the manifests failed to install". `--allow-empty-reference-set` overrides this and lets `--include-orphans` proceed anyway; it has no effect without `--include-orphans` and no effect when at least one manifest declares a dataset.

The plan and the delete happen in two separate calls, so the tree can change between them: a fetch can start, a pin can advance, or a candidate's classification can move between superseded and orphaned. The delete call removes a directory only when the plan listed it as a target and a fresh classification still does, so nothing is deleted that the confirmation did not list. A listed directory that is no longer on disk is reported as gone; a listed target that the fresh classification keeps (now referenced, orphaned or unrecognised) is reported as not removed, and the run exits 1. It refuses the whole run when the fresh reference set no longer permits deletion, or when the plan lists a directory outside this data root.

The data root must already exist; `fwl-io prune` never creates it, and a root that does not exist is an error.

Deletion needs directory handles opened without following symlinks, directory-relative renames and flock. A platform without them, such as Windows, refuses `--delete` before anything is touched; the dry run still prints the plan there, and exits 1 when a lock directory exists, since its locks cannot be checked.

Exit is 1 when the reference set is incomplete, when the data root cannot be fully read, when a fetch lock is held or cannot be checked, when an empty reference set blocks orphan deletion without the override, when deletion is refused at apply time because the tree changed since the plan or the platform cannot delete safely, when a confirmed target is kept or a removal fails, or when a confirmation is declined; a clean plan or a completed deletion exits 0. The command runs `fwl_io.plan_prune` and, after the confirmation, `fwl_io.apply_prune` on that plan; `fwl_io.prune_versions` plans and deletes in one call.

## fwl-io mirror

```bash
DATAVERSE_TOKEN=... fwl-io mirror <zenodo-doi> --collection <alias> \
    [--dataverse-url URL] [--contact-email EMAIL] [--subject SUBJECT] \
    [--file NAME ...] [--no-publish] [--dry-run]
```

Mirrors a pinned Zenodo deposit to a Dataverse collection: it downloads and checksum-verifies the deposit's files, creates a matching Dataverse dataset with citation metadata taken from the Zenodo record, uploads the files byte-identically (tabular ingest disabled), and by default publishes the dataset, then prints the Dataverse DOI to add to the consuming manifest. The API token is read from the `DATAVERSE_TOKEN` environment variable, never a command-line argument. A contact email (`--contact-email`) is required to create a dataset; only `--dry-run`, which makes no Dataverse writes, is exempt. `--subject` is validated by the server when the dataset is created, so a value outside the target installation's citation vocabulary is rejected then. `--dry-run` performs the download and metadata mapping only, making no Dataverse changes; `--no-publish` leaves the created dataset as a private draft. `--file NAME` restricts the mirror to that file of the deposit; repeat it for several files. Without `--file`, every file of the deposit is mirrored, and a name the deposit does not hold is an error. See [Mirror a deposit to Dataverse](../How-to/mirror_dataset.md).

## fwl-io mirror-publish

```bash
DATAVERSE_TOKEN=... fwl-io mirror-publish <persistent-id> \
    [--dataverse-url URL] [--version-type VERSION_TYPE]
```

Publishes an existing Dataverse draft by its persistent id: it never creates a dataset, so it is the second step of a create-draft-then-publish workflow, run once a draft created by `fwl-io mirror --no-publish` has been reviewed. `<persistent-id>` must be of the form `doi:<prefix>/<suffix>`, for example `doi:10.34894/EXAMPLE`. The API token is read from the `DATAVERSE_TOKEN` environment variable, never a command-line argument. `--version-type` is `major` by default and accepts only `major` or `minor`. Fails clearly if the dataset is already published or the persistent id does not resolve to a draft. See [Mirror a deposit to Dataverse](../How-to/mirror_dataset.md).

## fwl-io --version

Prints the installed version.
