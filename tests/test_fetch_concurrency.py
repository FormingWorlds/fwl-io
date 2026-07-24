"""Concurrency: a burst of processes fetching the same missing file must
result in exactly one download, not one per process.

This is the anti-"thundering herd" guarantee: without the per-target
inter-process lock in ``Fetcher.fetch``, N processes started together would
each miss the file and each hit the mirror at once, which is what gets the
collaboration rate-limited by Zenodo/Dataverse. The test drives real
processes (not threads) against a counting local HTTP server and asserts the
file was served once while every process still receives the correct bytes.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pooch
import pytest

from fwl_io.fetch import create_fetcher

pytestmark = pytest.mark.integration

SUBDIR = 'interior_lookup_tables/demo'
PAYLOAD = b'0.1 0.2 0.3\n' * 128
N_PROCS = 6


class _CountingHandler(SimpleHTTPRequestHandler):
    """Serve files, counting GETs and holding each one open briefly.

    The delay keeps the first (winning) process inside its download — and thus
    holding the file lock — long enough that every other process is queued on
    the lock before it is released, so the test exercises real contention
    rather than passing by luck.
    """

    hits = 0
    _lock = threading.Lock()

    def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        with _CountingHandler._lock:
            _CountingHandler.hits += 1
        time.sleep(0.5)
        super().do_GET()

    def log_message(self, *args):  # noqa: D102 -- silence request logging
        pass


def _worker(args):
    """Fetch one file in a fresh process; return its bytes for verification."""
    base_url, registry, data_root, fname = args
    fetcher = create_fetcher(
        subdir=SUBDIR, registry=registry, base_urls=[base_url], data_root=data_root
    )
    return fetcher.fetch(fname).read_bytes()


@pytest.fixture()
def counting_server(tmp_path_factory):
    """Serve one file over local HTTP, counting downloads; yield (url, registry)."""
    _CountingHandler.hits = 0
    root = tmp_path_factory.mktemp('served')
    (Path(root) / 'alpha.dat').write_bytes(PAYLOAD)
    digest = pooch.file_hash(str(Path(root) / 'alpha.dat'), alg='sha256')
    registry = {'alpha.dat': f'sha256:{digest}'}
    handler = partial(_CountingHandler, directory=str(root))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f'http://{host}:{port}/', registry
    server.shutdown()
    thread.join(timeout=5)


def test_concurrent_fetch_downloads_once(counting_server, tmp_path):
    base_url, registry = counting_server
    args = [(base_url, registry, str(tmp_path), 'alpha.dat')] * N_PROCS

    ctx = mp.get_context('spawn')
    with ctx.Pool(N_PROCS) as pool:
        results = pool.map(_worker, args)

    assert all(r == PAYLOAD for r in results), 'every process got the correct bytes'
    assert _CountingHandler.hits == 1, (
        f'expected exactly one download across {N_PROCS} processes, '
        f'got {_CountingHandler.hits} (the per-target lock did not serialise them)'
    )
    assert (tmp_path / SUBDIR / 'alpha.dat').read_bytes() == PAYLOAD
