"""Live checks against the real Zenodo API (nightly tier only).

These tests need network access and are excluded from PR CI by the slow
marker. They exist so drift between a committed registry and its live
Zenodo record is detected by the scheduled run instead of by a user's
failing fetch.
"""

import time

import pytest
import requests

from fwl_io.manifest import load_manifest, shared_manifest_path
from fwl_io.sync import (
    ZENODO_COMMUNITIES,
    fetch_zenodo_record,
    fetch_zenodo_registry,
    in_community,
    select_files,
)

pytestmark = pytest.mark.slow


def test_committed_registries_match_live_zenodo_records():
    datasets = load_manifest(shared_manifest_path())
    assert datasets, 'the shared manifest declares no datasets'
    records = {}
    for ds in datasets:
        if ds.zenodo not in records:
            records[ds.zenodo] = fetch_zenodo_registry(ds.zenodo)
        live = records[ds.zenodo]
        if ds.files is not None:
            live = select_files(live, ds.files, source=ds.key)
        assert live == ds.registry(), (
            f'{ds.key}: committed registry has drifted from Zenodo record {ds.zenodo}'
        )


def _record(doi, tries=4):
    """Read a Zenodo record; a failed read is tried again, since one timeout is common."""
    for _ in range(tries - 1):
        try:
            return fetch_zenodo_record(doi)
        except requests.RequestException:
            time.sleep(15)
    return fetch_zenodo_record(doi)


def test_shared_manifest_records_are_in_a_zenodo_community():
    """Every record that the shared manifest pins is an accepted record of a community of
    the framework. Every record is read before the test fails."""
    records = sorted({ds.zenodo for ds in load_manifest(shared_manifest_path())})
    assert records, 'the shared manifest declares no datasets'
    outside, unread = [], []
    for doi in records:
        try:
            if not in_community(_record(doi)):
                outside.append(doi)
        except requests.RequestException as exc:
            unread.append(f'{doi} ({exc})')
    assert not outside, f'not in a Zenodo community of {ZENODO_COMMUNITIES}: {outside}'
    assert not unread, f'not read after the retries: {unread}'
