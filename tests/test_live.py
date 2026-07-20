"""Live checks against the real Zenodo API (nightly tier only).

These tests need network access and are excluded from PR CI by the slow
marker. They exist so drift between a committed registry and its live
Zenodo record is detected by the scheduled run instead of by a user's
failing fetch.
"""

import pytest

from fwl_io.manifest import load_manifest, shared_manifest_path
from fwl_io.sync import fetch_zenodo_registry

pytestmark = pytest.mark.slow


def test_committed_registries_match_live_zenodo_records():
    datasets = load_manifest(shared_manifest_path())
    if not datasets:
        pytest.skip('shared manifest ships no datasets yet; nothing to verify')
    for ds in datasets:
        live = fetch_zenodo_registry(ds.zenodo)
        assert live == ds.registry(), (
            f'{ds.key}: committed registry has drifted from Zenodo record {ds.zenodo}'
        )
