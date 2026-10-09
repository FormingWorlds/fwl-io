"""Tests for ``fwl-io mirror-status``: datasets without a Dataverse pin or with a newer
Zenodo version, against a fake Zenodo."""

import pytest
import requests

from fwl_io import mirror_status as status
from fwl_io.cli import main
from fwl_io.manifest import Dataset
from fwl_io.mirror_status import StatusReport, latest_record_id, mirror_status

pytestmark = pytest.mark.unit


def _ds(key, recid, pin='10.34894/ABCDEF'):
    return Dataset(key=key, name=key, zenodo=f'10.5281/zenodo.{recid}', dataverse=pin)


def test_datasets_are_sorted_by_what_their_mirror_needs():
    """A pinned current record is in order; a missing pin, a newer version (also together)
    and a failed Zenodo read are named; each record is read once."""
    asked = []

    def latest(recid):
        asked.append(recid)
        if recid == '4':
            raise requests.ConnectionError('down')
        return {'1': '1', '2': '2', '3': '30'}[recid]

    datasets = [
        _ds('g.ok', 1),
        _ds('g.ok2', 1),
        _ds('g.unpinned', 2, pin=None),
        _ds('g.both', 3, pin=None),
        _ds('g.down', 4),
    ]
    report = mirror_status(datasets, latest=latest)
    assert report.ok == ['g.ok', 'g.ok2']
    assert report.unpinned == {'g.unpinned': '10.5281/zenodo.2', 'g.both': '10.5281/zenodo.3'}
    assert report.stale == {'g.both': 'pins Zenodo 3, newest version is 30'}
    assert report.unreadable == {'g.down': 'Zenodo 4: down'}
    assert asked == ['1', '2', '3', '4']
    assert report.exit_code == 5


@pytest.mark.parametrize(
    ('report', 'code'),
    [
        (StatusReport(ok=['a']), 0),
        (StatusReport(ok=['a'], unreadable={'b': 'x'}), 3),
        (StatusReport(unpinned={'a': 'z'}, unreadable={'b': 'x'}), 5),
        (StatusReport(stale={'a': 'z'}), 5),
        (StatusReport(stale={'a': 'z'}, manifest_errors={'m': 'x'}), 1),
    ],
)
def test_the_exit_code_puts_a_manifest_error_before_work_before_an_outage(report, code):
    """1 for a manifest error, then 5 for a dataset that needs work, then 3 for a record
    that could not be read; 0 when all are in order."""
    assert report.exit_code == code


def test_the_command_prints_the_rows_and_exits_by_the_verdict(monkeypatch, capsys):
    """The command reads every installed manifest and prints one line per dataset to act on."""
    monkeypatch.setattr(
        status, '_discover', lambda: ({'m': [_ds('g.ok', 1), _ds('g.new', 2, pin=None)]}, {})
    )
    monkeypatch.setattr(status, 'latest_record_id', lambda recid: recid)
    assert main(['mirror-status']) == 5
    out = capsys.readouterr().out.splitlines()
    assert out == [
        'UNPINNED g.new: 10.5281/zenodo.2',
        'in order: 1, without a pin: 1, with a newer Zenodo version: 0, '
        'not checked (Zenodo could not be read): 0',
    ]


def test_a_manifest_that_fails_to_load_is_reported(monkeypatch):
    """A manifest error is listed and fails the run."""
    monkeypatch.setattr(status, '_discover', lambda: ({}, {'broken': 'cannot load'}))
    report = mirror_status(latest=lambda recid: recid)
    assert report.manifest_errors == {'broken': 'cannot load'}
    assert report.summary().splitlines()[0] == 'FAIL manifest broken: cannot load'


def test_the_newest_version_is_read_from_the_versions_endpoint(monkeypatch):
    """The newest record id comes from <api>/<recid>/versions/latest; an HTTP error raises."""
    seen = []

    class _Reply:
        def __init__(self, status):
            self.status = status

        def raise_for_status(self):
            if self.status != 200:
                raise requests.HTTPError(str(self.status))

        def json(self):
            return {'id': 31}

    def get(url, timeout):
        seen.append(url)
        return _Reply(404 if 'zenodo.bad' in url else 200)

    monkeypatch.setattr(status.requests, 'get', get)
    assert latest_record_id('3', api_base='https://zenodo.example/api/records') == '31'
    assert seen == ['https://zenodo.example/api/records/3/versions/latest']
    with pytest.raises(requests.HTTPError):
        latest_record_id('3', api_base='https://zenodo.bad/api/records')
