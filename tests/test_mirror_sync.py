"""Tests for ``fwl-io mirror-sync`` and ``fwl-io mirror-pin``: which record gets a draft,
the check of a draft against its Zenodo record, and the pin written into a manifest, all
against fakes."""

import hashlib

import pytest

from fwl_io import mirror_sync as sync
from fwl_io.cli import main
from fwl_io.manifest import Dataset, ErrorKind, ProviderError, _Discovery
from fwl_io.mirror_status import StatusReport
from fwl_io.mirror_sync import (
    collection_mirrors,
    mirror_pin,
    mirror_sync,
    verify_draft,
    write_pin,
)

pytestmark = pytest.mark.unit

PID = 'doi:10.34894/DRAFT1'
NOTE = 'Mirror of Zenodo deposit 10.5281/zenodo.55. Zenodo is the primary source.'
DATA = {'a.dat': b'AAA\n', 'b.dat': b'BBBB\n'}


def _ds(key, recid, pin=None, files=None):
    return Dataset(key=key, name=key, zenodo=f'10.5281/zenodo.{recid}', dataverse=pin, files=files)


def _entry(name, data, file_id, kind='SHA-1'):
    value = hashlib.new(kind.replace('-', '').lower(), data).hexdigest()
    return {'id': file_id, 'filename': name, 'checksum': {'type': kind, 'value': value}}


class FakeClient:
    """A Dataverse client that answers from a table of path suffixes and lists a draft."""

    base_url, token, timeout, _headers = 'https://dv.example', 't', 5, {'X-Dataverse-key': 't'}

    def __init__(self, replies=None, files=None):
        self.replies, self.files, self.calls = replies or {}, files or {}, []

    def _retry(self, call, what, done=None):
        return call()

    def _request(self, method, path, params=None):
        self.calls.append((path, params))
        reply = next(r for suffix, r in self.replies.items() if path.endswith(suffix))
        return reply(params) if callable(reply) else reply

    def _draft_files(self, persistent_id):
        return self.files


def _version(state='DRAFT', license_name='CC-BY-4.0', note=NOTE, files=()):
    fields = [{'typeName': 'dsDescription', 'value': [{'dsDescriptionValue': {'value': note}}]}]
    version = {'versionState': state, 'metadataBlocks': {'citation': {'fields': fields}}}
    if license_name:
        version['license'] = {'name': license_name}
    return {**version, 'files': list(files)}


def test_collection_mirrors_reads_every_page_and_groups_by_record():
    """Datasets are grouped by the record their description names, across pages; a dataset
    without the note is left out."""

    def search(params):
        items = [
            {'global_id': f'doi:10.34894/P{params["start"]}', 'versionState': 'RELEASED'},
            {'global_id': 'doi:10.34894/NONOTE', 'description': 'other data'},
        ]
        items[0]['description'] = f'x {NOTE}' if params['start'] == 0 else NOTE.upper()
        return {'data': {'items': items, 'total_count': 150}}

    client = FakeClient({'/api/search': search})
    assert collection_mirrors(client, 'Coll') == {
        '55': [('RELEASED', 'doi:10.34894/P0'), ('RELEASED', 'doi:10.34894/P100')]
    }
    assert [params['start'] for _, params in client.calls] == [0, 100]
    assert client.calls[0][1]['subtree'] == 'Coll'


def _serve(monkeypatch, client_files, payloads=None, status=200):
    """Serve a two-file Zenodo record and the Dataverse downloads of ``payloads`` by id."""
    record = {
        'files': [
            {'key': n, 'checksum': 'md5:' + hashlib.md5(d).hexdigest(), 'size': len(d)}
            for n, d in DATA.items()
        ]
    }
    monkeypatch.setattr(sync, 'fetch_zenodo_record', lambda doi, api_base: record)
    payloads = payloads or {
        entry['id']: DATA.get(name, b'') for name, entry in client_files.items()
    }

    class _Reply:
        def __init__(self, url):
            self.status_code, self.data = status, payloads.get(int(url.rsplit('/', 1)[1]), b'')

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, size):
            return [self.data[:2], self.data[2:]]

    monkeypatch.setattr(sync.requests, 'get', lambda url, **kwargs: _Reply(url))


def _draft(**version):
    files = {'a.dat': _entry('a.dat', DATA['a.dat'], 1), 'b.dat': _entry('b.dat', DATA['b.dat'], 2)}
    return FakeClient({'/versions/:draft': {'data': _version(**version)}}, files)


def test_a_draft_with_the_bytes_of_its_record_is_verified(monkeypatch):
    """A draft with a license, the source note and both files byte for byte has no problem,
    also for the selected file alone."""
    client = _draft()
    _serve(monkeypatch, client.files)
    assert verify_draft(client, PID, '10.5281/zenodo.55') == []
    del client.files['b.dat']
    assert verify_draft(client, PID, '10.5281/zenodo.55', files=['a.dat']) == []


@pytest.mark.parametrize(
    ('version', 'change', 'problem'),
    [
        ({'state': 'RELEASED'}, None, "state is 'RELEASED', not DRAFT"),
        ({'license_name': None}, None, 'no license'),
        ({'note': 'Mirror of Zenodo deposit 10.5281/zenodo.556.'}, None, 'does not name'),
        ({}, 'drop b', 'b.dat missing'),
        ({}, 'extra', 'z.dat is not in the record'),
        ({}, 'other bytes', 'a.dat differs from Zenodo (md5)'),
        ({}, 'other bytes', 'a.dat differs from the checksum Dataverse lists'),
        ({}, 'unknown type', 'a.dat differs from the checksum Dataverse lists'),
        ({}, 'http 403', 'a.dat cannot be downloaded (HTTP 403)'),
    ],
)
def test_a_draft_that_differs_from_its_record_is_named(monkeypatch, version, change, problem):
    """Each way a draft can differ from its record gives its own line."""
    client = _draft(**version)
    payloads = None
    if change == 'drop b':
        del client.files['b.dat']
    elif change == 'extra':
        client.files['z.dat'] = _entry('z.dat', b'Z', 9)
    elif change == 'other bytes':
        payloads = {1: b'BAD\n', 2: DATA['b.dat']}
    elif change == 'unknown type':
        client.files['a.dat']['checksum']['type'] = 'CRC-99'
    _serve(monkeypatch, client.files, payloads, status=403 if change == 'http 403' else 200)
    problems = verify_draft(client, PID, '10.5281/zenodo.55')
    assert any(problem in line for line in problems), problems
    if change is None:
        assert len(problems) == 1


def _plan(monkeypatch, datasets, status, mirrors, problems=(), errors=None):
    """Patch discovery, the status, the collection listing and the mirror; return the calls."""
    made, _plan.clients = [], []
    monkeypatch.setattr(sync, 'DataverseClient', lambda *args: _plan.clients.append(args))
    monkeypatch.setattr(sync, '_discover_all', lambda: _Discovery({'m': datasets}, errors or {}))
    monkeypatch.setattr(sync, 'mirror_status', lambda found: status)
    monkeypatch.setattr(sync, 'collection_mirrors', lambda client, collection: mirrors)
    monkeypatch.setattr(sync, 'verify_draft', lambda client, pid, doi, files: list(problems))
    monkeypatch.setattr(
        sync, 'mirror_to_dataverse', lambda doi, **kwargs: made.append((doi, kwargs)) or PID
    )
    return made


def _run(dry_run=False):
    return mirror_sync(
        'Coll',
        dataverse_url='https://dv.example',
        token='t',
        contact_name='P',
        contact_email='c@x',
        dry_run=dry_run,
    )


def test_only_a_record_with_no_dataset_and_no_pin_gets_a_draft(monkeypatch):
    """A stale dataset, a record another dataset pins, a record with a released dataset
    and one with a draft only get a line; the first record left gets the one draft, with
    the files every dataset of the record asks for, unpublished."""
    datasets = [
        _ds('g.a', 1),
        _ds('g.b', 2),
        _ds('g.b2', 2, pin='10.34894/TWO'),
        _ds('g.c', 3),
        _ds('g.d', 4),
        _ds('g.e', 5, files=('x.dat',)),
        _ds('g.e2', 5, files=('y.dat', 'x.dat')),
        _ds('g.f', 6),
    ]
    unpinned = {ds.key: ds.zenodo for ds in datasets if not ds.dataverse}
    status = StatusReport(unpinned=unpinned, stale={'g.a': 'newer'}, unreadable={'g.f': 'down'})
    mirrors = {
        '3': [('DRAFT', 'doi:10.34894/NEXT'), ('RELEASED', 'doi:10.34894/THREE')],
        '4': [('DRAFT', 'doi:10.34894/FOUR'), ('DEACCESSIONED', 'doi:10.34894/GONE')],
    }
    made = _plan(monkeypatch, datasets, status, mirrors)
    lines, code = _run()
    assert code == 3, 'a record could not be read, and nothing else failed'
    assert lines[1:] == [
        'SKIPPED g.a: pin the newest Zenodo version first',
        'PIN MISSING g.b: run fwl-io mirror-pin doi:10.34894/TWO',
        'PIN MISSING g.c: run fwl-io mirror-pin doi:10.34894/THREE',
        'WAITING g.d: the collection holds doi:10.34894/FOUR (DRAFT), '
        'doi:10.34894/GONE (DEACCESSIONED)',
        'SKIPPED g.f: its Zenodo record could not be read',
        'records without a mirror: 1; this run takes Zenodo 5',
        f'CREATED draft {PID} for 10.5281/zenodo.5 (g.e, g.e2): verified',
    ]
    assert _plan.clients == [('https://dv.example', 't')], 'the listing needs the token'
    assert made == [
        (
            '10.5281/zenodo.5',
            {
                'dataverse_url': 'https://dv.example',
                'collection': 'Coll',
                'token': 't',
                'contact_name': 'P',
                'contact_email': 'c@x',
                'publish': False,
                'files': ['x.dat', 'y.dat'],
            },
        )
    ]


def test_a_record_one_dataset_reads_whole_is_mirrored_whole(monkeypatch):
    """When a dataset of the record names no files, the draft holds the whole record."""
    datasets = [_ds('g.part', 7, files=('x.dat',)), _ds('g.whole', 7)]
    status = StatusReport(unpinned={ds.key: ds.zenodo for ds in datasets})
    made = _plan(monkeypatch, datasets, status, {})
    _run()
    assert made[0][1]['files'] is None


@pytest.mark.parametrize(
    ('dry_run', 'problems', 'last', 'code', 'created'),
    [
        (True, (), 'WOULD CREATE a draft for 10.5281/zenodo.8 (g.new)', 0, 0),
        (False, ('a.dat missing', 'no license'), 'NOT verified: a.dat missing; no license', 1, 1),
    ],
)
def test_a_dry_run_creates_nothing_and_a_failed_check_fails_the_run(
    monkeypatch, dry_run, problems, last, code, created
):
    """A dry run names the draft it would create; a draft that is not verified fails."""
    datasets = [_ds('g.new', 8)]
    status = StatusReport(unpinned={'g.new': datasets[0].zenodo})
    made = _plan(monkeypatch, datasets, status, {}, problems)
    lines, got = _run(dry_run)
    assert lines[-1].endswith(last) and got == code and len(made) == created


def test_a_check_that_fails_to_run_still_names_the_draft(monkeypatch):
    """An error during the check of a new draft is reported with the draft's id, exit 1."""
    datasets = [_ds('g.new', 8)]
    _plan(monkeypatch, datasets, StatusReport(unpinned={'g.new': datasets[0].zenodo}), {})
    monkeypatch.setattr(sync, 'verify_draft', lambda *args: 1 / 0)
    lines, code = _run()
    assert code == 1 and lines[-1] == (
        f'CREATED draft {PID} for 10.5281/zenodo.8 (g.new): '
        'NOT verified: the check failed: division by zero'
    )


def test_nothing_is_created_without_a_record_to_mirror_or_with_a_manifest_left_out(monkeypatch):
    """All pinned: no draft and exit 0. A manifest left out: exit 1 before any request."""
    made = _plan(monkeypatch, [_ds('g.ok', 1, pin='10.34894/ONE')], StatusReport(ok=['g.ok']), {})
    lines, code = _run()
    assert (lines[-1], code, made) == ('no draft to create', 0, [])
    broken = {'p': ProviderError(ErrorKind.CONFLICT, 'claims a taken location')}
    made = _plan(monkeypatch, [], StatusReport(), {}, errors=broken)
    monkeypatch.setattr(sync, 'DataverseClient', lambda *a: pytest.fail('a client was made'))
    assert _run() == (['p: manifest left out, claims a taken location'], 1) and made == []


MANIFEST = """\
[g.first]
name = "First"
zenodo = "10.5281/zenodo.55"
required_by = ["demo"]

[g.second]
zenodo = "10.5281/zenodo.56"
dataverse = "10.34894/OLDPIN"
"""


def test_the_pin_is_written_after_the_zenodo_line_of_its_table(tmp_path):
    """A new pin follows the zenodo line of its own table; a pin that exists is replaced;
    the other table is left as it is."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(MANIFEST)
    write_pin(manifest, 'g.first', '10.34894/NEWPIN')
    write_pin(manifest, 'g.second', '10.34894/SECOND')
    assert manifest.read_text() == MANIFEST.replace(
        'zenodo = "10.5281/zenodo.55"\n',
        'zenodo = "10.5281/zenodo.55"\ndataverse = "10.34894/NEWPIN"\n',
    ).replace('OLDPIN', 'SECOND')


@pytest.mark.parametrize(
    ('key', 'pin', 'why'),
    [
        ('g.absent', '10.34894/NEWPIN', 'no \\[g.absent\\] table'),
        ('g.first', 'no doi', 'left unchanged'),
    ],
)
def test_a_pin_that_cannot_be_written_leaves_the_manifest_as_it_was(tmp_path, key, pin, why):
    """An unknown table, or a pin the manifest does not load with, changes nothing."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(MANIFEST)
    with pytest.raises(ValueError, match=why):
        write_pin(manifest, key, pin)
    assert manifest.read_text() == MANIFEST


def _pin(monkeypatch, tmp_path, version, others=(), problem=None, errors=None):
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(MANIFEST)
    found = {'other': list(others)}
    monkeypatch.setattr(sync, '_discover_all', lambda: _Discovery(found, errors or {}))
    checked = []
    monkeypatch.setattr(
        sync, 'pin_problem', lambda ds, client: checked.append((ds.key, ds.dataverse)) or problem
    )
    client = FakeClient({'/api/datasets/:persistentId': {'data': {'latestVersion': version}}})
    return manifest, checked, mirror_pin('doi:10.34894/NEWPIN', manifest=manifest, client=client)


def test_a_published_mirror_is_pinned_in_every_dataset_of_its_record(monkeypatch, tmp_path):
    """The dataset of the manifest gets the pin written; one another package declares gets
    the line to add; each is checked with the new pin first."""
    others = [
        _ds('other.same', 55),
        _ds('other.done', 55, pin='doi:10.34894/NEWPIN'),
        _ds('o.x', 9),
    ]
    others.append(_ds('g.first', 9))  # an installed copy of a key the manifest file declares
    manifest, checked, (lines, code) = _pin(monkeypatch, tmp_path, _version('RELEASED'), others)
    assert code == 0 and lines == [
        'PINNED g.first in manifest.toml',
        'other.done: already pinned',
        'other.same is declared by another package; add there: dataverse = "10.34894/NEWPIN"',
    ]
    assert 'dataverse = "10.34894/NEWPIN"' in manifest.read_text()
    assert sorted(checked) == [
        ('g.first', '10.34894/NEWPIN'),
        ('other.done', '10.34894/NEWPIN'),
        ('other.same', '10.34894/NEWPIN'),
    ]


def test_a_replaced_pin_and_a_manifest_left_out_are_reported(monkeypatch, tmp_path):
    """A dataset pinned to another mirror gets the new pin with the old one named; a manifest
    left out of discovery is listed and fails the run, since its datasets were not seen."""
    version = _version('RELEASED', note=NOTE.replace('.55', '.56'))
    errors = {'p': ProviderError(ErrorKind.LOAD_FAILURE, 'cannot load')}
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version, errors=errors)
    assert code == 1 and lines == [
        'p: manifest left out, cannot load',
        'PINNED g.second in manifest.toml (was 10.34894/OLDPIN)',
    ]
    assert manifest.read_text() == MANIFEST.replace('OLDPIN', 'NEWPIN')


@pytest.mark.parametrize(
    ('version', 'problem', 'line'),
    [
        (_version('DRAFT'), None, 'doi:10.34894/NEWPIN is not published'),
        (_version('RELEASED', note='no note'), None, 'does not name a Zenodo record'),
        (_version('RELEASED', note=NOTE.replace('.55', '.77')), None, 'no installed dataset pins'),
        (_version('RELEASED'), 'a.dat missing', 'FAIL g.first: a.dat missing'),
    ],
)
def test_a_mirror_that_cannot_be_pinned_writes_nothing(
    monkeypatch, tmp_path, version, problem, line
):
    """A draft, a dataset without the source note, a record no dataset pins and a mirror
    that does not serve the registry each fail with their reason and leave the manifest."""
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version, problem=problem)
    assert code == 1 and line in lines[0] and manifest.read_text() == MANIFEST


def test_the_commands_print_the_report_and_exit_by_it(monkeypatch, capsys):
    """mirror-sync needs the token and passes its options on; both print their lines and
    return the code."""
    seen = {}
    monkeypatch.delenv('DATAVERSE_TOKEN', raising=False)
    assert main(['mirror-sync', '--collection', 'C']) == 1
    assert 'set DATAVERSE_TOKEN' in capsys.readouterr().err
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')
    monkeypatch.setattr(
        sync, 'mirror_sync', lambda c, **kw: seen.update(kw, c=c) or (['x', 'y'], 1)
    )
    assert main(['mirror-sync', '--collection', 'C', '--dry-run', '--contact-email', 'c@x']) == 1
    assert capsys.readouterr().out == 'x\ny\n'
    assert (seen['c'], seen['token'], seen['dry_run'], seen['contact_email']) == (
        'C',
        'tok',
        True,
        'c@x',
    )
    monkeypatch.setattr(sync, 'mirror_pin', lambda pid, manifest: ([f'{pid} {manifest}'], 0))
    assert main(['mirror-pin', 'doi:10.34894/X', '--manifest', 'm.toml']) == 0
    assert capsys.readouterr().out == 'doi:10.34894/X m.toml\n'
