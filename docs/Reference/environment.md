# Environment variables

| Variable | Meaning |
|---|---|
| `FWL_DATA` | Root of the writable data tree. Required unless an explicit `data_root` is passed in code or `--data-root` on the CLI; there is no silent default location. |
| `FWL_DATA_CACHE` | Optional read-only, pre-populated copy of the tree (for example a group share on a cluster). Consulted before any download, verified by checksum, never written to. Ignored when the directory does not exist. |
| `FWL_IO_OFFLINE` | Set to `1` (or `true`, `yes`, `on`) to forbid all network access: a file fetch (`Fetcher.fetch`, `Fetcher.fetch_all`) raises `OfflineDataError` for a missing or corrupt data file instead of a download, a model fetch (`fetch_for`, `fwl-io fetch <model>`) reports each failed dataset in one `RuntimeError`, and a command that needs the network (`sync`, `mirror`, `mirror-publish`, `mirror-sync`, `mirror-pin`, `check-mirrors`, `mirror-status`, `latest`) raises `OfflineModeError` before it connects. The `OfflineDataError` message names the file and the resolved data root. |
