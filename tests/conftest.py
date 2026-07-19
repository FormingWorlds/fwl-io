"""Shared fixtures: a local HTTP server and a sample dataset.

All tests run without external network access. Download paths are exercised
against a threaded ``http.server`` bound to 127.0.0.1 that serves a
temporary directory.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pooch
import pytest


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 -- silence request logging in test output
        pass


@pytest.fixture()
def http_server(tmp_path_factory):
    """Serve a temporary directory over local HTTP; yield (base_url, root)."""
    root = tmp_path_factory.mktemp('served')
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f'http://{host}:{port}/', root
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def sample_files(http_server):
    """Publish two small files on the local server; yield (base_url, registry)."""
    base_url, root = http_server
    contents = {
        'alpha.dat': b'0.1 0.2 0.3\n',
        'beta.dat': b'columns\n1 2\n3 4\n',
    }
    registry: dict[str, str] = {}
    for name, payload in contents.items():
        path = Path(root) / name
        path.write_bytes(payload)
        registry[name] = 'sha256:' + pooch.file_hash(str(path), alg='sha256')
    return base_url, registry


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every test from the user's real data environment."""
    monkeypatch.delenv('FWL_DATA', raising=False)
    monkeypatch.delenv('FWL_DATA_CACHE', raising=False)
    monkeypatch.delenv('FWL_IO_OFFLINE', raising=False)
