"""Tests for the Dataverse pin check (``fwl-io check-mirrors``), against a fake client."""

from __future__ import annotations

import pytest

from fwl_io import pins
from fwl_io.cli import main
from fwl_io.manifest import Dataset, load_manifest, shared_manifest_path
from fwl_io.mirror import DataverseClient, DataverseError, DataverseRetryableError
from fwl_io.pins import Unreachable, check_mirrors, pin_problem, zenodo_sizes

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

MD5_A = 'md5:' + 'a' * 32
MD5_B = 'md5:' + 'b' * 32
SOURCE = '10.5281/zenodo.1'


def _dataset(tmp_path, key='group.data', pin='10.34894/ABCDEF', files=None, zenodo=SOURCE):
    registry = tmp_path / f'{key}.registry.txt'
    entries = {'a.dat': MD5_A} if files is None else files
    registry.write_text(''.join(f'{n} {h}\n' for n, h in entries.items()))
    return Dataset(key=key, name=key, zenodo=zenodo, dataverse=pin, registry_path=registry)


def _file(name, kind, value, size):
    return {
        'dataFile': {'filename': name, 'checksum': {'type': kind, 'value': value}, 'filesize': size}
    }


class FakeClient:
    """Answers the dataset read in the DataverseNL shape, or raises an error."""

    def __init__(self, state='RELEASED', files=None, error=None, source=SOURCE, extra=None):
        self.error = error
        listed = [_file('a.dat', 'SHA-1', 'f' * 40, 10)] if files is None else files
        description = [{'dsDescriptionValue': {'value': f'Mirror of Zenodo deposit {source}.'}}]
        fields = [{'typeName': 'dsDescription', 'value': description}] + (extra or [])
        self.body = {
            'data': {
                'latestVersion': {
                    'versionState': state,
                    'metadataBlocks': {'citation': {'fields': fields}},
                    'files': listed,
                }
            }
        }
        self.calls = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('params')))
        if self.error:
            raise self.error
        return self.body


def _sizes(doi):
    return {'a.dat': 10, 'b.dat': 7}


def test_a_released_mirror_with_every_registry_file_passes(tmp_path):
    """SHA-1 files pass on the Zenodo size, an MD5 file on its checksum (any case) without a
    size lookup, and an extra mirror file (a subset of a deposit) is allowed."""
    client = FakeClient()
    assert pin_problem(_dataset(tmp_path), client, sizes=_sizes) is None
    assert client.calls == [
        ('GET', '/api/datasets/:persistentId/', {'persistentId': 'doi:10.34894/ABCDEF'})
    ]
    md5 = FakeClient(files=[_file('a.dat', 'MD5', 'A' * 32, 1), _file('c.dat', 'MD5', 'c' * 32, 1)])
    assert pin_problem(_dataset(tmp_path), md5, sizes=lambda doi: pytest.fail('no lookup')) is None


@pytest.mark.parametrize(
    ('client', 'reason'),
    [
        (FakeClient(state='DRAFT'), "latest version is 'DRAFT'"),
        (FakeClient(source='10.5281/zenodo.2'), 'does not name Zenodo 10.5281/zenodo.1'),
        (FakeClient(source='10.5281/zenodo.12'), 'does not name Zenodo 10.5281/zenodo.1'),
        (
            FakeClient(
                source='10.5281/zenodo.2',
                extra=[
                    {'typeName': 'otherId', 'value': 'Mirror of Zenodo deposit 10.5281/zenodo.1.'}
                ],
            ),
            'does not name Zenodo 10.5281/zenodo.1',
        ),
        (FakeClient(files=[]), 'a.dat missing'),
        (FakeClient(files=[_file('a.dat', 'MD5', 'b' * 32, 10)]), 'a.dat checksum differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 11)]), 'a.dat size differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, None)]), 'a.dat size differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 10)] * 2), 'holds a.dat twice'),
        (FakeClient(error=DataverseError('404 not found')), 'cannot read doi:10.34894/ABCDEF'),
    ],
)
def test_a_mirror_that_cannot_serve_the_registry_is_named(tmp_path, client, reason):
    """Each failure mode returns its own reason; a longer Zenodo id or the DOI in another
    citation field does not pass as the source."""
    why = pin_problem(_dataset(tmp_path), client, sizes=_sizes)
    assert why is not None
    assert reason in why


def test_a_size_missing_on_both_sides_is_not_a_match(tmp_path):
    """A mirror file without a size does not match a Zenodo record that lacks the file."""
    client = FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, None)])
    assert 'size differs' in pin_problem(_dataset(tmp_path), client, sizes=lambda doi: {})
    assert pin_problem(_dataset(tmp_path), FakeClient(), sizes=_sizes) is None


def test_an_empty_registry_is_not_a_pass(tmp_path):
    """A registry with no file names checks nothing, so it is a reason, not a pass."""
    assert pin_problem(_dataset(tmp_path, files={}), FakeClient(), sizes=_sizes) == (
        'the registry lists no files'
    )
    assert pin_problem(_dataset(tmp_path), FakeClient(), sizes=_sizes) is None


def test_a_transient_server_or_zenodo_error_is_unreachable(tmp_path):
    """A bot-check page or gateway error, and an unreadable Zenodo record, raise Unreachable
    instead of reporting the pin as wrong."""
    with pytest.raises(Unreachable, match='doi:10.34894/ABCDEF: bot check'):
        pin_problem(_dataset(tmp_path), FakeClient(error=DataverseRetryableError('bot check')))

    def down(doi):
        raise ConnectionError('zenodo down')

    with pytest.raises(Unreachable, match='Zenodo 10.5281/zenodo.1 file sizes: zenodo down'):
        pin_problem(_dataset(tmp_path), FakeClient(), sizes=down)


def _patch(monkeypatch, found, errors=None, client=None, seen=None):
    monkeypatch.setattr(pins, '_discover', lambda: ({'m': found}, errors or {}))

    def make(url, token):
        if seen is not None:
            seen.append((url, token))
        return client or FakeClient()

    monkeypatch.setattr(pins, 'DataverseClient', make)
    monkeypatch.setattr(pins, 'zenodo_sizes', _sizes)


def test_check_mirrors_sorts_every_dataset(tmp_path, monkeypatch):
    """Pins pass or fail, a dataset with a missing registry fails without stopping the run,
    a manifest that fails to load is a failure, and unpinned datasets are only listed."""
    good = _dataset(tmp_path, 'group.good')
    bad = _dataset(tmp_path, 'group.bad', files={'b.dat': MD5_B})
    broken = _dataset(tmp_path, 'group.broken')
    broken.registry_path.unlink()
    loose = _dataset(tmp_path, 'group.loose', pin=None)
    _patch(monkeypatch, [good, bad, broken, loose], errors={'other': 'cannot load'})
    report = check_mirrors('https://example.org')
    assert report.passed == ['group.good'] and report.unpinned == ['group.loose']
    assert sorted(report.failed) == ['group.bad', 'group.broken', 'manifest other']
    assert report.failed['group.broken'].startswith('FileNotFoundError')
    assert not report.ok
    assert report.summary().splitlines()[-2:] == [
        'pins served by their mirror: 1, wrong: 3, not checked (server unreachable): 0, '
        'datasets without a pin: 1',
        'unpinned group.loose',
    ]


def test_check_mirrors_reads_each_zenodo_record_once(tmp_path, monkeypatch):
    """Datasets that share a Zenodo record (one deposit split in several datasets) read its
    file sizes once."""
    asked = []
    _patch(monkeypatch, [_dataset(tmp_path, 'g.one'), _dataset(tmp_path, 'g.two')])
    monkeypatch.setattr(pins, 'zenodo_sizes', lambda doi: asked.append(doi) or _sizes(doi))
    assert check_mirrors('https://example.org').passed == ['g.one', 'g.two']
    assert asked == [SOURCE]


@pytest.mark.parametrize(
    ('client', 'found', 'code', 'text'),
    [
        (FakeClient(), True, 0, 'pins served by their mirror: 1'),
        (FakeClient(state='DRAFT'), True, 1, 'FAIL group.good'),
        (FakeClient(error=DataverseRetryableError('504')), True, 2, 'UNREACHABLE group.good'),
        (FakeClient(), False, 1, 'pins served by their mirror: 0'),
    ],
)
def test_check_mirrors_command_exits_by_the_verdict(
    tmp_path, monkeypatch, capsys, client, found, code, text
):
    """Exit 0 when every pin is served, 1 for a wrong pin or nothing to check, 2 when only
    the server could not be read; the given URL reaches the client with no token."""
    seen = []
    _patch(
        monkeypatch, [_dataset(tmp_path, 'group.good')] if found else [], client=client, seen=seen
    )
    assert main(['check-mirrors', '--dataverse-url', 'https://x.example']) == code
    assert text in capsys.readouterr().out
    assert seen == [('https://x.example', '')]


def test_an_empty_token_sends_no_api_key(monkeypatch):
    """A read with an empty token sends no X-Dataverse-key header; a set token is sent."""
    sent = []

    class Response:
        ok, status_code, headers, text, content = (
            True,
            200,
            {'content-type': 'application/json'},
            '{}',
            b'{}',
        )

        def json(self):
            return {}

    def request(method, url, **kwargs):
        sent.append(kwargs.get('headers'))
        return Response()

    monkeypatch.setattr('fwl_io.mirror.requests.request', request)
    DataverseClient('https://example.org', token='')._request('GET', '/api/x')
    DataverseClient('https://example.org', token='t')._request('GET', '/api/x')
    assert sent == [{}, {'X-Dataverse-key': 't'}]


@pytest.mark.parametrize(
    'files',
    [
        [{'key': 'a.dat', 'size': 10}, {'key': 'b.dat', 'size': 7}],
        {'entries': {'a.dat': {'size': 10}, 'b.dat': {'size': 7}}},
    ],
)
def test_zenodo_sizes_reads_both_record_shapes(monkeypatch, files):
    """The legacy list and the InvenioRDM entries map give the same name-to-size map."""
    asked = []
    monkeypatch.setattr(
        pins, 'fetch_zenodo_record', lambda doi: asked.append(doi) or {'files': files}
    )
    assert zenodo_sizes(SOURCE) == {'a.dat': 10, 'b.dat': 7}
    assert asked == [SOURCE]


def test_shared_manifest_pins_are_consistent():
    """Every pin is a DataverseNL DOI, and datasets that share a Zenodo record share one pin
    (the PALEOS record backs three datasets)."""
    datasets = load_manifest(shared_manifest_path())
    pinned = [ds for ds in datasets if ds.dataverse]
    assert pinned
    assert all(ds.dataverse.startswith('10.34894/') and len(ds.dataverse) == 15 for ds in pinned)
    by_record: dict[str, set[str]] = {}
    for ds in pinned:
        by_record.setdefault(ds.zenodo, set()).add(ds.dataverse)
    assert all(len(dois) == 1 for dois in by_record.values())
    assert len(by_record) < len(pinned)
