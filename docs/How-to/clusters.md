# Run on clusters

HPC compute nodes usually have no internet access, and many jobs share one data tree. fwl-io is built for that situation.

## Offline mode

Set

```bash
export FWL_IO_OFFLINE=1
```

and fwl-io never touches the network: files resolve from the local tree (and the shared cache, below) or fail with an error naming the missing file and the resolved data root. Populate `FWL_DATA` from a login node or a transfer job before submitting.

## Read-only group cache

Point

```bash
export FWL_DATA_CACHE=/projects/shared/fwl_data
```

at a pre-populated, read-only copy of the data tree (a group share). Before any download, fwl-io looks for the file there, verifies its checksum, and copies it into the job's own `FWL_DATA` atomically. The cache is never written to. Combined with offline mode this serves entire array-job campaigns from one shared tree with zero network access.

## Concurrent jobs

Many jobs may fetch into one `FWL_DATA` tree concurrently. All writes are staged in `<FWL_DATA>/.fwl-io-staging` on the same filesystem and moved into place with an atomic rename, so a file is either absent or complete and verified; jobs racing on the same file converge on the same verified content. Staging entries left behind by killed jobs are pruned once they are older than a day, the next time any fetcher runs against the tree.

To stop an array job from stampeding the mirrors, each target is also guarded by a per-file inter-process lock under `<FWL_DATA>/.fwl-io-locks`: when many jobs miss the same file at once, one downloads while the rest wait and then reuse the result. The lock is coherent across nodes wherever the shared filesystem supports it (verified on Kapteyn NFS and Hábrók Lustre, which is mounted with `flock`). It is best-effort by design: on a filesystem with no working lock manager, or if a lock holder stalls past `lock_timeout` (five minutes by default), waiters log a warning and fetch unguarded rather than failing or blocking the batch — never worse than having no lock. `.fwl-io-staging` and `.fwl-io-locks` are internal bookkeeping directories; leave them out of `queued`-style completeness checks of the data tree.

## Recommended pattern for SLURM campaigns

1. On a login node: `fwl-io fetch <model>` for every model in the pipeline, into the shared tree.
2. In job scripts: `export FWL_IO_OFFLINE=1` and, if using a group share, `FWL_DATA_CACHE`.
3. A missing dataset then fails fast at job start with a clear message instead of thousands of jobs attempting downloads.
