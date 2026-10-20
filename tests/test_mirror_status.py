"""Tests for ``fwl-io mirror-status``: datasets without a Dataverse pin or with a newer
Zenodo version, against a fake Zenodo."""

import pytest
import requests

from fwl_io import mirror_status as status
from fwl_io.cli import main
from fwl_io.manifest import Dataset, ErrorKind, ProviderError, _Discovery
from fwl_io.mirror_status import StatusReport, latest_record_id, mirror_status

pytestmark = pytest.mark.unit


def _ds(key, recid, pin='10.34894/ABCDEF'):
    return Dataset(key=key, name=key, zenodo=f'10.5281/zenodo.{recid}', dataverse=pin)


def test_datasets_are_sorted_by_what_their_mirror_needs():
    """A pinned current record is in order; a missing pin, a newer version (also together,
    and with a pin), a record outside the communities and a failed Zenodo read are named;
    each record is read once, and the community only for a dataset without a pin."""
    asked, members = [], []

    def community(doi):
        members.append(doi)
        if doi.endswith(('.7', '.4')):
            raise requests.ConnectionError('no answer')
        return not doi.endswith('.6')

    def latest(recid):
        asked.append(recid)
        if recid == '4':
            raise requests.ConnectionError('down')
        return {'1': '1', '2': '2', '3': '30', '5': '50', '6': '6', '7': '7'}[recid]

    datasets = [
        _ds('g.ok', 1),
        _ds('g.ok2', 1),
        _ds('g.unpinned', 2, pin=None),
        _ds('g.both', 3, pin=None),
        _ds('g.stale', 5),
        _ds('g.down', 4),
        _ds('g.out', 6, pin=None),
        _ds('g.out2', 6, pin=None),
        _ds('g.unknown', 7, pin=None),
        _ds('g.down2', 4, pin=None),
    ]
    report = mirror_status(datasets, latest=latest, community=community)
    assert report.ok == ['g.ok', 'g.ok2']
    assert sorted(report.unpinned) == [
        'g.both',
        'g.down2',
        'g.out',
        'g.out2',
        'g.unknown',
        'g.unpinned',
    ]
    assert report.outside == dict.fromkeys(
        ('g.out', 'g.out2'), 'Zenodo 6 is in no community of the framework'
    )
    assert members == [f'10.5281/zenodo.{n}' for n in (2, 3, 6, 7, 4)]
    assert report.stale == {
        'g.both': 'pins Zenodo 3, newest version is 30',
        'g.stale': 'pins Zenodo 5, newest version is 50',
    }
    assert report.unreadable == {
        'g.down': 'Zenodo 4: down',
        'g.unknown': 'Zenodo 7: no answer',
        'g.down2': 'Zenodo 4: down',
    }, 'the reason of the first failed read stays'
    assert asked == ['1', '2', '3', '5', '4', '6', '7']
    assert 'OUTSIDE g.out: Zenodo 6 is in no community of the framework' in report.summary()
    assert report.summary().splitlines()[-1] == (
        'in order: 2, without a pin: 6, with a newer Zenodo version: 2, '
        'outside the communities: 2, not checked (Zenodo could not be read): 3'
    )
    assert report.exit_code == 5


@pytest.mark.parametrize(
    ('report', 'code'),
    [
        (StatusReport(ok=['a']), 0),
        (StatusReport(ok=['a'], unreadable={'b': 'x'}), 3),
        (StatusReport(unpinned={'a': 'z'}, unreadable={'b': 'x'}), 5),
        (StatusReport(stale={'a': 'z'}), 5),
        (
            StatusReport(
                stale={'a': 'z'}, manifest_errors={'m': ProviderError(ErrorKind.CONFLICT, 'x')}
            ),
            1,
        ),
    ],
)
def test_the_exit_code_puts_a_manifest_error_before_work_before_an_outage(report, code):
    """1 for a manifest error, then 5 for a dataset that needs work, then 3 for a record
    that could not be read; 0 when all are in order."""
    assert report.exit_code == code


def test_the_command_prints_the_rows_and_exits_by_the_verdict(monkeypatch, capsys):
    """The command reads every installed manifest and prints one line per dataset to act on."""
    found = {'m': [_ds('g.ok', 1), _ds('g.new', 2, pin=None)]}
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery(found, {}))
    monkeypatch.setattr(status, 'latest_record_id', lambda recid: recid)
    monkeypatch.setattr(status, 'accepted', lambda doi: True)
    assert main(['mirror-status']) == 5
    out = capsys.readouterr().out.splitlines()
    assert out == [
        'UNPINNED g.new: 10.5281/zenodo.2',
        'in order: 1, without a pin: 1, with a newer Zenodo version: 0, '
        'outside the communities: 0, '
        'not checked (Zenodo could not be read): 0',
    ]


def test_a_manifest_left_out_is_reported_with_its_reason(monkeypatch):
    """A manifest that fails to load and one left out for a conflict are listed as
    check-mirrors lists them, and fail the run."""
    broken = {
        'other': ProviderError(ErrorKind.CONFLICT, 'claims a taken location'),
        'broken': ProviderError(ErrorKind.LOAD_FAILURE, 'cannot load'),
    }
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery({}, broken))
    report = mirror_status(latest=lambda recid: recid)
    assert report.manifest_errors == broken and report.exit_code == 1
    assert report.summary().splitlines()[:2] == [
        'FAIL broken: MANIFEST FAILED TO LOAD, cannot load',
        'FAIL other: MANIFEST NOT USED, claims a taken location',
    ]


def test_the_count_line_names_each_group_with_its_own_count():
    """Five groups of five sizes: each count stands after its own words."""

    def group(letter, size):
        return {f'{letter}{i}': 'x' for i in range(size)}

    report = StatusReport(
        ok=['a'],
        unpinned=group('u', 2),
        stale=group('s', 3),
        outside=group('o', 4),
        unreadable=group('r', 5),
    )
    assert report.summary().splitlines()[-1] == (
        'in order: 1, without a pin: 2, with a newer Zenodo version: 3, '
        'outside the communities: 4, not checked (Zenodo could not be read): 5'
    )


def test_a_record_is_accepted_when_its_zenodo_record_lists_a_community(monkeypatch):
    """The default community test reads the record of the DOI and looks at its communities."""
    records = {
        '10.5281/zenodo.1': {'metadata': {'communities': [{'id': 'paleos'}]}},
        '10.5281/zenodo.2': {'metadata': {}},
    }
    monkeypatch.setattr(status, 'fetch_zenodo_record', records.__getitem__)
    assert status.accepted('10.5281/zenodo.1') and not status.accepted('10.5281/zenodo.2')


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


def test_newer_versions_reads_each_record_of_the_prefix_once():
    """Only datasets under the prefix are checked; three datasets of one record cost one read,
    and a record with a newer version is stale while the current one is in order."""
    asked = []

    def latest(recid):
        asked.append(recid)
        return {'22776069': '22776069', '19680050': '29680050'}[recid]

    datasets = [
        _ds('interior.eos.paleos_iron', 22776069),
        _ds('interior.eos.paleos_h2o', 22776069),
        _ds('interior.eos.paleos_mgsio3_unified', 22776069),
        _ds('interior.eos.paleos_mgsio3', 19680050),
        _ds('interior.eos.seager_2007', 15727878),
    ]
    report = status.newer_versions('interior.eos.paleos', datasets, latest=latest)
    assert sorted(asked) == ['19680050', '22776069']
    assert report.stale == {
        'interior.eos.paleos_mgsio3': 'pins Zenodo 19680050, newest version is 29680050'
    }
    assert sorted(report.ok) == [
        'interior.eos.paleos_h2o',
        'interior.eos.paleos_iron',
        'interior.eos.paleos_mgsio3_unified',
    ]
    assert report.exit_code == 5


def test_newer_versions_reports_an_unread_record_without_a_retry():
    """A Zenodo record that cannot be read is named once, not retried, and exits 3."""
    asked = []

    def latest(recid):
        asked.append(recid)
        raise requests.ConnectionError('down')

    datasets = [_ds('interior.eos.paleos_iron', 1), _ds('interior.eos.paleos_h2o', 1)]
    report = status.newer_versions('interior.eos.paleos', datasets, latest=latest)
    assert asked == ['1']
    assert set(report.unreadable) == {'interior.eos.paleos_iron', 'interior.eos.paleos_h2o'}
    assert report.exit_code == 3 and not report.stale


def test_newer_versions_with_every_pin_current_exits_zero():
    """All pins at their newest version: in order, exit 0."""
    datasets = [_ds('a.one', 5), _ds('a.two', 6)]
    report = status.newer_versions('a.', datasets, latest=lambda recid: recid)
    assert report.ok == ['a.one', 'a.two']
    assert report.exit_code == 0


def test_newer_versions_keeps_a_manifest_left_out(monkeypatch):
    """A manifest that fails to load fails the check, as in mirror-status."""
    broken = {'broken': ProviderError(ErrorKind.LOAD_FAILURE, 'cannot load')}
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery({}, broken))
    report = status.newer_versions('', latest=lambda recid: recid)
    assert report.manifest_errors == broken
    assert report.exit_code == 1


def test_the_latest_command_fails_on_a_newer_version(monkeypatch, capsys):
    """fwl-io latest prints the stale pin and exits 5 so a scheduled run fails."""
    found = {'m': [_ds('interior.eos.paleos_iron', 1), _ds('other.key', 2)]}
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery(found, {}))
    monkeypatch.setattr(status, 'latest_record_id', lambda recid: '9')
    assert main(['latest', '--prefix', 'interior.eos.paleos']) == 5
    out = capsys.readouterr().out
    assert out.splitlines() == [
        'STALE interior.eos.paleos_iron: pins Zenodo 1, newest version is 9',
        'newest version pinned: 0, with a newer Zenodo version: 1, '
        'not checked (Zenodo could not be read): 0',
    ]
    assert 'other.key' not in out


def test_the_latest_command_refuses_a_prefix_that_selects_nothing(monkeypatch, capsys):
    """A prefix no dataset key starts with exits 1 without a Zenodo read, so a typo never passes."""
    found = {'m': [_ds('interior.eos.paleos_iron', 1)]}
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery(found, {}))
    monkeypatch.setattr(status, 'latest_record_id', lambda recid: pytest.fail('read Zenodo'))
    assert main(['latest', '--prefix', 'interior.eos.paleo_']) == 1
    assert 'no dataset key starts with interior.eos.paleo_' in capsys.readouterr().err


def test_the_paleos_prefix_selects_the_four_shipped_paleos_datasets():
    """The prefix the scheduled check uses selects every PALEOS dataset of the shipped manifest."""
    from fwl_io.manifest import load_manifest, shared_manifest_path

    keys = [ds.key for ds in load_manifest(shared_manifest_path())]
    paleos = [k for k in keys if k.startswith('interior.eos.paleos')]
    assert sorted(paleos) == [
        'interior.eos.paleos_h2o',
        'interior.eos.paleos_iron',
        'interior.eos.paleos_mgsio3',
        'interior.eos.paleos_mgsio3_unified',
    ]
    assert all('paleos' not in k for k in keys if k not in paleos)


def _http(code):
    """An HTTPError with status ``code``."""
    return requests.HTTPError(response=type('R', (), {'status_code': code})())


def _latest_cli(monkeypatch, datasets, newest):
    """Run fwl-io latest over ``datasets``, Zenodo answered by ``newest`` (an id or an error)."""
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery({'m': datasets}, {}))

    def latest(recid):
        if isinstance(newest.get(recid), Exception):
            raise newest[recid]
        return newest[recid]

    monkeypatch.setattr(status, 'latest_record_id', latest)
    return main(['latest', '--prefix', 'p.'])


def test_the_latest_count_line_counts_each_group(monkeypatch, capsys):
    """2 current, 3 stale and 4 unread datasets give their own count each, and exit 5."""
    ds = [_ds(f'p.{i}', i) for i in range(1, 10)]
    newest = {str(i): str(i) for i in (1, 2)}
    newest.update({str(i): str(i + 100) for i in (3, 4, 5)})
    newest.update({str(i): requests.ConnectionError('down') for i in (6, 7, 8, 9)})
    assert _latest_cli(monkeypatch, ds, newest) == 5
    assert capsys.readouterr().out.splitlines()[-1] == (
        'newest version pinned: 2, with a newer Zenodo version: 3, '
        'not checked (Zenodo could not be read): 4'
    )


def test_latest_with_one_record_unread_warns_with_exit_3(monkeypatch, capsys):
    """One record read and current, one not read: UNREADABLE line, exit 3."""
    ds = [_ds('p.a', 1), _ds('p.b', 2)]
    assert _latest_cli(monkeypatch, ds, {'1': '1', '2': requests.ConnectionError('down')}) == 3
    out = capsys.readouterr().out
    assert 'UNREADABLE p.b: Zenodo 2: down' in out and 'STALE' not in out


def test_latest_with_no_record_read_fails_with_exit_4(monkeypatch, capsys):
    """Zenodo read for no record, by passing faults only: the check saw nothing, exit 4."""
    ds = [_ds('p.a', 1), _ds('p.b', 2)]
    err = requests.ConnectionError('down')
    assert _latest_cli(monkeypatch, ds, {'1': err, '2': err}) == 4
    assert capsys.readouterr().out.count('UNREADABLE') == 2


def test_latest_with_every_pin_current_prints_only_the_count(monkeypatch, capsys):
    """The daily steady state: one count line, exit 0."""
    assert _latest_cli(monkeypatch, [_ds('p.a', 1), _ds('p.b', 2)], {'1': '1', '2': '2'}) == 0
    assert capsys.readouterr().out.splitlines() == [
        'newest version pinned: 2, with a newer Zenodo version: 0, '
        'not checked (Zenodo could not be read): 0'
    ]


def test_latest_reports_a_manifest_left_out(monkeypatch, capsys):
    """A manifest that fails to load prints its FAIL line and exits 1."""
    broken = {'broken': ProviderError(ErrorKind.LOAD_FAILURE, 'cannot load')}
    monkeypatch.setattr(status, '_discover_all', lambda: _Discovery({'m': [_ds('p.a', 1)]}, broken))
    monkeypatch.setattr(status, 'latest_record_id', lambda recid: recid)
    assert main(['latest', '--prefix', 'p.']) == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        'FAIL broken: MANIFEST FAILED TO LOAD, cannot load'
    )


def test_each_declaration_of_a_key_is_read(monkeypatch):
    """Two declarations of one key with two pins are both read: no pin is hidden."""
    asked = []

    def latest(recid):
        asked.append(recid)
        return recid

    status.newer_versions('p.', [_ds('p.a', 1), _ds('p.a', 2)], latest=latest)
    assert sorted(asked) == ['1', '2']


@pytest.mark.parametrize(
    'error',
    [
        _http(400),
        _http(404),
        _http(410),
        _http(499),
        status.RecordWithoutId('no id in the reply'),
    ],
    ids=['400', '404', '410', '499', 'no id'],
)
def test_a_record_that_is_gone_fails_with_exit_4(monkeypatch, capsys, error):
    """A pinned record Zenodo no longer serves, or a reply of another shape, is no passing fault:
    exit 4, also when the other record is read."""
    ds = [_ds('p.a', 1), _ds('p.b', 2)]
    assert _latest_cli(monkeypatch, ds, {'1': '1', '2': error}) == 4
    assert 'GONE p.b' in capsys.readouterr().out


@pytest.mark.parametrize(
    'error',
    [
        requests.ConnectionError('down'),
        requests.Timeout('slow'),
        _http(399),
        _http(401),
        _http(403),
        _http(408),
        _http(429),
        _http(500),
        _http(501),
        requests.HTTPError('no status'),
        requests.exceptions.ChunkedEncodingError('cut'),
        requests.exceptions.ContentDecodingError('bad gzip'),
        requests.TooManyRedirects('loop'),
        requests.exceptions.RetryError('retries'),
        requests.exceptions.JSONDecodeError('html page', 'doc', 0),
        OSError('socket'),
    ],
    ids=[
        'connection',
        'timeout',
        '399',
        '401',
        '403',
        '408',
        '429',
        '500',
        '501',
        'no status',
        'cut body',
        'bad encoding',
        'redirect loop',
        'retries',
        'not json',
        'os error',
    ],
)
def test_a_passing_fault_on_one_record_warns_with_exit_3(monkeypatch, capsys, error):
    """A connection fault, a timeout, a 5xx or a rate limit on one record warns: exit 3."""
    ds = [_ds('p.a', 1), _ds('p.b', 2)]
    assert _latest_cli(monkeypatch, ds, {'1': '1', '2': error}) == 3
    assert 'UNREADABLE p.b' in capsys.readouterr().out


class _Reply:
    """A requests reply with a status and a JSON body or a body that is no JSON."""

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def test_a_reply_without_an_id_is_a_record_without_id(monkeypatch):
    """A 200 reply without 'id' raises RecordWithoutId; a reply that is no JSON raises the
    JSON error, which is a passing fault."""
    monkeypatch.setattr(status.requests, 'get', lambda url, timeout: _Reply({'other': 1}))
    with pytest.raises(status.RecordWithoutId):
        latest_record_id('3')
    bad = requests.exceptions.JSONDecodeError('html', 'doc', 0)
    monkeypatch.setattr(status.requests, 'get', lambda url, timeout: _Reply(bad))
    with pytest.raises(requests.exceptions.JSONDecodeError):
        latest_record_id('3')


@pytest.mark.parametrize('body', [['id'], 'id', 7, None], ids=['list', 'string', 'int', 'null'])
def test_a_reply_that_is_no_object_is_a_record_without_id(monkeypatch, body):
    """A JSON reply that is no object, even one holding the text 'id', is a record without id."""
    monkeypatch.setattr(status.requests, 'get', lambda url, timeout: _Reply(body))
    with pytest.raises(Exception) as info:
        latest_record_id('3')
    assert type(info.value) is status.RecordWithoutId


def test_the_prefix_matches_only_the_start_of_a_key(monkeypatch, capsys):
    """A key that holds the prefix after its start is not checked."""
    ds = [_ds('p.a', 1), _ds('x.p.b', 2)]
    assert _latest_cli(monkeypatch, ds, {'1': '1', '2': '9'}) == 0
    assert 'x.p.b' not in capsys.readouterr().out


def test_a_4xx_status_on_an_error_other_than_httperror_is_a_passing_fault(monkeypatch, capsys):
    """Only an HTTPError carries a status that makes a record gone; another error that holds a
    404 reply warns with exit 3."""
    error = requests.ConnectionError(response=type('R', (), {'status_code': 404})())
    ds = [_ds('p.a', 1), _ds('p.b', 2)]
    assert _latest_cli(monkeypatch, ds, {'1': '1', '2': error}) == 3
    assert 'UNREADABLE p.b' in capsys.readouterr().out


@pytest.mark.parametrize(
    'groups, code',
    [
        ({'manifest_errors': {'m': 'x'}, 'stale': {'a': 'x'}}, 1),
        ({'stale': {'a': 'x'}, 'gone': {'b': 'x'}}, 5),
        ({'ok': ['a'], 'gone': {'b': 'x'}}, 4),
        ({'unreadable': {'b': 'x'}}, 4),
        ({'ok': ['a'], 'unreadable': {'b': 'x'}}, 3),
        ({'ok': ['a']}, 0),
    ],
    ids=[
        'manifest over stale',
        'stale over gone',
        'gone',
        'none read',
        'one unread',
        'all current',
    ],
)
def test_the_latest_exit_code_follows_its_precedence(groups, code):
    """1 before 5 before 4 before 3 before 0, and no record read is 4."""
    assert status.latest_exit_code(StatusReport(**groups)) == code
    assert StatusReport(**groups).exit_code == (code if code != 4 or 'gone' in groups else 3)


def test_the_report_of_mirror_status_shows_a_gone_record():
    """A report with a gone record exits 4 and lists it, counted with the records not checked."""
    report = StatusReport(ok=['a'], gone={'g': 'Zenodo 7: 410'}, unreadable={'r': 'x'})
    assert report.exit_code == 4
    lines = report.summary().splitlines()
    assert 'GONE g: Zenodo 7: 410' in lines
    assert lines[-1].endswith('not checked (Zenodo could not be read): 2')


def test_the_latest_count_line_counts_gone_records_as_not_checked(monkeypatch, capsys):
    """One current, one stale, one gone and one unread: the count line shows each."""
    ds = [_ds('p.a', 1), _ds('p.b', 2), _ds('p.c', 3), _ds('p.d', 4)]
    newest = {'1': '1', '2': '20', '3': _http(410), '4': requests.ConnectionError('down')}
    assert _latest_cli(monkeypatch, ds, newest) == 5
    assert capsys.readouterr().out.splitlines()[-1] == (
        'newest version pinned: 1, with a newer Zenodo version: 1, '
        'not checked (Zenodo could not be read): 2'
    )


def test_two_declarations_of_a_key_are_named_by_their_record():
    """A key declared twice is reported per declaration, named with its record, so neither
    reason is lost and the counts are declarations."""
    report = status.newer_versions(
        'p.', [_ds('p.a', 1), _ds('p.a', 2)], latest=lambda r: {'1': '1', '2': '20'}[r]
    )
    assert report.ok == ['p.a (Zenodo 1)']
    assert list(report.stale) == ['p.a (Zenodo 2)']
