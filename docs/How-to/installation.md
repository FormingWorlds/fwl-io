# Installation

## Regular install

```bash
pip install fwl-io
```

Until the first PyPI release lands, install from the repository instead:

```bash
pip install git+https://github.com/FormingWorlds/fwl-io.git
```

Requires Python 3.11 or newer. The runtime dependencies are [pooch](https://www.fatiando.org/pooch/) and [requests](https://requests.readthedocs.io/).

## Development install

```bash
git clone https://github.com/FormingWorlds/fwl-io.git
cd fwl-io
pip install -e ".[develop]"
```

Run the checks:

```bash
pytest -m "unit or smoke"       # fast tier, no network
pytest                          # full suite; uses a local test server only
ruff check src tests && ruff format --check src tests
```

The test suite never contacts external services; download logic is exercised against a local HTTP server.

## Documentation build

```bash
pip install -e ".[docs]"
zensical serve        # local preview
zensical build --clean
```
