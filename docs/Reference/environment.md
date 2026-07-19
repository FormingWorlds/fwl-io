# Environment variables

| Variable | Meaning |
|---|---|
| `FWL_DATA` | Root of the writable data tree. Required unless an explicit `data_root` is passed in code or `--data-root` on the CLI; there is no silent default location. |
| `FWL_DATA_CACHE` | Optional read-only, pre-populated copy of the tree (for example a group share on a cluster). Consulted before any download, verified by checksum, never written to. Ignored when the directory does not exist. |
| `FWL_IO_OFFLINE` | Set to `1` (or `true`, `yes`, `on`) to forbid all network access. Missing or corrupt files then raise an error naming the file and the resolved data root. |
