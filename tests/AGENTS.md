# fwl-io test instructions

<!-- fwl-tests-core:begin sha256=0d210e346c572a96 -->
## Test rules shared by the PROTEUS ecosystem

Each test file starts with a module-level tier marker and a timeout. CI selects tests by marker, so a file without one runs in no CI job; the timeout stops a hang, it is not a target.

```python
pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]
```

| tier | what it tests | timeout |
|---|---|---|
| `unit` | Python logic with the heavy physics mocked; aim for < 100 ms | 30 s |
| `smoke` | the real binaries or solvers, one step, low resolution | 60 s |
| `integration` | several modules coupled | 300 s |
| `slow` | full physics validation | 3600 s |

Each test carries exactly one tier marker, so that the tier filters select it in the one CI job meant for it; a function marker for a second tier breaks that. Split a file whose tests need different tiers. `skip` excludes a test from every CI job.

Every new test covers an edge case (a boundary value, an empty input, an extreme physical parameter), exercises the error contract (a documented exception with the check that no side effect ran, a guard, a clamp, or the limit input of the formula), and asserts values that do not follow trivially from the implementation. A pinned value of 1 that every exponent reproduces checks nothing.

`python tools/check_test_quality.py --check` fails when the count of any rule rises above `tools/test_quality_baseline.json`; the offenders it prints are the first few of that rule in the tree, not necessarily yours. The rules: a file without a tier marker, a test without a docstring, a test with one assertion or none, a weak assertion as the only one (`is None`, `is not None`, `> 0`, `len(...) > 0`, `isinstance`), `==` next to a non-zero float literal, and an optional dependency imported without `pytest.importorskip`. `bash tools/validate_test_structure.sh` runs the repository's own structure check; the repository part of this file says what it checks.

Physics tests carry markers so their coverage is tracked apart from line coverage:
- `@pytest.mark.physics_invariant` on each test function that asserts a conservation law, a bound (T > 0, fractions in [0, 1]), a monotonicity or symmetry, or a pinned value with a discrimination guard. The marker goes on the function, not the module: structural tests in the same file do not carry it.
- `@pytest.mark.reference_pinned` (together with `physics_invariant`) on a test that pins a published benchmark, an analytical limit or a cross-implementation result; cite the paper, table or figure in the docstring.
- A discrimination guard asserts that the most plausible wrong formula (a missing factor, a swapped exponent, the wrong unit) gives a result outside the tolerance of the pinned value.

Floats: compare with `pytest.approx` or `np.testing.assert_allclose` and a tolerance you can justify from the method, not the one that makes the test pass.

Mocks: mock at the narrowest scope (the one external call), and return physically plausible values, so the code under test runs its real branches. Set random seeds and write files only under `tmp_path`.

A module-level constant read from an environment variable at import time does not change with `monkeypatch.setenv`; patch the constant with `monkeypatch.setattr`.
<!-- fwl-tests-core:end -->

## fwl-io specifics

fwl-io has no `tools/check_test_quality.py` and no `tools/validate_test_structure.sh`; the rules of the shared block above apply, and a reviewer checks them by hand. fwl-io holds no physics, so the `physics_invariant` and `reference_pinned` markers, the `validation_plot` fixture and `docs/Validation/` of the shared block do not apply; the markers are not registered here.

Markers registered in `pyproject.toml`: `unit` (one function or class, no network, no subprocess), `smoke` (the package wiring, no network), `integration` (several components, a local test server at most), `slow` (excluded from `pytest -m "not slow"`; `community.yaml` runs one such test on matching pull requests). PR CI runs `pytest -m "not slow"`; the weekly nightly runs `pytest -m slow`. Every test carries at least one tier marker, and some carry two: `test_cache.py` marks each test and mixes `unit` and `integration`; `test_cli.py`, `test_fetch.py`, `test_mirror.py` and `test_sync.py` combine a module-level `integration` with function-level `unit` markers (`pytest --collect-only -m "unit and integration"` lists them); `test_manifest.py` has one `smoke` test in a `unit` module. Do not copy these: a new test carries one tier, and a new test file takes one module-level tier and a timeout. Only `test_check.py`, `test_pins.py`, `test_prune.py` and `test_relocate.py` set a module-level timeout (30 s); one test in `test_mirror.py` sets 10 s.

- Tests run offline. A download path is tested against the `http_server` fixture of `tests/conftest.py` (a threaded server on 127.0.0.1 that serves a temporary directory) and the `sample_files` fixture (two small files and their `sha256:` registry), never against a live service.
- `tests/conftest.py` removes `FWL_DATA`, `FWL_DATA_CACHE` and `FWL_IO_OFFLINE` for every test (autouse `_clean_env`); a test sets the ones it needs with `monkeypatch` and writes only under `tmp_path`.
- `test_live.py` is the only test file that reads Zenodo; it carries `slow` and runs in the nightly, and `community.yaml` runs its community test on a pull request (see `AGENTS.md`). A new test that needs the network goes there; its `_record` helper retries a failed read (up to 4 tries).
- Dataverse and Zenodo calls go to a local fake server, an in-process fake client or a patched `requests` call (`test_mirror.py`, `test_mirror_sync.py`, `test_mirror_status.py`, `test_pins.py`, `test_sync.py`); no test writes to a live Dataverse. The tests that start a local server need to bind a port on 127.0.0.1.
- `tests/data/cache_key/` holds the fixed inputs of the `actions/cache` key tests.
