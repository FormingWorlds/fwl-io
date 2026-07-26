# Manifests and registries

## Manifest schema

A manifest is a TOML file with one table per dataset. A table is recognized as a dataset by the presence of the `zenodo` key. A table without `zenodo` is a grouping level when it contains sub-tables, and is rejected otherwise.

```toml
[interior.eos.wolf_bower_2018]
name = "Wolf & Bower (2018) MgSiO3 equation of state"    # optional display name
zenodo = "10.5281/zenodo.1234567"                        # required, version DOI
dataverse = "10.34894/ABCDEF"                            # optional mirror DOI
required_by = ["aragog", "zalmoxis", "spider"]           # models that need it
extract = "tar"                                          # optional, unpack a single archive
```

The dotted table key is the dataset location below `FWL_DATA`: the table above resolves into `interior/eos/wolf_bower_2018`, and its files land in the version directory `interior/eos/wolf_bower_2018/r1234567`. The key is the only source of that location, so the declared name and the directory on disk cannot drift apart.

Validation at load time:

- Every key segment must be letters, digits, `_` or `-`, starting with a letter, digit or `_`. A quoted key carrying a separator, a dot or a `..` component is rejected, so a key can neither escape the data root nor split into an unintended path depth. A directory name containing a dot, a space or a non-ASCII character therefore has no manifest spelling.
- `zenodo` is required and must have the form `10.5281/zenodo.<record-id>`.
- `name`, when present, must be text with something in it. It is a human-readable label for the dataset; a dataset without one falls back to its dotted key.
- `dataverse`, when present, must be a DOI.
- `required_by`, when present, must be a list of model names.
- `extract`, when present, must be `"tar"` or `"zip"`.
- A dataset table must not contain sub-tables, and arrays of tables are rejected; ambiguous structures fail loudly instead of being silently dropped.
- A dataset table declares only the fields above, and a grouping level declares none: anything else raises `ManifestSchemaError`, naming the field and the manifest schema this fwl-io implements. A model ships its manifest with its own code, so a manifest can be newer than the installed fwl-io; ignoring an unknown field silently would leave the manifest asking for something it never gets, and a `required_by` written one level above its dataset would leave the dataset claiming no model needs it. The message names the action that fits the case: move a dataset field that sits too high, delete a field this fwl-io no longer takes, and for a name it does not know at all, check the spelling or upgrade. Two things are outside the check. A scalar at the manifest root is reserved for a manifest's own settings and is ignored, unless it names a dataset field or `subdir`. And a table is recognised as a dataset by its `zenodo` key, so a misspelt `zenodo` is reported as a table with no pin rather than as an unknown field.
- A manifest may declare the schema it was written against with a root `manifest_schema = <n>`. It is optional, and a manifest that declares one is held to it: only the schema the installed fwl-io implements is accepted. A higher number means the reader is too old, so the error says to upgrade. A lower one means the manifest was written for a schema that stopped loading when the number rose, so the error names both numbers and points here. Accepting only the implemented number is what sharpens the rest: an unknown field in a manifest that declares its schema is reported as a misspelling alone, with no second reading to weigh. The value must be a whole number of at least 1; `true` is rejected rather than read as 1. A table *named* `manifest_schema` is an ordinary directory level, as with `subdir`, and the key written inside a table is reported as misplaced rather than misspelt.
- A manifest that fails to load takes its whole provider with it: `discover_manifests` skips that package and logs a warning, so its other datasets disappear from the result too. Use `fwl-io list` to see the error.
- A `subdir` field is rejected on any table, dataset or grouping level, and at the manifest root: the location comes from the key. A table *named* `subdir` is an ordinary directory level.
- Within one manifest, two keys that differ only in case are rejected: they would share one directory and one registry file on a case-insensitive filesystem. Two installed packages declaring keys that collide is a separate check, tracked in [#18](https://github.com/FormingWorlds/fwl-io/issues/18).

### Schema versions

| Schema | From | What it means |
|---|---|---|
| 1 | 26.7.22 | A dataset's location is derived from its dotted table key; `subdir` is not a field. |

An error raised while reading a manifest names the schema the running code implements, so a mismatch between a manifest and an installed fwl-io can be placed against this table.

A manifest that declares `manifest_schema` is checked against it directly, and only the implemented number is accepted. Incrementing the schema is therefore a breaking change for any manifest that declares the old one, which is the intent: the number rises precisely when manifests written for the previous one stop loading, so they should fail at the increment with a message naming both numbers rather than part way through a load with a message about some individual field. A manifest that declares nothing is read on a best-effort basis, as before.

## Archive datasets

A deposit packaged as a single archive sets `extract = "tar"` or `"zip"`. Its registry lists the one archive file and its checksum; the fetcher downloads and verifies the archive, then extracts the members into the dataset directory and discards the archive, so consumers see the extracted tree rather than a tarball. Extraction is staged and the tree is moved into place atomically, so an interrupted fetch never leaves a half-populated dataset, and any member that escapes the directory (an absolute path or a `..` component) or is not a plain file or directory (a symlink, hardlink, or device node) is rejected before anything is written.

## Registries

Each dataset has a registry file next to its manifest, named `<dotted-key>.registry.txt`, one `<name> <hash>` entry per line in the format pooch reads natively. Registries are generated by `fwl-io sync` and committed; they are never edited by hand. File names may contain forward slashes for files nested below the dataset directory, and carry the file-name rules of the deposit itself: dots and mixed case are fine, while an absolute name, a backslash or a `..` component is rejected. The stricter character set applies to dataset keys, which name directories, not to the files inside them.

## Discovery

fwl-io aggregates manifests from every installed package that registers one under the `fwl_io.manifests` entry-point group. The entry point resolves to a zero-argument callable returning the manifest path. `fwl-io list` shows everything installed, flags datasets whose registry file is missing, and reports providers whose manifest failed to load; a broken provider is skipped, not fatal.

## Fetch resolution order

For every requested file:

1. Present under the data root with a matching checksum: done.
2. Present in the read-only shared cache (`FWL_DATA_CACHE`) with a matching checksum: copied in atomically.
3. Offline mode active: error naming the file and resolved root.
4. Otherwise: downloaded via Zenodo, then the Dataverse mirror, verified, placed atomically.

## Provenance

`Fetcher.provenance()` returns one record per registry entry with the file path, checksum, and the actual origin of files resolved in the current session (`local`, `cache:<path>`, or the mirror that served the download). Files not yet fetched are marked `declared:` since their true origin is unknown. These records are designed to feed per-run provenance manifests in PROTEUS.

## The FWL_DATA layout

This section is the target layout specification: new datasets and migrating models use it; existing trees keep their legacy directory names until their consumers migrate, so both forms coexist during the transition. A flat copy left by a pre-versioning fetch is re-fetched rather than adopted; a command that relocates such trees in place is tracked in [#13](https://github.com/FormingWorlds/fwl-io/issues/13).

The target tree is organized by physical domain, mirroring the package structure of the PROTEUS source tree (`src/proteus/`), with one deliberate exception: the two interior packages (`interior_struct`, `interior_energetics`) share a single `interior/` data domain, because the equation-of-state tables serve both.

Naming rules for dataset directories: all lowercase snake_case; for datasets identified by a publication, author tag first and year second, then any descriptor (`baraffe_2015`, `zeng_2019`, `dk09_1tpa_elec_free`); datasets without a citation use their plain source or product name (`solar`, `phoenix`, `muscles`).

Below its dataset directory, every dataset resolves into a version directory `r<zenodo-record-id>` derived from its manifest pin, so updated deposits land beside superseded ones instead of overwriting them. Fetching a dataset as a whole (the model-facing `fetch_for`) writes a `.fwl-io.json` stamp into its version directory, recording the schema version, the pinned DOI, the file checksums, and the fetch date, so a completed dataset directory or a shared read-only cache is self-describing.

```
FWL_DATA/
  atmos_clim/
    spectral_files/<set>/<bands>/r<recid>/
    surface_albedos/hammond_2024/r<recid>/
  atmos_chem/                        # chemistry networks and cross-sections
  interior/
    eos/<dataset>/r<recid>/
    melting_curves/<dataset>/r<recid>/
  star/
    tracks/<dataset>/r<recid>/
    spectra/<dataset>/r<recid>/
  observe/
    exoplanet_reference/r<recid>/
    mass_radius/zeng_2019/r<recid>/
  outgas/  escape/  orbit/           # created when their first dataset lands
```

The tree holds **immutable fetched reference data only**: anything generated at runtime (derived tables, interpolation caches, solver caches) belongs in run output or cache directories, never below `FWL_DATA`. This keeps a shared read-only cache trustworthy as a whole.

Models adopt this layout when they migrate to fwl-io; legacy directories from the previous layout remain readable by unmigrated code and age out when their last consumer migrates (a relocate command for cleaning local trees immediately is tracked in [#13](https://github.com/FormingWorlds/fwl-io/issues/13)). The mapping from the legacy locations:

| Legacy location (live today) | Target location |
|---|---|
| `spectral_files/<Set>/<bands>` | `atmos_clim/spectral_files/<set>/<bands>/r<recid>` |
| `surface_albedos/Hammond24` | `atmos_clim/surface_albedos/hammond_2024/r<recid>` |
| `interior_lookup_tables/1TPa-dK09-elec-free` | `interior/eos/dk09_1tpa_elec_free/r<recid>` |
| `interior_lookup_tables/Melting_curves` | `interior/melting_curves/<dataset>/r<recid>` |
| `zalmoxis_eos/EOS_PALEOS_*` | `interior/eos/paleos_*/r<recid>` |
| `stellar_evolution_tracks/{Spada,Baraffe}` | `star/tracks/{spada_2013,baraffe_2015}/r<recid>` |
| `stellar_spectra/{solar,PHOENIX,MUSCLES,Named}` | `star/spectra/{solar,phoenix,muscles,named}/r<recid>` |
| `mass_radius/Zeng2019` | `observe/mass_radius/zeng_2019/r<recid>` |
| `planet_reference/Exoplanets` | `observe/exoplanet_reference/r<recid>` |
