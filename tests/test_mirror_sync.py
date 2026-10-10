"""Tests for ``fwl-io mirror-sync`` and ``fwl-io mirror-pin``: which record gets a draft,
the check of a draft against its Zenodo record, and the pin written into a manifest, all
against fakes."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fwl_io import mirror_sync as sync
from fwl_io.cli import main
from fwl_io.manifest import Dataset, ErrorKind, ProviderError, _Discovery
from fwl_io.mirror import DataverseError, source_record
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


def _search(second_page):
    """Answer a search of 150 datasets in pages; the last item of a page carries the note."""

    def search(params):
        start = params['start']
        count = 100 if start == 0 else second_page
        items = [
            {'global_id': f'doi:10.34894/N{start + i}', 'description': 'other data'}
            for i in range(count - 1)
        ]
        note = f'x {NOTE}' if start == 0 else NOTE.upper()
        items.append(
            {'global_id': f'doi:10.34894/P{start}', 'versionState': 'RELEASED', 'description': note}
        )
        return {'data': {'items': items, 'total_count': 150}}

    return search


def test_collection_mirrors_reads_every_page_and_groups_by_record():
    """Datasets are grouped by the record their description names, across pages of the
    step size; a dataset without the note is left out."""
    client = FakeClient({'/api/search': _search(50)})
    assert collection_mirrors(client, 'Coll') == {
        '55': [('RELEASED', 'doi:10.34894/P0'), ('RELEASED', 'doi:10.34894/P100')]
    }
    query = {'q': '*', 'subtree': 'Coll', 'type': 'dataset', 'per_page': 100}
    assert client.calls == [('/api/search', {**query, 'start': start}) for start in (0, 100)]


def test_a_listing_that_misses_a_dataset_is_an_error():
    """Fewer items than the count the server gives raise: a missed dataset would get a
    second draft."""
    client = FakeClient({'/api/search': _search(49)})
    with pytest.raises(DataverseError, match='listing of Coll: read 149 of 150 datasets'):
        collection_mirrors(client, 'Coll')


def _serve(monkeypatch, client_files, payloads=None, status=200):
    """Serve a two-file Zenodo record and the Dataverse downloads of ``payloads`` by id."""
    registry = {n: 'md5:' + hashlib.md5(d).hexdigest() for n, d in DATA.items()}
    monkeypatch.setattr(sync, 'fetch_zenodo_registry', lambda doi, api_base: registry)
    payloads = payloads or {
        entry['id']: DATA.get(name, b'') for name, entry in client_files.items()
    }

    class _Reply:
        is_redirect = False

        def __init__(self, url):
            self.status_code, self.data = status, payloads.get(int(url.rsplit('/', 1)[1]), b'')

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, size):
            return [self.data[:2], self.data[2:]]

    asked = []
    monkeypatch.setattr(
        sync.requests, 'get', lambda url, **kwargs: asked.append((url, kwargs)) or _Reply(url)
    )
    return asked


def _draft(**version):
    files = {'a.dat': _entry('a.dat', DATA['a.dat'], 1), 'b.dat': _entry('b.dat', DATA['b.dat'], 2)}
    return FakeClient({'/versions/:draft': {'data': _version(**version)}}, files)


def test_a_draft_with_the_bytes_of_its_record_is_verified(monkeypatch):
    """A draft with a license, the source note and both files byte for byte has no problem,
    also for the selected file alone."""
    client = _draft()
    asked = _serve(monkeypatch, client.files)
    assert verify_draft(client, PID, '10.5281/zenodo.55') == []
    options = {'headers': client._headers, 'allow_redirects': False, 'stream': True, 'timeout': 5}
    assert asked == [(f'https://dv.example/api/access/datafile/{i}', options) for i in (1, 2)]
    del client.files['b.dat']
    assert verify_draft(client, PID, '10.5281/zenodo.55', files=['a.dat']) == []


def _server(reply, seen):
    """Start a local server that records the token header of each GET, then calls ``reply``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get('X-Dataverse-key'))
            reply(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_token_stays_on_the_dataverse_server_when_a_download_redirects(monkeypatch):
    """Dataverse answers a download with a redirect to a storage server: the file is read
    there and verified, and only the Dataverse server sees the token."""
    at_dataverse, at_store = [], []

    def stored(handler):
        data = {'/1': DATA['a.dat'], '/2': DATA['b.dat']}[handler.path]
        handler.send_response(200)
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    store = _server(stored, at_store)

    def redirect(handler):
        handler.send_response(303)
        file_id = handler.path.rsplit('/', 1)[1]
        handler.send_header('Location', f'http://127.0.0.1:{store.server_port}/{file_id}')
        handler.send_header('Content-Length', '0')
        handler.end_headers()

    dataverse = _server(redirect, at_dataverse)
    try:
        client = _draft()
        client.base_url = f'http://127.0.0.1:{dataverse.server_port}'
        registry = {n: 'md5:' + hashlib.md5(d).hexdigest() for n, d in DATA.items()}
        monkeypatch.setattr(sync, 'fetch_zenodo_registry', lambda doi, api_base: registry)
        assert verify_draft(client, PID, '10.5281/zenodo.55') == []
    finally:
        for server in (dataverse, store):
            server.shutdown()
            server.server_close()
    assert at_dataverse == ['t', 't'], 'the draft download needs the token'
    assert at_store == [None, None], 'the token left the Dataverse server'


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
    made, _plan.clients, _plan.verified = [], [], []
    monkeypatch.setattr(sync, 'DataverseClient', lambda *args: _plan.clients.append(args))
    monkeypatch.setattr(sync, '_discover_all', lambda: _Discovery({'m': datasets}, errors or {}))
    monkeypatch.setattr(sync, 'mirror_status', lambda found: status)
    monkeypatch.setattr(sync, 'collection_mirrors', lambda client, collection: mirrors)
    monkeypatch.setattr(
        sync, 'verify_draft', lambda *args: _plan.verified.append(args) or list(problems)
    )
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
        _ds('g.b2', 2, pin='doi:10.34894/TWO'),
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
    assert _plan.verified == [(None, PID, '10.5281/zenodo.5', ['x.dat', 'y.dat'])]
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
    """An error during the check of a new draft is reported with the draft's id, exit 1,
    also when a Zenodo record could not be read (exit 3 is for a run with no failure)."""
    datasets = [_ds('g.new', 8)]
    status = StatusReport(unpinned={'g.new': datasets[0].zenodo}, unreadable={'g.other': 'x'})
    _plan(monkeypatch, datasets, status, {})
    monkeypatch.setattr(sync, 'verify_draft', lambda *args: 1 / 0)
    lines, code = _run()
    assert code == 1 and lines[-1] == (
        f'CREATED draft {PID} for 10.5281/zenodo.8 (g.new): '
        'NOT verified: the check failed: ZeroDivisionError: division by zero'
    )


def _raise(exc):
    raise exc


def test_a_listing_or_a_creation_that_fails_keeps_the_lines_before_it(monkeypatch):
    """A failed listing still gives the status report; a failed creation still gives the
    lines before it and the message, which names a draft that is kept. Both exit 1."""
    datasets = [_ds('g.new', 8)]
    status = StatusReport(unpinned={'g.new': datasets[0].zenodo})
    made = _plan(monkeypatch, datasets, status, {})
    monkeypatch.setattr(sync, 'collection_mirrors', lambda *args: _raise(DataverseError('down')))
    assert _run() == ([status.summary(), 'FAIL listing of Coll: DataverseError: down'], 1)
    assert made == []
    _plan(monkeypatch, datasets, status, {})
    kept = RuntimeError(f'{PID} is kept as a draft')
    monkeypatch.setattr(sync, 'mirror_to_dataverse', lambda doi, **kwargs: _raise(kept))
    assert _run() == (
        [
            status.summary(),
            'records without a mirror: 1; this run takes Zenodo 8',
            f'FAIL draft for 10.5281/zenodo.8: RuntimeError: {PID} is kept as a draft',
        ],
        1,
    )
    assert _plan.verified == []


def test_nothing_is_created_without_a_record_to_mirror_or_with_a_manifest_left_out(monkeypatch):
    """All pinned: no draft and exit 0. A manifest left out: exit 1 before any request."""
    ok = [_ds('g.ok', 1, pin='10.34894/ONE')]
    made = _plan(monkeypatch, ok, StatusReport(ok=['g.ok']), {})
    lines, code = _run()
    assert (lines[-1], code, made) == ('no draft to create', 0, [])
    _plan(monkeypatch, ok, StatusReport(unreadable={'g.ok': 'x'}), {})
    assert _run()[1] == 3, 'a record that could not be read is the only problem'
    datasets = [_ds('g.down', 1), _ds('g.new', 2)]
    unpinned = {ds.key: ds.zenodo for ds in datasets}
    _plan(monkeypatch, datasets, StatusReport(unpinned=unpinned, unreadable={'g.down': 'x'}), {})
    assert _run(dry_run=True)[1] == 3
    broken = {'p': ProviderError(ErrorKind.CONFLICT, 'claims a taken location')}
    made = _plan(monkeypatch, [], StatusReport(), {}, errors=broken)
    monkeypatch.setattr(sync, 'DataverseClient', lambda *a: pytest.fail('a client was made'))
    assert _run() == (['FAIL p: MANIFEST NOT USED, claims a taken location'], 1) and made == []


MANIFEST = """\
[g.first]
name = "First"
zenodo = "10.5281/zenodo.55"
required_by = ["demo"]

[g.second]
zenodo = "10.5281/zenodo.56"
dataverse = "10.34894/OLDPIN"
"""
TWO_OF_ONE_RECORD = MANIFEST + '\n[g.third]\nzenodo = "10.5281/zenodo.55"\n'


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('x MIRROR  of zenodo\tdeposit 10.5281/ZENODO.55. y', '55'),
        ('Mirror of Zenodo deposit 10.34894/ABCDEF.', None),
        ('other data', None),
        ('see 10.5281/zenodo.55 for details', None),
    ],
)
def test_the_record_of_a_source_note_is_read_in_any_case_or_spacing(text, expected):
    """The note names a Zenodo record in any case or spacing; another DOI or no note gives
    None."""
    assert source_record(text) == expected


def test_a_pin_keeps_the_bytes_of_the_rest_of_the_manifest(tmp_path):
    """CRLF line endings, a character outside ASCII and a last line without a newline stay
    as they are around the new pin, and after a pin that cannot be written."""
    manifest = tmp_path / 'manifest.toml'
    start = '[g.a]\r\nname = "Ångström"\r\nzenodo = "10.5281/zenodo.6"\r\n\r\n[g.b]\r\n'
    start = (start + 'zenodo = "10.5281/zenodo.7"').encode()
    manifest.write_bytes(start)
    write_pin(manifest, 'g.b', '10.34894/BPIN')
    pinned = start + b'\r\ndataverse = "10.34894/BPIN"\r\n'
    assert manifest.read_bytes() == pinned
    with pytest.raises(ValueError, match='left unchanged'):
        write_pin(manifest, 'g.a', 'no doi')
    assert manifest.read_bytes() == pinned


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


def test_a_pin_that_would_change_other_content_is_refused(tmp_path):
    """A line inside a multi-line string that looks like an old pin is not removed, and a
    manifest that does not parse is not edited: the edit is refused, the file unchanged."""
    manifest = tmp_path / 'manifest.toml'
    text = '[g.a]\nname = """Spectra\ndataverse mirror notes\n"""\nzenodo = "10.5281/zenodo.6"\n'
    manifest.write_text(text)
    with pytest.raises(ValueError, match='would change more than its pin'):
        write_pin(manifest, 'g.a', '10.34894/APIN')
    assert manifest.read_text() == text
    broken = '[g.a]\nzenodo = "10.5281/zenodo.6"\nname = \n'
    manifest.write_text(broken)
    with pytest.raises(ValueError, match='would change more than its pin'):
        write_pin(manifest, 'g.a', '10.34894/APIN')
    assert manifest.read_text() == broken, 'a manifest that does not parse is not edited'


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


def _pin(monkeypatch, tmp_path, version, others=(), problem=None, errors=None, text=MANIFEST):
    """Run mirror-pin on a manifest file; ``problem`` is the pin problem of ``g.first``,
    or of each key of a dict."""
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(text)
    problems = problem if isinstance(problem, dict) else dict.fromkeys(('g.first',), problem)
    found = {'other': list(others)}
    monkeypatch.setattr(sync, '_discover_all', lambda: _Discovery(found, errors or {}))
    checked = []
    monkeypatch.setattr(
        sync,
        'pin_problem',
        lambda ds, client: checked.append((ds.key, ds.dataverse)) or problems.get(ds.key),
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
        'other.done: already pinned',
        'other.same is declared by another package; add there: dataverse = "10.34894/NEWPIN"',
        'PINNED g.first in manifest.toml',
    ]
    assert 'dataverse = "10.34894/NEWPIN"' in manifest.read_text()
    assert sorted(checked) == [
        ('g.first', '10.34894/NEWPIN'),
        ('other.done', '10.34894/NEWPIN'),
        ('other.same', '10.34894/NEWPIN'),
    ]


def test_a_replaced_pin_names_the_old_one_and_a_manifest_left_out_stops_the_write(
    monkeypatch, tmp_path
):
    """A dataset pinned to another mirror gets the new pin with the old one named. With a
    manifest left out of discovery, whose datasets were not seen, nothing is written."""
    version = _version('RELEASED', note=NOTE.replace('.55', '.56'))
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version)
    assert (lines, code) == (['PINNED g.second in manifest.toml (was 10.34894/OLDPIN)'], 0)
    assert manifest.read_text() == MANIFEST.replace('OLDPIN', 'NEWPIN')
    errors = {
        'p': ProviderError(ErrorKind.LOAD_FAILURE, 'cannot load'),
        'c': ProviderError(ErrorKind.CONFLICT, 'claims a taken location'),
    }
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version, errors=errors)
    assert code == 1 and lines == [
        'FAIL c: MANIFEST NOT USED, claims a taken location',
        'FAIL p: MANIFEST FAILED TO LOAD, cannot load',
        'NOT PINNED g.second: nothing is written after a FAIL',
    ]
    assert manifest.read_text() == MANIFEST
    version = _version('RELEASED', note=NOTE.replace('.55', '.77'))
    _, _, (lines, code) = _pin(monkeypatch, tmp_path, version, errors=errors)
    assert lines[-1] == 'no installed dataset pins Zenodo 77' and len(lines) == 3 and code == 1


def test_no_pin_is_written_when_one_dataset_of_the_record_fails(monkeypatch, tmp_path):
    """With two datasets of one record, a mirror that does not serve the second, or a second
    pin that cannot be written, leaves the file as it was and says which pins wait."""
    version = _version('RELEASED')
    manifest, checked, (lines, code) = _pin(
        monkeypatch, tmp_path, version, problem={'g.third': 'a.dat missing'}, text=TWO_OF_ONE_RECORD
    )
    assert (
        len(checked) == 2
        and code == 1
        and lines
        == [
            'FAIL g.third: a.dat missing',
            'NOT PINNED g.first: nothing is written after a FAIL',
        ]
    )
    assert manifest.read_text() == TWO_OF_ONE_RECORD
    write = sync.write_pin
    monkeypatch.setattr(
        sync,
        'write_pin',
        lambda path, key, pin: (
            write(path, key, pin) if key == 'g.first' else _raise(ValueError('x'))
        ),
    )
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version, text=TWO_OF_ONE_RECORD)
    assert code == 1 and lines == [
        'FAIL x',
        'NOT PINNED g.first: nothing is written after a FAIL',
        'NOT PINNED g.third: nothing is written after a FAIL',
    ]
    assert manifest.read_text() == TWO_OF_ONE_RECORD
    monkeypatch.setattr(sync, 'write_pin', write)
    manifest, _, (lines, code) = _pin(monkeypatch, tmp_path, version, text=TWO_OF_ONE_RECORD)
    assert code == 0 and [line.split()[:2] for line in lines] == [
        ['PINNED', 'g.first'],
        ['PINNED', 'g.third'],
    ]
    assert manifest.read_text().count('dataverse = "10.34894/NEWPIN"') == 2


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
    return the code, also that of a pin that failed."""
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
    monkeypatch.setattr(sync, 'mirror_pin', lambda pid, manifest: ([f'{pid} {manifest}'], 1))
    assert main(['mirror-pin', 'doi:10.34894/X', '--manifest', 'm.toml']) == 1
    assert capsys.readouterr().out == 'doi:10.34894/X m.toml\n'
