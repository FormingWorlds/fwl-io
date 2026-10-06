"""Tests for the Dataverse pin check (``fwl-io check-mirrors``), against a fake client."""

from __future__ import annotations

import pytest

from fwl_io.cli import main
from fwl_io.manifest import Dataset, load_manifest, shared_manifest_path
from fwl_io.mirror import DataverseClient, DataverseError
from fwl_io.pins import check_mirrors, pin_problem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

MD5_A = 'md5:' + 'a' * 32
MD5_B = 'md5:' + 'b' * 32


def _dataset(tmp_path, key='group.data', pin='10.34894/ABCDEF', files=None):
    registry = tmp_path / f'{key}.registry.txt'
    registry.write_text(''.join(f'{n} {h}\n' for n, h in (files or {'a.dat': MD5_A}).items()))
    return Dataset(
        key=key, name=key, zenodo='10.5281/zenodo.1', dataverse=pin, registry_path=registry
    )


class FakeClient:
    """Answers the dataset read with one version, or raises a DataverseError."""

    def __init__(self, state='RELEASED', files=None, error=None, source='10.5281/zenodo.1'):
        self.error = error
        listed = files if files is not None else {'a.dat': ('MD5', 'a' * 32, 10)}
        self.body = {
            'data': {
                'latestVersion': {
                    'versionState': state,
                    'metadataBlocks': {'citation': {'note': f'Mirror of Zenodo deposit {source}.'}},
                    'files': [
                        {
                            'dataFile': {
                                'filename': n,
                                'checksum': {'type': t, 'value': v},
                                'filesize': size,
                            }
                        }
                        for n, (t, v, size) in listed.items()
                    ],
                }
            }
        }
        self.calls = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('params')))
        if self.error:
            raise self.error
        return self.body


def test_a_released_mirror_with_every_registry_file_passes(tmp_path):
    """A released version holding each registry file with its MD5 serves the dataset; an
    extra mirror file (a dataset that takes a subset of a deposit) is allowed."""
    client = FakeClient(files={'a.dat': ('MD5', 'a' * 32, 10), 'other.dat': ('MD5', 'c' * 32, 5)})
    assert (
        pin_problem(_dataset(tmp_path), client, sizes=lambda doi: pytest.fail('no size lookup'))
        is None
    )
    assert client.calls == [
        ('GET', '/api/datasets/:persistentId/', {'persistentId': 'doi:10.34894/ABCDEF'})
    ]


@pytest.mark.parametrize(
    ('client', 'reason'),
    [
        (FakeClient(state='DRAFT'), "latest version is 'DRAFT'"),
        (FakeClient(source='10.5281/zenodo.2'), 'does not name Zenodo 10.5281/zenodo.1'),
        (FakeClient(files={}), 'a.dat missing'),
        (FakeClient(files={'a.dat': ('MD5', 'b' * 32, 10)}), 'a.dat checksum differs'),
        (FakeClient(files={'a.dat': ('SHA-1', 'f' * 40, 11)}), 'a.dat size differs from Zenodo'),
        (
            FakeClient(error=DataverseError('504 Gateway Time-out')),
            'cannot read doi:10.34894/ABCDEF',
        ),
    ],
)
def test_a_mirror_that_cannot_serve_the_registry_is_named(tmp_path, client, reason):
    """Each failure mode returns its own reason instead of passing."""
    why = pin_problem(_dataset(tmp_path), client, sizes=lambda doi: {'a.dat': 10})
    assert why is not None and reason in why


def test_a_sha1_mirror_is_checked_by_the_zenodo_file_size(tmp_path):
    """DataverseNL stores SHA-1 and the registry MD5: the file passes on the size of the
    pinned Zenodo record, read once per dataset; an unreadable record is the reason."""
    client = FakeClient(files={'a.dat': ('SHA-1', 'f' * 40, 10), 'b.dat': ('SHA-1', 'e' * 40, 7)})
    asked = []
    ds = _dataset(tmp_path, files={'a.dat': MD5_A, 'b.dat': MD5_B})
    assert (
        pin_problem(ds, client, sizes=lambda doi: asked.append(doi) or {'a.dat': 10, 'b.dat': 7})
        is None
    )
    assert asked == ['10.5281/zenodo.1']

    def down(doi):
        raise ConnectionError('zenodo down')

    assert 'cannot read Zenodo 10.5281/zenodo.1' in pin_problem(ds, client, sizes=down)


def test_check_mirrors_sorts_pinned_and_unpinned_datasets(tmp_path, monkeypatch):
    """Pinned datasets pass or fail on their mirror; unpinned ones are listed and do not
    fail the check."""
    good = _dataset(tmp_path, 'group.good')
    bad = _dataset(tmp_path, 'group.bad', files={'b.dat': MD5_B})
    loose = _dataset(tmp_path, 'group.loose', pin=None)
    monkeypatch.setattr('fwl_io.pins.DataverseClient', lambda url, token: FakeClient())
    report = check_mirrors('https://example.org', [good, bad, loose])
    assert (report.passed, list(report.failed), report.unpinned) == (
        ['group.good'],
        ['group.bad'],
        ['group.loose'],
    )
    assert not report.ok
    assert report.summary().splitlines() == [
        'FAIL group.bad: b.dat missing',
        '1 pinned datasets served by their mirror, 1 not, 1 without a pin',
        'unpinned group.loose',
    ]


def test_check_mirrors_command_exits_by_the_verdict(tmp_path, monkeypatch, capsys):
    """check-mirrors exits 0 when every pin serves its dataset and 1 otherwise."""
    pinned = [_dataset(tmp_path, 'group.good')]
    monkeypatch.setattr('fwl_io.pins.discover_manifests', lambda: {'m': pinned})
    monkeypatch.setattr('fwl_io.pins.DataverseClient', lambda url, token: FakeClient())
    assert main(['check-mirrors']) == 0
    assert '1 pinned datasets served by their mirror' in capsys.readouterr().out
    monkeypatch.setattr('fwl_io.pins.DataverseClient', lambda url, token: FakeClient(state='DRAFT'))
    assert main(['check-mirrors']) == 1
    assert 'FAIL group.good' in capsys.readouterr().out


def test_an_empty_token_sends_no_api_key():
    """A read of published data sends no X-Dataverse-key; a set token is sent."""
    assert DataverseClient('https://example.org', token='')._headers == {}
    assert DataverseClient('https://example.org', token='t')._headers == {'X-Dataverse-key': 't'}


def test_shared_manifest_pins_are_dataverse_nl_dois():
    """Every pin in the shared manifest names a DataverseNL DOI, and the datasets without a
    published mirror (Frostflow 4096, Honeyside 256, PHOENIX) stay unpinned."""
    datasets = load_manifest(shared_manifest_path())
    pins = {ds.key: ds.dataverse for ds in datasets if ds.dataverse}
    assert all(doi.startswith('10.34894/') for doi in pins.values())
    assert sorted(ds.key for ds in datasets if not ds.dataverse) == [
        'atmos_clim.spectral_files.frostflow.4096',
        'atmos_clim.spectral_files.honeyside.256',
        'star.spectra.phoenix',
    ]


@pytest.mark.parametrize(
    'files',
    [
        [{'key': 'a.dat', 'size': 10}, {'key': 'b.dat', 'size': 7}],
        {'entries': {'a.dat': {'size': 10}, 'b.dat': {'size': 7}}},
    ],
)
def test_zenodo_sizes_reads_both_record_shapes(monkeypatch, files):
    """The legacy list and the InvenioRDM entries map give the same name-to-size map."""
    from fwl_io import pins

    asked = []
    monkeypatch.setattr(
        pins, 'fetch_zenodo_record', lambda doi: asked.append(doi) or {'files': files}
    )
    assert pins.zenodo_sizes('10.5281/zenodo.1') == {'a.dat': 10, 'b.dat': 7}
    assert asked == ['10.5281/zenodo.1']
