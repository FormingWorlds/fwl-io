# fwl-io agent instructions

fwl-io downloads, verifies and places the reference data of the PROTEUS ecosystem: manifests declare the datasets, committed registries hold their checksums, and files come from Zenodo with a DataverseNL mirror as fallback. It holds no physics. Before a first edit:

- Run every command from the repository root. Tests for `src/fwl_io/<file>.py` go in `tests/test_<file>.py` where that file exists (`doi.py`, `fs_guard.py` and `transient.py` are tested through their callers); the test rules are in `tests/AGENTS.md`.
- These commands decide whether a change is ready (CI runs the tests on Python 3.11 to 3.14 and macOS, and the lint job; Python uses single quotes and lines up to 100 characters, as `pyproject.toml` sets for ruff):

```bash
pip install -e ".[develop]"
pytest -m "not slow" --cov=src --cov-report=term
ruff check src tests
ruff format --check src tests
python tools/agents/check_agents_md.py
```

One test file: `pytest tests/test_registry.py -q`. A pull request that changes `shared_manifest.toml`, `sync.py` or `test_live.py` also runs one live test (`community.yaml`): every record of the shared manifest must be in a Zenodo community. The weekly nightly runs `pytest -m slow` (live Zenodo reads) and `fwl-io check-mirrors` (live DataverseNL pins); run them by hand only when a change touches those paths, and not in a loop. Docs: `pip install -e ".[docs]"`, then `zensical build --clean`.

<!-- fwl-core:begin sha256=1adfa510a4f117ec -->
## PROTEUS ecosystem rules

PROTEUS couples separate module repositories into one model of the evolution of rocky planets and their atmospheres. Its config selects one module per process; the config values are the lowercase names:

- `interior_energetics.module`: `aragog`, `spider` (or the built-in `boundary`); `interior_struct.module`: `zalmoxis`, `spider`
- `atmos_clim.module`: `agni`, `janus`, both with SOCRATES spectral radiative transfer
- `outgas.module`: `calliope`, `atmodeller`; `atmos_chem.module`: `vulcan`
- `star.module`: `mors`; `escape.module`: `zephyrus`, `boreas`; `orbit.module` (tides): `obliqua`, `lovepy`
- fwl-io downloads the reference data the modules use

A module change to anything PROTEUS calls or reads (a function signature, a config key, an output column, a unit) can break the coupled model while the module's own tests pass. Search `src/proteus/` in PROTEUS for the name before you change it, and name the affected PROTEUS call sites in the pull request.

### Physics and numerics

- Units differ between and within modules: MORS and VULCAN use cgs; CALLIOPE and atmodeller work with pressures in bar; AGNI takes surface pressure in bar at setup and holds Pa inside; PROTEUS states the unit of every `hf_row` key in `GetHelpfileKeys` (`src/proteus/utils/coupler.py`: atmospheric pressures in bar, interior pressures in Pa, model time in years, orbital periods in s), and its config reference pages state each key's unit (for example stellar mass in M_sun, initial partial surface pressures in bar, stellar age in Gyr). State the unit of every physical quantity in its docstring and convert explicitly at the boundary: a unit mismatch between two modules passes the tests of both and shows only in the coupled run.
- A physics test must fail for the most plausible wrong formula: check a conservation law, a bound, a monotonicity, or a published or analytical value, and assert that the wrong result falls outside the tolerance.
- Take each physical constant from one source per repository (in Python `scipy.constants` or the module's constants file). Two retyped values of one constant differ at round-off and hide real differences between code paths.
- Do not loosen a solver tolerance, a conservation check or a clamp to make a run or a test pass. Find the cause; a loosened check also hides the next defect.

### Validation

A new or changed physics routine gets the validation cases named in its confirmed plan: an analytical law, a published figure or table, or a cross-code check. Each case is a `reference_pinned` test that runs the real code on real data, with no mocks, asserts the tolerance, and writes its light and dark plot through the `validation_plot` fixture of proteus-mpl. `pytest -m reference_pinned --validation-plots` makes all plots again. Line plots are SVG, maps are AVIF; each file is at most 60 kB, under `docs/assets/validation/`. Each case has a subsection on its page in the Validation tab, with the reference, the tolerance, the test id and the command. Data read from a published figure comes from 2 independent figlab reads that agree within the stated error, as a CSV with its provenance under `tests/validation/data/`.

### Code

- Keep an inline comment block to 2 lines or fewer, and never more than 4, because a long block drifts from the code it describes. An explanation that needs more goes in the docstring, the commit message or the pull-request description.

### Branches and pull requests

- Work on a feature branch `<initials>/<short-description>`; `main` changes only through a reviewed pull request.
- Fill every section of the repository's pull-request template, where it has one.
- Before you push, run the checks listed at the top of this `AGENTS.md` and in `tests/AGENTS.md`.

### Where knowledge goes

Rules for every contributor go in the `AGENTS.md` files, the reason for a change in its commit message and pull-request description, and the scientific validation of a module in its `docs/Validation/` pages, where the repository has them. Do not add memory, notes or decision-log files to the repository: nobody maintains them, and they go stale.
<!-- fwl-core:end -->

<!-- fwl-voice:begin sha256=b4fe24baeef8b734 -->
### Commit messages and public text

Commit messages, pull-request text, code comments, docstrings, test names, test skip reasons, parametrize ids, log strings that ship with the code, and CI job and step names describe the change and the current state of the code. They name no tool used to write the change, carry no tool-attribution trailer, use no internal plan, phase or work-group labels, and use no em or en dashes (a page range in a citation is the exception).
<!-- fwl-voice:end -->

## Architecture

The command `fwl-io` (`cli.py`, entry point `fwl_io.cli:main`) covers each operation; `fwl-io --help` lists the subcommands. The modules by role:

- Declaration: `manifest.py` (dataset manifests, discovery through the `fwl_io.manifests` entry-point group, model-level fetching), `registry.py` (read and write the committed registries), `sync.py` (`fwl-io sync`: registries from the Zenodo API), `doi.py` (DOI parsing).
- Data tree: `paths.py` (data root, shared cache, offline mode), `fetch.py` (hash-verified, mirrored, offline-first fetching), `archive.py` (safe tar and zip extraction), `check.py` (does a tree match its manifest, without a download), `relocate.py` and `prune.py` (move or remove dataset directories, with the safety checks of `fs_guard.py`), `cache.py` (the key of an `actions/cache` entry that holds a data tree), `transient.py` (a transient against a permanent HTTP failure).
- Mirrors: `mirror.py` (mirror a Zenodo deposit to a DataverseNL collection), `mirror_sync.py` (create a draft mirror, pin a published one), `mirror_status.py` (`fwl-io mirror-status`: mirrors that need work; `fwl-io latest`: pinned records with a newer Zenodo version) and `pins.py` (`fwl-io check-mirrors`).
- `src/fwl_io/data/`: `shared_manifest.toml` (datasets shared by several models), `legacy_layout.toml`, and one `<dotted-key>.registry.txt` per dataset. Package data is `data/*.toml` and `data/*.txt`, so a new file type there needs `pyproject.toml`.

## Conventions

- A registry holds one `<name> <algorithm>:<hash>` line per file (`md5:` as Zenodo reports it, or `sha256:`). It is written by `fwl-io sync <manifest>` and committed with the manifest change; never edit it by hand. The checksums always come from the Zenodo record.
- Manifests name Zenodo version DOIs only, never a concept DOI. Zenodo is required; a `dataverse` line adds a mirror that holds byte-identical copies.
- `FWL_DATA` is required (or `data_root` in code, `--data-root` on the command line); there is no default location. `FWL_DATA_CACHE` is a read-only copy, searched after the data root and before any download, and never written. `FWL_IO_OFFLINE=1` makes a fetch raise an error in place of a download; the other commands (`sync`, the `mirror` commands, `check-mirrors`, `mirror-status`, `latest`) do not read it.
- A fetch resolves each file in this order: the data root with a matching checksum, the shared cache, an error in offline mode, then a download from Zenodo and then the mirror. Each file and each extracted archive tree is placed atomically, so no file is partial; `fetch_all` writes the `.fwl-io.json` stamp last, so a version directory without the stamp can be incomplete.
- Raising `manifest_schema` breaks every manifest that declares the old number; do it only with a change that old manifests cannot load.
- `mirror`, `mirror-publish` and `mirror-sync` write to DataverseNL and change a public service, and so do the workflows `mirror.yaml`, `mirror-publish.yaml` and `mirror-sync.yaml` that run them: run or dispatch them only when the task says so. `mirror --dry-run` needs no token; `mirror-sync` needs `DATAVERSE_TOKEN` even with `--dry-run`; `mirror-publish` has no dry run. `mirror-pin` (and `mirror-pin.yaml`) writes the pin into a manifest, by default the shared one.
- `fwl-io prune` is a dry run unless `--delete` is given, and `--delete` asks for a confirmation unless `--yes` is given; `--include-orphans` also removes directories that no installed manifest knows. `fwl-io relocate --dry-run` reports without a move.
- The workflows read the exit codes and output lines of `check-mirrors` (`nightly.yml`), `mirror-status` (`mirror-sync.yaml`, weekly) and `latest` (`latest-paleos.yaml`, daily); change these only together with the workflow.
- Consumers: PROTEUS (`src/proteus/utils/data.py` and others), MORS, Zalmoxis, JANUS and ZEPHYRUS read data through fwl-io, and several register their own manifests under `fwl_io.manifests`. A change to the manifest schema, the registry format, the data layout or a command breaks them; name the affected repositories in the pull request.
- Releases are CalVer tags on `main`, `YY.MM.DD`, with `.N` for a second release on one day; setuptools-scm derives the version from the tag, and publishing a GitHub release runs the PyPI upload (`publish.yaml`).

## Agent files

`AGENTS.md` and `tests/AGENTS.md` hold the instructions; each `CLAUDE.md` holds only `@AGENTS.md`. The shared blocks come from PROTEUS (`tools/agents/sync_core.py`); edit them there, not here. `python tools/agents/check_agents_md.py` checks the block hashes, the `CLAUDE.md` files and the size caps, and the CI lint job runs it.
