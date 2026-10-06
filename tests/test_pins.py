"""Tests for the Dataverse pin check (``fwl-io check-mirrors``), against a fake client."""

from __future__ import annotations

import re
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from fwl_io import pins
from fwl_io.cli import main
from fwl_io.manifest import Dataset, load_manifest, shared_manifest_path
from fwl_io.mirror import (
    DataverseClient,
    DataverseError,
    DataverseRetryableError,
    zenodo_record_to_citation,
)
from fwl_io.pins import MirrorReport, Unreachable, check_mirrors, pin_problem, zenodo_sizes

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

MD5_A = 'md5:' + 'a' * 32
MD5_B = 'md5:' + 'b' * 32
SOURCE = '10.5281/zenodo.1'


def _dataset(tmp_path, key='group.data', pin='10.34894/ABCDEF', files=None, zenodo=SOURCE):
    registry = tmp_path / f'{key}.registry.txt'
    entries = {'a.dat': MD5_A} if files is None else files
    registry.write_text(''.join(f'{n} {h}\n' for n, h in entries.items()))
    return Dataset(key=key, name=key, zenodo=zenodo, dataverse=pin, registry_path=registry)


def _file(name, kind, value, size, restricted=False):
    return {
        'restricted': restricted,
        'dataFile': {
            'filename': name,
            'checksum': {'type': kind, 'value': value},
            'filesize': size,
        },
    }


class FakeClient:
    """Answers the dataset read in the DataverseNL shape, or raises an error."""

    base_url = 'https://dataverse.example'

    def __init__(
        self, state='RELEASED', files=None, error=None, source=SOURCE, extra=None, note=None
    ):
        self.error = error
        listed = [_file('a.dat', 'SHA-1', 'f' * 40, 10)] if files is None else files
        note = note or f'Mirror of Zenodo deposit {source}.'
        description = [{'dsDescriptionValue': {'value': note}}]
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
    upper = _dataset(tmp_path, files={'a.dat': 'MD5:' + 'A' * 32})
    assert pin_problem(upper, md5, sizes=lambda doi: pytest.fail('no lookup')) is None
    for note in (f'mirror of zenodo deposit {SOURCE}', f'Mirror of Zenodo\n deposit {SOURCE}'):
        assert pin_problem(_dataset(tmp_path), FakeClient(note=note), sizes=_sizes) is None


def test_the_note_written_by_mirror_passes_the_source_check(tmp_path):
    """The description a mirror is created with (record text, then the source note) names its
    own Zenodo DOI and no other."""
    citation = zenodo_record_to_citation(
        {'doi': SOURCE, 'metadata': {'title': 't', 'description': 'Cites 10.5281/zenodo.2.'}},
        contact_name='c',
        contact_email='c@x',
        subject='Other',
    )
    fields = citation['datasetVersion']['metadataBlocks']['citation']['fields']
    client = FakeClient()
    client.body['data']['latestVersion']['metadataBlocks']['citation']['fields'] = fields
    assert pin_problem(_dataset(tmp_path), client, sizes=_sizes) is None
    other = _dataset(tmp_path, zenodo='10.5281/zenodo.2')
    assert 'does not name Zenodo 10.5281/zenodo.2' in pin_problem(other, client, sizes=_sizes)


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
                    {
                        'typeName': 'dsDescription',
                        'value': [{'dsDescriptionValue': {'value': 'Cites 10.5281/zenodo.1.'}}],
                    },
                    {'typeName': 'otherId', 'value': 'Mirror of Zenodo deposit 10.5281/zenodo.1.'},
                ],
            ),
            'does not name Zenodo 10.5281/zenodo.1',
        ),
        (FakeClient(files=[]), 'a.dat missing'),
        (FakeClient(files=[_file('a.dat', 'MD5', 'b' * 32, 10)]), 'a.dat checksum differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 11)]), 'a.dat size differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, None)]), 'a.dat size differs'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 10)] * 2), 'holds a.dat twice'),
        (FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 10, True)]), 'a.dat restricted'),
        (FakeClient(files=[_file('a.dat', 'MD5', 'a' * 32, 10, True)]), 'a.dat restricted'),
        (FakeClient(error=DataverseError('404 not found')), 'cannot read doi:10.34894/ABCDEF'),
    ],
)
def test_a_mirror_that_cannot_serve_the_registry_is_named(tmp_path, client, reason):
    """Each failure mode returns its own reason; a longer Zenodo id or the DOI in another
    citation field does not pass as the source."""
    why = pin_problem(_dataset(tmp_path), client, sizes=_sizes)
    assert why is not None
    assert reason in why


def test_a_duplicate_outside_the_registry_is_allowed(tmp_path):
    """Only a registry file held twice is ambiguous; a repeated extra file is not."""
    extra = _file('c.dat', 'SHA-1', 'e' * 40, 3)
    client = FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, 10), extra, extra])
    assert pin_problem(_dataset(tmp_path), client, sizes=_sizes) is None


def test_a_size_missing_on_both_sides_is_not_a_match(tmp_path):
    """A mirror file without a size does not match a Zenodo record that lacks the file."""
    client = FakeClient(files=[_file('a.dat', 'SHA-1', 'f' * 40, None)])
    assert 'size differs' in pin_problem(_dataset(tmp_path), client, sizes=lambda doi: {})


def test_an_empty_registry_is_not_a_pass(tmp_path):
    """A registry with no file names checks nothing, so it is a reason, not a pass."""
    why = pin_problem(_dataset(tmp_path, files={}), FakeClient(), sizes=_sizes)
    assert why == 'the registry lists no files'


def _http(status):
    response = requests.Response()
    response.status_code, response.url = status, 'https://zenodo.org/api/records/1'
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc
    raise AssertionError(f'status {status} raised nothing')


CERT = requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))


@pytest.mark.parametrize(
    'error',
    [
        requests.ConnectionError('down'),
        requests.Timeout('down'),
        requests.exceptions.ChunkedEncodingError('down'),
        requests.exceptions.JSONDecodeError('down', '<html>', 0),
        ConnectionError('down'),
        TimeoutError('down'),
        *(_http(status) for status in (408, 429, 500, 501, 503, 599)),
    ],
)
def test_a_transient_zenodo_error_is_unreachable(tmp_path, error):
    """A lost connection, a timeout, a cut or non-JSON body, or an HTTP 408, 429 or 5xx from
    Zenodo raises Unreachable instead of reporting the pin as wrong."""
    with pytest.raises(Unreachable) as raised:
        pin_problem(_dataset(tmp_path), FakeClient(), sizes=Mock(side_effect=error))
    assert str(raised.value) == f'Zenodo 10.5281/zenodo.1 file sizes: {error}'


def test_a_transient_dataverse_error_is_unreachable(tmp_path):
    """A bot-check page or gateway error from the Dataverse server raises Unreachable."""
    with pytest.raises(Unreachable) as raised:
        pin_problem(_dataset(tmp_path), FakeClient(error=DataverseRetryableError('bot check')))
    assert str(raised.value) == 'doi:10.34894/ABCDEF: bot check'


@pytest.mark.parametrize(
    'error',
    [
        *(_http(status) for status in (404, 407, 409, 499)),
        ValueError('concept DOI'),
        CERT,
        requests.exceptions.InvalidURL('bad url'),
        requests.exceptions.TooManyRedirects('redirect loop'),
        FileNotFoundError('no file'),
        KeyError('size'),
    ],
)
def test_a_permanent_zenodo_error_is_a_reason(tmp_path, error):
    """A 4xx other than 408 and 429, a concept DOI, a certificate failure, a bad URL, a
    redirect loop or a malformed record makes the pin wrong."""
    why = pin_problem(_dataset(tmp_path), FakeClient(), sizes=Mock(side_effect=error))
    assert why == f'Zenodo 10.5281/zenodo.1 file sizes: {error}'


@pytest.fixture
def waits(monkeypatch):
    """Record the waits between certificate retries instead of sleeping."""
    slept = []
    monkeypatch.setattr(pins.time, 'sleep', slept.append)
    return slept


def _cert_dataverse_error():
    error = DataverseError('Dataverse GET failed: certificate verify failed')
    error.__cause__ = CERT
    return error


class FlakyClient(FakeClient):
    """Fails the dataset read with a certificate error ``failures`` times, then answers."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def _request(self, method, path, **kwargs):
        if len(self.calls) < self.failures:
            self.calls.append((method, path, kwargs.get('params')))
            raise _cert_dataverse_error()
        return super()._request(method, path, **kwargs)


@pytest.mark.parametrize('failures', [1, 2])
def test_a_certificate_error_that_clears_is_served_with_a_warning(
    tmp_path, monkeypatch, waits, failures
):
    """A certificate failure on the Zenodo or the Dataverse read is retried 30 s later; a pin
    whose read then succeeds is served and the summary warns about the recovery."""
    sizes = Mock(side_effect=[CERT] * failures + [_sizes(SOURCE)])
    _patch(monkeypatch, [_dataset(tmp_path, 'g.one')])
    monkeypatch.setattr(pins, 'zenodo_sizes', sizes)
    report = check_mirrors('https://example.org')
    note = f'certificate error, recovered after {failures + 1} attempts'
    assert report.passed == ['g.one'] and report.exit_code == 0
    assert report.warnings == {'g.one': f'Zenodo {SOURCE}: {note}'}
    assert f'WARNING g.one: Zenodo {SOURCE}: {note}' in report.summary().splitlines()
    assert waits == [30.0] * failures and sizes.call_count == failures + 1

    waits.clear()
    _patch(monkeypatch, [_dataset(tmp_path, 'g.two')], client=FlakyClient(failures))
    report = check_mirrors('https://example.org')
    assert report.passed == ['g.two']
    assert report.warnings == {'g.two': f'doi:10.34894/ABCDEF: {note}'}
    assert waits == [30.0] * failures


def test_a_certificate_error_on_every_attempt_fails_the_pin(tmp_path, monkeypatch, waits):
    """Three certificate failures in a row make the pin wrong (exit 1), on either server."""
    sizes = Mock(side_effect=CERT)
    _patch(monkeypatch, [_dataset(tmp_path, 'g.one')])
    monkeypatch.setattr(pins, 'zenodo_sizes', sizes)
    report = check_mirrors('https://example.org')
    assert report.failed == {'g.one': f'Zenodo {SOURCE} file sizes: {CERT}'}
    assert report.exit_code == 1 and not report.warnings
    assert waits == [30.0, 30.0] and sizes.call_count == 3

    waits.clear()
    client = FlakyClient(failures=3)
    _patch(monkeypatch, [_dataset(tmp_path, 'g.two')], client=client)
    report = check_mirrors('https://example.org')
    assert report.failed['g.two'].startswith('cannot read doi:10.34894/ABCDEF:')
    assert len(client.calls) == 3 and waits == [30.0, 30.0]


def test_only_a_certificate_error_is_retried(tmp_path, monkeypatch, waits):
    """A lost connection is not retried within the run; it is reported as unreachable."""
    sizes = Mock(side_effect=ConnectionError('down'))
    _patch(monkeypatch, [_dataset(tmp_path, 'g.one')])
    monkeypatch.setattr(pins, 'zenodo_sizes', sizes)
    assert sorted(check_mirrors('https://example.org').unreachable) == ['g.one']
    assert waits == [] and sizes.call_count == 1


def _served(status, text, content_type='application/json'):
    """Return a stub for requests.request that answers every call with one response."""
    response = requests.Response()
    response.status_code, response._content = status, text.encode()
    response.headers['Content-Type'] = content_type
    return lambda method, url, **kwargs: response


@pytest.mark.parametrize(
    ('status', 'text', 'content_type'),
    [
        (408, 'timeout', 'text/plain'),
        (429, 'slow down', 'text/plain'),
        (500, 'error', 'text/plain'),
        (501, 'error', 'text/plain'),
        (520, 'error', 'text/plain'),
        (200, '<html>maintenance</html>', 'text/html'),
    ],
)
def test_a_transient_dataverse_answer_is_unreachable(
    tmp_path, monkeypatch, status, text, content_type
):
    """An HTTP 408, 429 or 5xx, or an HTML page in place of the API answer, from the real
    client is an outage (exit 3), as on the Zenodo side."""
    monkeypatch.setattr('fwl_io.mirror.requests.request', _served(status, text, content_type))
    client = DataverseClient('https://dataverse.example', token='')
    with pytest.raises(Unreachable, match=f'doi:10.34894/ABCDEF: .*{status}'):
        pin_problem(_dataset(tmp_path), client, sizes=_sizes)


def test_a_missing_dataverse_dataset_is_a_wrong_pin(tmp_path, monkeypatch):
    """A 404 from the real client makes the pin wrong (exit 1)."""
    monkeypatch.setattr('fwl_io.mirror.requests.request', _served(404, '{"status": "ERROR"}'))
    client = DataverseClient('https://dataverse.example', token='')
    why = pin_problem(_dataset(tmp_path), client, sizes=_sizes)
    assert why.startswith('cannot read doi:10.34894/ABCDEF:') and '404' in why


def test_a_doi_prefix_on_either_pin_is_accepted(tmp_path):
    """A pin written as doi:<doi> reads the same dataset and Zenodo record as a bare one."""
    ds = _dataset(tmp_path, pin='doi:10.34894/ABCDEF', zenodo=f'doi:{SOURCE}')
    client, sizes = FakeClient(), Mock(side_effect=_sizes)
    assert pin_problem(ds, client, sizes=sizes) is None
    assert client.calls[0][2] == {'persistentId': 'doi:10.34894/ABCDEF'}
    sizes.assert_called_once_with(SOURCE)


def test_a_host_that_failed_its_certificate_is_not_retried_again(tmp_path, monkeypatch, waits):
    """Once a host failed every certificate attempt, later reads of it fail at once and the
    summary says they were not retried; this holds for Zenodo and for the Dataverse host."""
    datasets = [_dataset(tmp_path, f'g.{n}', zenodo=f'10.5281/zenodo.{n}') for n in (1, 2, 3)]
    notes = ' '.join(f'Mirror of Zenodo deposit 10.5281/zenodo.{n}.' for n in (1, 2, 3))
    _patch(monkeypatch, datasets, client=FakeClient(note=notes))
    sizes = Mock(side_effect=CERT)
    monkeypatch.setattr(pins, 'zenodo_sizes', sizes)
    report = check_mirrors('https://example.org')
    assert sorted(report.failed) == ['g.1', 'g.2', 'g.3']
    assert waits == [30.0, 30.0] and sizes.call_count == 5
    assert sorted(report.warnings) == ['g.2', 'g.3']
    assert 'not retried since zenodo.org failed 3 attempts' in report.warnings['g.3']

    waits.clear()
    client = FlakyClient(failures=99)
    _patch(monkeypatch, datasets, client=client)
    report = check_mirrors('https://example.org')
    assert sorted(report.failed) == ['g.1', 'g.2', 'g.3'] and len(client.calls) == 5
    assert waits == [30.0, 30.0]
    assert 'not retried since dataverse.example failed' in report.warnings['g.2']


def test_an_outage_after_a_found_problem_still_reports_it(tmp_path):
    """A checksum mismatch found before the Zenodo read is reported when that read fails."""
    files = {'a.dat': MD5_A, 'b.dat': MD5_B}
    client = FakeClient(
        files=[_file('a.dat', 'SHA-1', 'f' * 40, 10), _file('b.dat', 'MD5', 'c' * 32, 7)]
    )
    down = Mock(side_effect=ConnectionError('down'))
    why = pin_problem(_dataset(tmp_path, files=files), client, sizes=down)
    assert why == 'b.dat checksum differs; Zenodo 10.5281/zenodo.1 file sizes: down'


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
    assert sorted(report.failed) == ['group.bad', 'group.broken']
    assert report.manifest_errors == {'other': 'cannot load'}
    assert report.failed['group.broken'].startswith('FileNotFoundError')
    assert not report.ok
    assert report.summary().splitlines()[0] == 'FAIL manifest other: cannot load'
    assert report.summary().splitlines()[-2:] == [
        'pins served by their mirror: 1, wrong: 2, not checked (could not be read): 0, '
        'datasets without a pin: 1, manifests that failed to load: 1',
        'unpinned group.loose',
    ]


def test_an_unexpected_error_fails_one_dataset_only(tmp_path, monkeypatch):
    """An error of any type in one dataset is a FAIL for it, and the run goes on."""
    broken = _dataset(tmp_path, 'g.one', pin='10.34894/BROKEN')

    class Broken(FakeClient):
        def _request(self, method, path, **kwargs):
            if kwargs['params']['persistentId'].endswith('BROKEN'):
                raise AttributeError('no body')
            return super()._request(method, path, **kwargs)

    _patch(monkeypatch, [broken, _dataset(tmp_path, 'g.two')], client=Broken())
    report = check_mirrors('https://example.org')
    assert report.failed == {'g.one': 'AttributeError: no body'}
    assert report.passed == ['g.two']


def test_check_mirrors_reads_each_zenodo_record_once(tmp_path, monkeypatch):
    """Datasets that share a Zenodo record (one deposit split in several datasets) read its
    file sizes once, also when the read fails."""
    asked = []
    _patch(monkeypatch, [_dataset(tmp_path, 'g.one'), _dataset(tmp_path, 'g.two')])
    monkeypatch.setattr(pins, 'zenodo_sizes', lambda doi: asked.append(doi) or _sizes(doi))
    assert check_mirrors('https://example.org').passed == ['g.one', 'g.two']
    assert asked == [SOURCE]
    down = Mock(side_effect=ConnectionError('down'))
    monkeypatch.setattr(pins, 'zenodo_sizes', down)
    assert sorted(check_mirrors('https://example.org').unreachable) == ['g.one', 'g.two']
    down.assert_called_once_with(SOURCE)


@pytest.mark.parametrize(
    ('client', 'found', 'code', 'text'),
    [
        (FakeClient(), True, 0, 'pins served by their mirror: 1'),
        (FakeClient(state='DRAFT'), True, 1, 'FAIL group.good'),
        (FakeClient(error=DataverseRetryableError('504')), True, 3, 'UNREACHABLE group.good'),
        (FakeClient(), False, 1, 'pins served by their mirror: 0'),
    ],
)
def test_check_mirrors_command_exits_by_the_verdict(
    tmp_path, monkeypatch, capsys, client, found, code, text
):
    """Exit 0 when every pin is served, 1 for a wrong pin or nothing to check, 3 when only
    the server could not be read; the given URL reaches the client with no token."""
    seen = []
    _patch(
        monkeypatch, [_dataset(tmp_path, 'group.good')] if found else [], client=client, seen=seen
    )
    assert main(['check-mirrors', '--dataverse-url', 'https://x.example']) == code
    assert text in capsys.readouterr().out
    assert seen == [('https://x.example', '')]


@pytest.mark.parametrize(
    ('report', 'code'),
    [
        (MirrorReport(passed=['a'], failed={'b': 'x'}, unreachable={'c': 'y'}), 1),
        (MirrorReport(passed=['a'], manifest_errors={'m': 'x'}, unreachable={'c': 'y'}), 1),
        (MirrorReport(unreachable={'c': 'y'}), 3),
        (MirrorReport(unpinned=['d']), 1),
        (MirrorReport(passed=['a'], unpinned=['d']), 0),
    ],
)
def test_exit_code_puts_a_wrong_pin_before_an_outage(report, code):
    """A wrong pin or manifest error wins over an unreachable server, and 3 stays clear of
    the 2 that argparse uses for a usage error."""
    assert report.exit_code == code
    assert report.ok == (code == 0)


def test_an_empty_token_sends_no_api_key(monkeypatch):
    """A read with an empty token sends no X-Dataverse-key header; a set token is sent."""
    sent = []

    response = SimpleNamespace(
        ok=True,
        status_code=200,
        headers={'content-type': 'application/json'},
        text='{}',
        content=b'{}',
        json=lambda: {},
    )

    def request(method, url, **kwargs):
        sent.append(kwargs.get('headers'))
        return response

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
    """Every pin is a DataverseNL DOI, and datasets that share a Zenodo record share one pin."""
    datasets = load_manifest(shared_manifest_path())
    pinned = [ds for ds in datasets if ds.dataverse]
    assert pinned
    assert all(re.fullmatch(r'10\.34894/[A-Z0-9]{6}', ds.dataverse) for ds in pinned)
    by_record: dict[str, set[str]] = {}
    for ds in pinned:
        by_record.setdefault(ds.zenodo, set()).add(ds.dataverse)
    assert all(len(dois) == 1 for dois in by_record.values())


def test_spectral_file_entries_match_their_key():
    """Each spectral-file entry names its set as its key does and holds <Set>.sf and <Set>.sf_k."""
    datasets = load_manifest(shared_manifest_path())
    spectral = [ds for ds in datasets if ds.key.startswith('atmos_clim.spectral_files.')]
    assert len(spectral) == 17
    for ds in spectral:
        set_name, bands = ds.key.split('.')[2:]
        stem = set_name.capitalize()
        assert ds.name == f'SOCRATES spectral files {stem}, {bands} bands'
        names = set(ds.registry())
        if set_name == 'legacy':
            assert {'sp_b318_HITRAN_a16_no_spectrum', 'sp_b318_HITRAN_a16_no_spectrum_k'} <= names
        else:
            assert {f'{stem}.sf', f'{stem}.sf_k'} <= names


def test_only_phoenix_is_unpinned_in_the_shared_manifest():
    """Every shared dataset has a DataverseNL mirror except the PHOENIX spectra."""
    datasets = load_manifest(shared_manifest_path())
    assert {ds.key for ds in datasets if not ds.dataverse} == {'star.spectra.phoenix'}
    assert len(datasets) > 1


def test_seager_and_zeng_are_shared_beside_the_proteus_copies(tmp_path):
    """The shared Seager and Zeng datasets sit at their own locations, so the collision rule
    keeps both providers while PROTEUS still declares its copies, and drops both on a clash."""
    from fwl_io.manifest import _drop_conflicting_datasets

    shared = {ds.key: ds for ds in load_manifest(shared_manifest_path())}
    seager, zeng = shared['interior.eos.seager_2007'], shared['interior.mass_radius.zeng_2019']
    assert (seager.subdir, seager.zenodo, seager.dataverse) == (
        'interior/eos/seager_2007',
        '10.5281/zenodo.15727998',
        '10.34894/QZZGHW',
    )
    assert (zeng.subdir, zeng.zenodo, zeng.dataverse) == (
        'interior/mass_radius/zeng_2019',
        '10.5281/zenodo.15727899',
        '10.34894/ZGZA6I',
    )
    proteus = [
        _dataset(tmp_path, key='interior_struct.eos.seager_2007'),
        _dataset(tmp_path, key='observe.mass_radius.zeng_2019'),
    ]
    found, errors = {'fwl-io': list(shared.values()), 'proteus': proteus}, {}
    _drop_conflicting_datasets(found, errors)
    assert (sorted(found), errors) == (['fwl-io', 'proteus'], {})
    clash = {'fwl-io': list(shared.values()), 'proteus': [_dataset(tmp_path, key=seager.key)]}
    _drop_conflicting_datasets(clash, errors)
    assert clash == {} and sorted(errors) == ['fwl-io', 'proteus']
