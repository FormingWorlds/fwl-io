# Contributing

fwl-io is at version 0 and open for suggestions from the collaboration. The API is not yet frozen, so this is the right moment for structural feedback.

## Giving feedback

- Open a [GitHub issue](https://github.com/FormingWorlds/fwl-io/issues) for any suggestion, from naming to architecture. If it concerns one of the stated [design decisions](https://proteus-framework.org/fwl-io/Explanations/design/), say which one.
- Small fixes are welcome directly as pull requests.

## Development setup

```bash
git clone https://github.com/FormingWorlds/fwl-io.git
cd fwl-io
pip install -e ".[develop]"
```

## Before opening a PR

```bash
pytest                                          # full suite, no network needed
ruff check src tests && ruff format --check src tests
```

- Keep tests offline: exercise download paths against a local HTTP server (see `tests/conftest.py`), never against live services in the PR tier.
- Use the tiered pytest markers (`unit`, `smoke`, `integration`, `slow`).
- New behavior needs a test that fails without the change.

## Releasing

Releases are tagged with CalVer (`YY.MM.DD`) on `main`; setuptools-scm derives the package version from the tag.
