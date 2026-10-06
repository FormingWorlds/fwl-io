"""Live checks against the real Zenodo API (nightly tier only).

These tests need network access and are excluded from PR CI by the slow
marker. They exist so drift between a committed registry and its live
Zenodo record is detected by the scheduled run instead of by a user's
failing fetch.
"""

import pytest

from fwl_io.manifest import load_manifest, shared_manifest_path
from fwl_io.sync import fetch_zenodo_registry, select_files

pytestmark = pytest.mark.slow


def test_committed_registries_match_live_zenodo_records():
    datasets = load_manifest(shared_manifest_path())
    assert datasets, 'the shared manifest declares no datasets'
    for ds in datasets:
        live = fetch_zenodo_registry(ds.zenodo)
        if ds.files is not None:
            live = select_files(live, ds.files, source=ds.key)
        assert live == ds.registry(), (
            f'{ds.key}: committed registry has drifted from Zenodo record {ds.zenodo}'
        )


def test_shared_manifest_pins_are_served_by_their_dataverse_mirrors():
    """Every pin resolves to a released DataverseNL dataset holding the registry files with
    their checksums, so the fallback download path works when Zenodo is down."""
    from fwl_io.pins import check_mirrors

    report = check_mirrors('https://dataverse.nl', load_manifest(shared_manifest_path()))
    assert report.passed, 'the shared manifest pins no dataset'
    assert report.ok, report.summary()
