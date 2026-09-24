"""Tests for the Zenodo-to-Dataverse mirror.

The Zenodo side is exercised against the local ``http_server`` fixture (record
JSON plus the actual files, checksum-verified by the real fetcher). The
Dataverse side runs against a mock native-API server that records every
request, so the create/upload/publish orchestration and the request shapes are
asserted without touching a real Dataverse installation.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pooch
import pytest
import urllib3
from requests import exceptions as requests_exceptions

from fwl_io.mirror import (
    DataverseClient,
    DataverseError,
    DataversePublishUnconfirmed,
    mirror_to_dataverse,
    publish_existing_dataverse_draft,
    zenodo_record_to_citation,
)

pytestmark = pytest.mark.integration


def _serve_zenodo_record(root, recid, files):
    """Publish a Zenodo record JSON plus its files; return (record, registry)."""
    registry = {}
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        registry[name] = 'md5:' + pooch.file_hash(str(root / name), alg='md5')
    record = {
        'id': int(recid),
        'conceptrecid': str(int(recid) - 1),
        'doi': f'10.5281/zenodo.{recid}',
        'metadata': {
            'title': 'Demo tracks',
            'creators': [
                {'name': 'Doe, Jane', 'affiliation': 'Example University'},
                {'name': 'Roe, Richard'},
            ],
            'description': 'A demo dataset.',
        },
        'files': [{'key': n, 'checksum': h} for n, h in registry.items()],
    }
    api_dir = root / 'api' / 'records'
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / str(recid)).write_text(json.dumps(record))
    return record, registry


class _DataverseHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    fail_on_create: bool = False  # set by a test to force a create-time server rejection
    fail_on_add: bool = False  # set by a test to force an upload failure
    fail_on_publish: bool = False  # set by a test to force a publish failure
    fail_on_delete: bool = False  # set by a test to force a rollback failure
    omit_persistent_id: bool = False  # set by a test to drop the create response id
    # (method, path suffix) -> replies served before the normal one: 'challenge' or a status
    script: dict = {}
    # (method, path suffix) -> replies served after the normal handling ran
    script_after: dict = {}
    draft_files: list = []  # files the fake draft holds, as the listing reports them
    released: bool = False
    deleted: bool = False

    def log_message(self, *args):  # noqa: D102 -- silence request logging
        pass

    def _record(self, method):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        self.calls.append(
            {
                'method': method,
                'path': parsed.path,
                'query': parse_qs(parsed.query),
                'key': self.headers.get('X-Dataverse-key'),
                'content_type': self.headers.get('Content-Type', ''),
                'body': body,
            }
        )
        return parsed

    def _scripted(self, method, path, table):
        for (m, suffix), replies in table.items():
            if m == method and path.endswith(suffix) and replies:
                reply = replies.pop(0)
                if isinstance(reply, str):
                    # 'challenge' or 'proxy', with an optional status: 'challenge 403'
                    kind, _, status = reply.partition(' ')
                    page = b'<!doctype html><html><head><title>Not Found</title></head></html>'
                    if kind == 'challenge':
                        page = b'<!doctype html><html><head><title>Oh noes!</title>'
                        page += b'<link href="/.within.website/x/xess/xess.min.css"></head></html>'
                    self.send_response(int(status or 200))
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(page)
                else:
                    self._reply(reply, {'status': 'ERROR', 'message': 'gateway'})
                return True
        return False

    def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        parsed = self._record('GET')
        if self._scripted('GET', parsed.path, self.script):
            return
        if parsed.path.endswith('/versions/:draft/files'):
            self._reply(200, {'status': 'OK', 'data': [{'dataFile': f} for f in self.draft_files]})
        elif self.deleted:
            self._reply(404, {'status': 'ERROR', 'message': 'not found'})
        else:
            state = 'RELEASED' if self.released else 'DRAFT'
            self._reply(200, {'status': 'OK', 'data': {'latestVersion': {'versionState': state}}})

    def _reply(self, status, payload):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        parsed = self._record('POST')
        if self._scripted('POST', parsed.path, self.script):
            return
        if parsed.path.endswith('/add') and self._scripted('POST', '/add', self.script_after):
            # The upload reached the draft, but the response was lost.
            self.draft_files.append(self._uploaded_file())
            return
        if parsed.path.endswith('/actions/:publish') and self.script_after.get(
            ('POST', '/actions/:publish')
        ):
            _DataverseHandler.released = True
            self._scripted('POST', '/actions/:publish', self.script_after)
            return
        if parsed.path.endswith('/datasets'):
            if self.fail_on_create:
                # Mimic a real Dataverse citation-validation rejection (e.g. an
                # unknown subject), which the live server returns as a 400.
                self._reply(
                    400,
                    {'status': 'ERROR', 'message': "Value 'X' does not exist in type 'subject'"},
                )
                return
            data = {} if self.omit_persistent_id else {'persistentId': 'doi:10.34894/DEMO01'}
            self._reply(200, {'status': 'OK', 'data': data})
        elif parsed.path.endswith('/add'):
            if self.fail_on_add:
                self._reply(400, {'status': 'ERROR', 'message': 'bad field'})
            else:
                self.draft_files.append(self._uploaded_file())
                self._reply(200, {'status': 'OK', 'data': {'files': [{'label': 'ok'}]}})
        elif parsed.path.endswith('/actions/:publish'):
            if self.fail_on_publish:
                self._reply(400, {'status': 'ERROR', 'message': 'publish rejected'})
                return
            _DataverseHandler.released = True
            self._reply(200, {'status': 'OK', 'data': {'id': 7}})
        else:
            self.send_response(404)
            self.end_headers()

    def _uploaded_file(self):
        body = self.calls[-1]['body']
        name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
        payload = body.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0]
        checksum = {'type': 'MD5', 'value': hashlib.md5(payload).hexdigest()}
        return {'filename': name, 'filesize': len(payload), 'checksum': checksum}

    def do_DELETE(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        parsed = self._record('DELETE')
        if self._scripted('DELETE', parsed.path, self.script):
            return
        if self._scripted('DELETE', parsed.path, self.script_after):
            _DataverseHandler.deleted = True
            return
        if self.fail_on_delete:
            self._reply(500, {'status': 'ERROR', 'message': 'delete failed'})
        else:
            _DataverseHandler.deleted = True
            self._reply(200, {'status': 'OK', 'data': {'message': 'deleted'}})


@pytest.fixture()
def dataverse_server():
    """Run a mock Dataverse native-API server; yield (base_url, calls)."""
    _DataverseHandler.calls = []
    _DataverseHandler.fail_on_create = False
    _DataverseHandler.fail_on_add = False
    _DataverseHandler.fail_on_publish = False
    _DataverseHandler.fail_on_delete = False
    _DataverseHandler.omit_persistent_id = False
    _DataverseHandler.script = {}
    _DataverseHandler.script_after = {}
    _DataverseHandler.draft_files = []
    _DataverseHandler.released = False
    _DataverseHandler.deleted = False
    server = ThreadingHTTPServer(('127.0.0.1', 0), _DataverseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f'http://{host}:{port}', _DataverseHandler.calls
    server.shutdown()
    thread.join(timeout=5)


def _mirror(http_server, dataverse_server, **overrides):
    base_url, root = http_server
    dv_url, calls = dataverse_server
    _serve_zenodo_record(root, 55, {'a.dat': b'AAA\n', 'b.dat': b'BBBB\n'})
    kwargs = dict(
        dataverse_url=dv_url,
        collection='Proteus_Fr',
        token='secret-token',
        contact_name='PROTEUS',
        contact_email='contact@example.org',
        api_base=f'{base_url}api/records',
        base_urls=[base_url],
    )
    kwargs.update(overrides)
    result = mirror_to_dataverse('10.5281/zenodo.55', **kwargs)
    return result, calls


def test_full_mirror_creates_uploads_and_publishes(http_server, dataverse_server):
    """A published mirror creates the dataset, uploads every file, then publishes."""
    result, calls = _mirror(http_server, dataverse_server)

    assert result == 'doi:10.34894/DEMO01'
    paths = [c['path'] for c in calls]
    # The dataset is created in the requested collection.
    assert '/api/dataverses/Proteus_Fr/datasets' in paths
    # Both files are uploaded (one add call each), then the dataset is published.
    assert paths.count('/api/datasets/:persistentId/add') == 2
    assert paths.count('/api/datasets/:persistentId/actions/:publish') == 1
    # Create precedes uploads which precede publish (order is load-bearing:
    # publishing before the files are attached would ship an empty deposit).
    assert paths.index('/api/dataverses/Proteus_Fr/datasets') < paths.index(
        '/api/datasets/:persistentId/add'
    )
    # The publish is the last write; a state check confirms it.
    assert paths.index('/api/datasets/:persistentId/actions/:publish') == len(paths) - 2
    assert calls[-1]['method'] == 'GET' and paths[-1] == '/api/datasets/:persistentId'
    # A clean run does not roll back: no draft is deleted.
    assert not any(c['method'] == 'DELETE' for c in calls)


def test_create_receives_the_mapped_zenodo_citation(http_server, dataverse_server):
    """The metadata built from the Zenodo record actually reaches the create call."""
    _, calls = _mirror(http_server, dataverse_server)
    create = next(c for c in calls if c['path'].endswith('/datasets'))
    body = json.loads(create['body'])
    fields = {
        f['typeName']: f['value']
        for f in body['datasetVersion']['metadataBlocks']['citation']['fields']
    }
    # The wired-through metadata carries the Zenodo title and authors, not a
    # placeholder or empty body (a regression passing {} would fail here).
    assert fields['title'] == 'Demo tracks'
    assert fields['author'][0]['authorName']['value'] == 'Doe, Jane'


def test_every_request_carries_the_token(http_server, dataverse_server):
    """The API token authenticates every Dataverse write, not just the first."""
    _, calls = _mirror(http_server, dataverse_server)
    assert calls, 'expected Dataverse requests'
    assert all(c['key'] == 'secret-token' for c in calls)


def test_upload_disables_tabular_ingest_and_sends_the_real_bytes(http_server, dataverse_server):
    """Each upload disables ingest and carries the exact file bytes and name."""
    _, calls = _mirror(http_server, dataverse_server)
    adds = [c for c in calls if c['path'].endswith('/add')]
    assert len(adds) == 2
    for add in adds:
        # noVarDetect=true keeps the mirror byte-identical (no tabular conversion).
        assert add['query'].get('noVarDetect') == ['true']
        # The persistent id targets the just-created dataset.
        assert add['query'].get('persistentId') == ['doi:10.34894/DEMO01']
        assert add['content_type'].startswith('multipart/form-data')
    # Byte-identity is the mirror's purpose: the actual file payloads and their
    # names must appear in the multipart bodies, not merely a non-empty body.
    bodies = b''.join(a['body'] for a in adds)
    assert b'AAA\n' in bodies and b'BBBB\n' in bodies
    assert b'filename="a.dat"' in bodies and b'filename="b.dat"' in bodies


def test_files_argument_mirrors_only_the_listed_files(http_server, dataverse_server):
    """Only the named files are uploaded; the rest of the record stays behind."""
    _, calls = _mirror(http_server, dataverse_server, files=['b.dat'])
    adds = [c for c in calls if c['path'].endswith('/add')]
    assert len(adds) == 1
    assert b'filename="b.dat"' in adds[0]['body']
    assert b'filename="a.dat"' not in adds[0]['body']


def test_files_argument_naming_an_absent_file_is_refused_before_any_call(
    http_server, dataverse_server
):
    """A file the record does not hold aborts the mirror before Dataverse is touched."""
    dv_url, calls = dataverse_server
    with pytest.raises(ValueError, match='c.dat'):
        _mirror(http_server, dataverse_server, files=['a.dat', 'c.dat'])
    assert calls == []


def test_files_argument_empty_list_is_refused_before_any_call(http_server, dataverse_server):
    """An empty ``files`` list would create a dataset with nothing in it, so it is refused."""
    dv_url, calls = dataverse_server
    with pytest.raises(ValueError, match='selects no files'):
        _mirror(http_server, dataverse_server, files=[])
    assert calls == []


def test_no_publish_creates_draft_without_publishing(http_server, dataverse_server):
    """With publish disabled the dataset is created and filled but not published."""
    result, calls = _mirror(http_server, dataverse_server, publish=False)
    paths = [c['path'] for c in calls]
    assert result == 'doi:10.34894/DEMO01'
    assert paths.count('/api/datasets/:persistentId/add') == 2
    # Discrimination: no publish call at all, so the deposit stays a private draft.
    assert not any(p.endswith('/actions/:publish') for p in paths)


def test_dry_run_downloads_but_makes_no_dataverse_calls(http_server, dataverse_server):
    """A dry run verifies the Zenodo side and returns None without any Dataverse write."""
    result, calls = _mirror(http_server, dataverse_server, dry_run=True)
    assert result is None
    # Discrimination: the Dataverse server received nothing, so nothing was
    # created or published, yet the run completed (the download succeeded).
    assert calls == []


def test_empty_record_rejected(http_server, dataverse_server):
    """A Zenodo record with no files is refused before any Dataverse call."""
    base_url, root = http_server
    dv_url, calls = dataverse_server
    api_dir = root / 'api' / 'records'
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / '77').write_text(json.dumps({'id': 77, 'conceptrecid': '76', 'files': []}))
    with pytest.raises(ValueError, match='lists no files'):
        mirror_to_dataverse(
            '10.5281/zenodo.77',
            dataverse_url=dv_url,
            collection='Proteus_Fr',
            token='t',
            contact_name='x',
            contact_email='y@z',
            api_base=f'{base_url}api/records',
            base_urls=[base_url],
        )
    assert calls == []


def test_subject_is_carried_into_the_create_body(http_server, dataverse_server):
    """The requested subject reaches the create call verbatim; the server vets it there.

    The mirror does not second-guess the subject locally (the target installation
    is authoritative), so the value must flow through unchanged. A non-default
    value is used so the assertion discriminates against the argparse default.
    """
    result, calls = _mirror(http_server, dataverse_server, subject='Physics')
    assert result == 'doi:10.34894/DEMO01'
    create = next(c for c in calls if c['path'].endswith('/datasets'))
    fields = {
        f['typeName']: f
        for f in json.loads(create['body'])['datasetVersion']['metadataBlocks']['citation'][
            'fields'
        ]
    }
    # The exact value is sent, as a controlledVocabulary list, not the default.
    assert fields['subject']['value'] == ['Physics']
    assert fields['subject']['value'] != ['Astronomy and Astrophysics']
    assert fields['subject']['typeClass'] == 'controlledVocabulary'


def test_create_rejection_aborts_without_upload_or_rollback(http_server, dataverse_server):
    """A create rejected by the server raises and mints nothing.

    The mock rejects the create with a 400, as the live server does for an
    invalid citation field (an unknown subject among them); the subject value
    here is illustrative, not what triggers the mock, which rejects any create
    when armed. Because the create fails before a persistentId exists, there is
    nothing to upload, publish, or roll back: the only Dataverse call is the
    create, with no /add, no publish, and no DELETE. This pins the DataverseError
    path the accept-anything mock cannot otherwise reach; server-side validation
    of the subject itself is not exercised here.
    """
    from fwl_io.mirror import DataverseError

    _DataverseHandler.fail_on_create = True
    with pytest.raises(DataverseError, match='400'):
        _mirror(http_server, dataverse_server, subject='Planetary Science')
    _, calls = dataverse_server
    paths = [c['path'] for c in calls]
    # Only the create was attempted; no orphan draft, so nothing to clean up.
    assert paths == ['/api/dataverses/Proteus_Fr/datasets']
    assert not any(c['method'] == 'DELETE' for c in calls)


@pytest.mark.unit
def test_dataverse_error_on_failed_request():
    """A non-2xx Dataverse response raises DataverseError, not a silent pass."""
    import requests

    class _FakeResp:
        ok = False
        status_code = 403
        text = 'Forbidden'
        headers = {'Content-Type': 'text/plain'}

        def json(self):
            raise ValueError('not JSON')

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *a, **k: _FakeResp()
    try:
        with pytest.raises(DataverseError, match='403'):
            client.create_dataset('coll', {'datasetVersion': {}})
    finally:
        requests.request = orig


@pytest.mark.unit
def test_transport_failure_becomes_a_dataverse_error():
    """A network failure talking to Dataverse is wrapped as DataverseError, not leaked raw.

    So a caller has a single Dataverse error type to catch: a ConnectionError
    from the underlying request must surface as DataverseError, and the original
    cause must be chained for debugging.
    """
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request

    def boom(*args, **kwargs):
        raise requests.ConnectionError('name resolution failed')

    requests.request = boom
    try:
        # Match the cause text so the message interpolation is exercised, not just
        # the generic 'failed' that the HTTP-status message also contains.
        with pytest.raises(DataverseError, match='name resolution failed') as exc_info:
            client.create_dataset('coll', {'datasetVersion': {}})
        # The transport error is chained, not swallowed, so the cause survives.
        assert isinstance(exc_info.value.__cause__, requests.ConnectionError)
    finally:
        requests.request = orig


def _fake_response(status_code: int, content: bytes):
    import requests

    response = requests.Response()
    response.status_code = status_code
    response._content = content
    return response


def _publish_route(seen, post_reply, states=('DRAFT',)):
    """Answer state checks with ``states`` (the last repeats), others with ``post_reply``."""
    states = list(states)

    def route(method, *args, **kwargs):
        seen.append(method)
        if method != 'GET':
            return post_reply(method, *args, **kwargs)
        state = states.pop(0) if len(states) > 1 else states[0]
        body = {'status': 'OK', 'data': {'latestVersion': {'versionState': state}}}
        return _fake_response(200, json.dumps(body).encode())

    return route


@pytest.mark.unit
def test_create_with_an_empty_success_body_raises_for_the_missing_persistent_id():
    """A 2xx create response with an empty body still fails, for the missing id."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'')
    try:
        with pytest.raises(DataverseError, match='no persistentId'):
            client.create_dataset('coll', {'datasetVersion': {}})
    finally:
        requests.request = orig


@pytest.mark.unit
def test_create_with_a_non_json_success_body_raises_with_the_status_and_body():
    """A 2xx create response with a non-JSON body raises, naming the status and body.

    This is the exact shape a live Dataverse.nl call returned: a 2xx status with
    a body that failed to parse as JSON. The status code and raw body must be in
    the exception message, not just discoverable by decoding the JSON error text.
    """
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'this is not json')
    try:
        with pytest.raises(DataverseError, match='this is not json') as exc_info:
            client.create_dataset('coll', {'datasetVersion': {}})
        assert '200' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_create_with_a_non_object_json_body_raises_with_the_status_and_body():
    """A 2xx create response whose body parses to a non-object (e.g. null) raises.

    ``response.json()`` succeeds here, so this must be checked separately from
    the decode failure above; a caller must never see a bare AttributeError
    from treating a non-dict body as a dict.
    """
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'null')
    try:
        with pytest.raises(DataverseError, match='non-object JSON body') as exc_info:
            client.create_dataset('coll', {'datasetVersion': {}})
        assert '200' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_request_returns_the_decoded_body_on_a_valid_success_response():
    """A 2xx response with a non-empty JSON-object body decodes and returns as-is."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    body_bytes = b'{"status": "OK", "data": {"id": 7}}'
    requests.request = lambda *args, **kwargs: _fake_response(200, body_bytes)
    try:
        body = client._request('POST', '/api/datasets/:persistentId/add')
        assert body == {'status': 'OK', 'data': {'id': 7}}
    finally:
        requests.request = orig


@pytest.mark.unit
def test_add_file_accepts_an_empty_success_body(tmp_path):
    """add_file does not raise when Dataverse returns 2xx with an empty body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'')
    target = tmp_path / 'f.dat'
    target.write_bytes(b'data')
    try:
        client.add_file('doi:10.34894/DEMO01', target)  # must not raise
    finally:
        requests.request = orig


@pytest.mark.unit
def test_add_file_raises_with_the_status_and_body_on_a_non_json_success_body(tmp_path):
    """add_file raises on a 2xx non-JSON body, naming the status and body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'this is not json')
    target = tmp_path / 'f.dat'
    target.write_bytes(b'data')
    try:
        with pytest.raises(DataverseError, match='this is not json') as exc_info:
            client.add_file('doi:10.34894/DEMO01', target)
        assert '200' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_accepts_an_empty_success_body():
    """A 2xx with an empty body, then a RELEASED state, is a publish."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    seen = []
    requests.request = _publish_route(
        seen, lambda *args, **kwargs: _fake_response(200, b''), ('DRAFT', 'RELEASED')
    )
    try:
        client.publish('doi:10.34894/DEMO01')  # must not raise
        assert seen == ['GET', 'POST', 'GET']
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_raises_with_the_status_and_body_on_a_non_json_success_body(sleeps):
    """A 2xx non-JSON body and no RELEASED state is unconfirmed, naming the status and body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    seen = []
    requests.request = _publish_route(
        seen, lambda *args, **kwargs: _fake_response(200, b'this is not json')
    )
    try:
        with pytest.raises(DataversePublishUnconfirmed, match='this is not json') as exc_info:
            client.publish('doi:10.34894/DEMO01')
        assert '200' in str(exc_info.value)
        assert seen == ['GET', 'POST'] + ['GET'] * 5
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_existing_draft_never_creates_a_dataset(monkeypatch):
    """The publish-only wrapper calls publish() and never create_dataset()."""

    def _forbidden(*args, **kwargs):
        raise AssertionError('publish_existing_dataverse_draft must never create a dataset')

    monkeypatch.setattr(DataverseClient, 'create_dataset', _forbidden)
    monkeypatch.setattr(DataverseClient, 'publish', lambda self, pid, **k: None)

    publish_existing_dataverse_draft(
        'doi:10.34894/DEMO01', dataverse_url='http://unused', token='tok'
    )


@pytest.mark.unit
def test_publish_existing_draft_forwards_the_persistent_id_and_version_type(monkeypatch):
    """The wrapper forwards persistent_id and version_type to DataverseClient.publish."""
    captured = {}
    monkeypatch.setattr(
        DataverseClient,
        'publish',
        lambda self, pid, **k: captured.update(persistent_id=pid, **k),
    )

    publish_existing_dataverse_draft(
        'doi:10.34894/DEMO01',
        dataverse_url='http://unused',
        token='tok',
        version_type='minor',
    )
    assert captured == {'persistent_id': 'doi:10.34894/DEMO01', 'version_type': 'minor'}


@pytest.mark.unit
def test_publish_existing_draft_raises_clearly_on_an_already_published_dataset():
    """Publishing an already-published (or nonexistent) persistentId errors clearly.

    Dataverse answers a re-publish or an unknown persistentId with a non-2xx
    status; the wrapper must let DataverseError propagate with that status and
    body rather than silently no-op.
    """
    import requests

    orig = requests.request
    seen = []
    requests.request = _publish_route(
        seen,
        lambda *args, **kwargs: _fake_response(
            403, b'{"status": "ERROR", "message": "Dataset already published"}'
        ),
    )
    try:
        with pytest.raises(DataverseError, match='already published') as exc_info:
            publish_existing_dataverse_draft(
                'doi:10.34894/DEMO01', dataverse_url='http://unused', token='tok'
            )
        assert '403' in str(exc_info.value)
        assert seen == ['GET', 'POST']
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_existing_draft_raises_with_the_status_and_body_on_a_non_json_success_body(
    sleeps,
):
    """A 2xx non-JSON body and no RELEASED state is unconfirmed, naming the status and body."""
    import requests

    orig = requests.request
    seen = []
    requests.request = _publish_route(
        seen, lambda *args, **kwargs: _fake_response(200, b'this is not json')
    )
    try:
        with pytest.raises(DataversePublishUnconfirmed, match='this is not json') as exc_info:
            publish_existing_dataverse_draft(
                'doi:10.34894/DEMO01', dataverse_url='http://unused', token='tok'
            )
        assert '200' in str(exc_info.value)
        assert seen == ['GET', 'POST'] + ['GET'] * 5
    finally:
        requests.request = orig


@pytest.mark.unit
@pytest.mark.parametrize(
    'persistent_id',
    [
        '',
        '10.34894/DEMO01',
        'doi:',
        'doi:noSlashHere',
        'doi:10.34894/',
        'doi:/DEMO01',
        'doi: 10.34894/DEMO01',
        'doi:10.34894/DEMO01 ',
        'doi:10.34894/DE MO01',
    ],
)
def test_publish_existing_draft_rejects_a_malformed_persistent_id(persistent_id):
    """A persistent id that isn't 'doi:<prefix>/<suffix>' is rejected locally.

    No request is made: DataverseClient.publish is never reached, so this
    raises ValueError even with an unreachable dataverse_url.
    """
    with pytest.raises(ValueError, match='Dataverse persistent id'):
        publish_existing_dataverse_draft(persistent_id, dataverse_url='http://unused', token='tok')


@pytest.mark.unit
@pytest.mark.parametrize('version_type', ['', 'Major', 'patch', 'MAJOR'])
def test_publish_existing_draft_rejects_an_invalid_version_type(version_type):
    """A version_type other than 'major' or 'minor' is rejected locally.

    No request is made: DataverseClient.publish is never reached, so this
    raises ValueError even with an unreachable dataverse_url.
    """
    with pytest.raises(ValueError, match='version type'):
        publish_existing_dataverse_draft(
            'doi:10.34894/DEMO01',
            dataverse_url='http://unused',
            token='tok',
            version_type=version_type,
        )


@pytest.mark.unit
def test_publish_existing_draft_checks_version_type_before_persistent_id():
    """When both are invalid, the version_type error is raised first.

    No request is made either way: both checks run before DataverseClient
    is ever constructed.
    """
    with pytest.raises(ValueError, match='version type'):
        publish_existing_dataverse_draft(
            'not-a-doi',
            dataverse_url='http://unused',
            token='tok',
            version_type='bogus',
        )


@pytest.mark.unit
def test_publish_existing_draft_raises_clearly_on_a_nonexistent_persistent_id():
    """Publishing a persistentId Dataverse doesn't recognize errors clearly."""
    import requests

    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(
        404,
        b'{"status": "ERROR", "message": "Dataset with Persistent ID doi:10.34894/'
        b'NOPE could not be found."}',
    )
    try:
        with pytest.raises(DataverseError, match='could not be found') as exc_info:
            publish_existing_dataverse_draft(
                'doi:10.34894/NOPE', dataverse_url='http://unused', token='tok'
            )
        assert '404' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_delete_draft_accepts_an_empty_success_body():
    """delete_draft (the rollback path) does not raise when Dataverse returns an empty 2xx."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'')
    try:
        client.delete_draft('doi:10.34894/DEMO01')  # must not raise
    finally:
        requests.request = orig


@pytest.mark.unit
def test_delete_draft_raises_with_the_status_and_body_on_a_non_json_success_body():
    """delete_draft (the rollback path) raises on a 2xx non-JSON body, naming status and body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'this is not json')
    try:
        with pytest.raises(DataverseError, match='this is not json') as exc_info:
            client.delete_draft('doi:10.34894/DEMO01')
        assert '200' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_citation_maps_zenodo_metadata_faithfully():
    """The citation block carries the Zenodo title, authors, and source note."""
    record = {
        'id': 55,
        'doi': '10.5281/zenodo.55',
        'metadata': {
            'title': 'Demo tracks',
            'creators': [
                {'name': 'Doe, Jane', 'affiliation': 'Example University'},
                {'name': 'Roe, Richard'},
            ],
            'description': 'A demo dataset.',
        },
    }
    citation = zenodo_record_to_citation(
        record,
        contact_name='PROTEUS',
        contact_email='c@x.org',
        subject='Astronomy and Astrophysics',
    )
    fields = {
        f['typeName']: f['value']
        for f in citation['datasetVersion']['metadataBlocks']['citation']['fields']
    }
    assert fields['title'] == 'Demo tracks'
    # Two authors carried through; the affiliation is preserved where present
    # and omitted where absent (not invented).
    assert fields['author'][0]['authorName']['value'] == 'Doe, Jane'
    assert fields['author'][0]['authorAffiliation']['value'] == 'Example University'
    assert 'authorAffiliation' not in fields['author'][1]
    # The Zenodo description survives AND a source note records the origin DOI,
    # so both the original text and the provenance are present (two entries).
    descriptions = [d['dsDescriptionValue']['value'] for d in fields['dsDescription']]
    assert len(descriptions) == 2
    assert 'A demo dataset.' in descriptions
    assert any('10.5281/zenodo.55' in d for d in descriptions)
    assert fields['subject'] == ['Astronomy and Astrophysics']


@pytest.mark.unit
def test_citation_falls_back_to_title_when_description_missing():
    """With no Zenodo description the title stands in, so the field is never empty."""
    record = {'id': 3, 'doi': '10.5281/zenodo.3', 'metadata': {'title': 'Only a title'}}
    citation = zenodo_record_to_citation(
        record, contact_name='c', contact_email='c@x', subject='Other'
    )
    fields = {
        f['typeName']: f['value']
        for f in citation['datasetVersion']['metadataBlocks']['citation']['fields']
    }
    descriptions = [d['dsDescriptionValue']['value'] for d in fields['dsDescription']]
    # The primary description falls back to the title (Dataverse rejects an empty
    # description), and the source note is still present.
    assert 'Only a title' in descriptions
    assert any('10.5281/zenodo.3' in d for d in descriptions)


@pytest.mark.unit
def test_citation_defaults_author_when_creators_missing():
    """A record without creators still yields a valid single author field."""
    record = {'id': 9, 'doi': '10.5281/zenodo.9', 'metadata': {'title': 'No authors'}}
    citation = zenodo_record_to_citation(
        record, contact_name='c', contact_email='c@x', subject='Other'
    )
    fields = {
        f['typeName']: f['value']
        for f in citation['datasetVersion']['metadataBlocks']['citation']['fields']
    }
    # Dataverse requires at least one author; a placeholder is supplied rather
    # than an empty list that the API would reject.
    assert len(fields['author']) == 1
    assert fields['author'][0]['authorName']['value'] == 'Unknown'
    # The placeholder author is fully typed too: the no-creators fallback must
    # not revert to a bare {'value': ...} that the server rejects.
    assert fields['author'][0]['authorName']['typeClass'] == 'primitive'
    assert fields['author'][0]['authorName']['multiple'] is False
    # No affiliation is invented for the placeholder author.
    assert 'authorAffiliation' not in fields['author'][0]


@pytest.mark.unit
def test_citation_fields_declare_typeclass_and_multiple():
    """Every field, and every compound sub-field, carries the Dataverse type metadata.

    The native API requires ``typeClass`` and ``multiple`` on each field and on
    each sub-field of a compound field; a field sent with only a name and value
    is rejected by the server. The assertions walk the whole emitted document
    rather than a fixed name list, so a bare ``{'typeName', 'value'}`` shape on
    any field or sub-field, present now or added later, fails the test.
    """
    record = {
        'id': 12,
        'doi': '10.5281/zenodo.12',
        'metadata': {
            'title': 'Typed tracks',
            'creators': [{'name': 'Doe, Jane', 'affiliation': 'Example University'}],
            'description': 'A demo dataset.',
        },
    }
    citation = zenodo_record_to_citation(
        record,
        contact_name='PROTEUS',
        contact_email='c@x.org',
        subject='Astronomy and Astrophysics',
    )
    fields = citation['datasetVersion']['metadataBlocks']['citation']['fields']
    valid_classes = {'primitive', 'compound', 'controlledVocabulary'}

    def assert_typed(field, where):
        assert set(field) >= {'typeName', 'typeClass', 'multiple', 'value'}, where
        assert field['typeClass'] in valid_classes, f'{where}: {field["typeClass"]}'
        assert isinstance(field['multiple'], bool), where

    # Walk every top-level field and every sub-field of every compound field, so
    # a dropped attribute anywhere (authorAffiliation, datasetContactName, or a
    # field added later) is caught, not just the few names spelled out below.
    for field in fields:
        assert_typed(field, field['typeName'])
        if field['typeClass'] == 'compound':
            for entry in field['value']:
                for sub_name, sub in entry.items():
                    assert_typed(sub, f'{field["typeName"]}.{sub_name}')
                    assert sub['typeClass'] == 'primitive', f'{field["typeName"]}.{sub_name}'

    by_name = {f['typeName']: f for f in fields}
    # Exact typeClass per field, so a wrong-but-valid class (subject built as a
    # primitive, say) is caught, not only a missing attribute.
    assert by_name['title']['typeClass'] == 'primitive'
    assert by_name['author']['typeClass'] == 'compound'
    assert by_name['datasetContact']['typeClass'] == 'compound'
    assert by_name['dsDescription']['typeClass'] == 'compound'
    assert by_name['subject']['typeClass'] == 'controlledVocabulary'
    # Exact multiple per field: single-valued title against the repeatable
    # compounds and subject, so a flipped flag on any of them fails.
    assert by_name['title']['multiple'] is False
    assert by_name['author']['multiple'] is True
    assert by_name['datasetContact']['multiple'] is True
    assert by_name['dsDescription']['multiple'] is True
    assert by_name['subject']['multiple'] is True
    # The affiliation sub-field, present in this record, is fully typed too (a
    # path a check for authorName alone would skip).
    author = by_name['author']['value'][0]
    assert author['authorAffiliation']['typeClass'] == 'primitive'
    assert author['authorAffiliation']['value'] == 'Example University'
    # The mapped values still survive alongside the type metadata.
    contact = by_name['datasetContact']['value'][0]
    assert contact['datasetContactEmail']['value'] == 'c@x.org'
    assert by_name['subject']['value'] == ['Astronomy and Astrophysics']


def test_contact_email_required_even_for_a_draft(http_server, dataverse_server):
    """A no-publish draft still needs a contact email; it is refused without one.

    Dataverse requires a point-of-contact email on every dataset, so an empty
    email is rejected up front even when publishing is off, before any download
    or deposit, rather than surfacing as a server error mid-run. The served
    record ensures the only reason to raise is the guard, not a missing record.
    """
    base_url, root = http_server
    dv_url, calls = dataverse_server
    _serve_zenodo_record(root, 55, {'a.dat': b'AAA\n'})
    with pytest.raises(ValueError, match='contact email'):
        mirror_to_dataverse(
            '10.5281/zenodo.55',
            dataverse_url=dv_url,
            collection='Proteus_Fr',
            token='t',
            contact_name='x',
            contact_email='',  # empty: refused even though publish is False
            publish=False,
            api_base=f'{base_url}api/records',
            base_urls=[base_url],
        )
    # Discrimination against a publish-only guard: publish=False still refuses, so
    # no draft or DOI is minted and the server is never touched.
    assert calls == []


def test_failed_upload_rolls_back_the_draft(http_server, dataverse_server):
    """When an upload fails, the created draft is deleted so no orphan is left."""
    from fwl_io.mirror import DataverseError

    _DataverseHandler.fail_on_add = True
    with pytest.raises(DataverseError):
        _mirror(http_server, dataverse_server)
    _, calls = dataverse_server
    # The draft was created, then deleted after the add failed; it was never published.
    assert any(c['path'].endswith('/datasets') and c['method'] == 'POST' for c in calls)
    deletes = [c for c in calls if c['method'] == 'DELETE']
    assert len(deletes) == 1
    assert deletes[0]['query'].get('persistentId') == ['doi:10.34894/DEMO01']
    assert not any(c['path'].endswith('/actions/:publish') for c in calls)


def test_failed_publish_rolls_back_the_draft(http_server, dataverse_server):
    """When publishing fails, the created draft is deleted so no orphan is left.

    Publish is the last orphan-creating step: all files upload, then the publish
    is rejected, so the draft must be removed rather than left as a private
    deposit with a minted DOI. This covers the rollback branch past the upload
    failure that the upload-rollback test exercises.
    """
    from fwl_io.mirror import DataverseError

    _DataverseHandler.fail_on_publish = True
    with pytest.raises(DataverseError, match='publish rejected'):
        _mirror(http_server, dataverse_server)
    _, calls = dataverse_server
    # Both files uploaded and a publish was attempted, then the draft was deleted.
    assert sum(c['path'].endswith('/add') for c in calls) == 2
    assert any(c['path'].endswith('/actions/:publish') for c in calls)
    deletes = [c for c in calls if c['method'] == 'DELETE']
    assert len(deletes) == 1
    assert deletes[0]['query'].get('persistentId') == ['doi:10.34894/DEMO01']


def test_failed_publish_keeps_the_original_error_when_rollback_also_fails(
    http_server, dataverse_server
):
    """A publish failure is preserved even when the rollback delete also fails.

    With both the publish and the cleanup delete rejected, the original publish
    error must propagate (not the delete error), and a rollback delete must still
    have been attempted. This is the publish-origin analogue of the upload-origin
    "original error wins" case, guarding the shared rollback if publish ever gets
    its own block.
    """
    from fwl_io.mirror import DataverseError

    _DataverseHandler.fail_on_publish = True
    _DataverseHandler.fail_on_delete = True
    # The propagated error is the publish rejection, not the delete failure.
    with pytest.raises(DataverseError, match='publish rejected'):
        _mirror(http_server, dataverse_server)
    _, calls = dataverse_server
    # A rollback delete was attempted even though it too failed.
    assert any(c['method'] == 'DELETE' for c in calls)


def test_publish_requires_contact_email(http_server, dataverse_server):
    """Publishing without a contact email fails fast, before any Dataverse write."""
    _, calls = dataverse_server  # noqa: F841 -- asserted empty below
    with pytest.raises(ValueError, match='contact email'):
        _mirror(http_server, dataverse_server, contact_email='', publish=True)
    # Fail-fast: nothing was created, so no draft or DOI was minted.
    assert dataverse_server[1] == []


def test_download_failure_aborts_before_any_dataverse_write(
    http_server, dataverse_server, monkeypatch
):
    """A failed Zenodo download stops the mirror before any Dataverse call."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.fetch import DownloadError

    base_url, root = http_server
    dv_url, calls = dataverse_server
    _serve_zenodo_record(root, 55, {'a.dat': b'AAA\n'})

    # Stub the download to fail as the fetcher would on a checksum/network error;
    # patching keeps the test hermetic (no fall-through to the real doi.org).
    def boom(*a, **k):
        raise DownloadError('checksum mismatch for a.dat')

    monkeypatch.setattr(mirror_mod, '_download_zenodo_files', boom)

    with pytest.raises(DownloadError):
        mirror_to_dataverse(
            '10.5281/zenodo.55',
            dataverse_url=dv_url,
            collection='Proteus_Fr',
            token='t',
            contact_name='x',
            contact_email='y@z',
            api_base=f'{base_url}api/records',
            base_urls=[base_url],
        )
    # The download failed, so the Dataverse server was never touched.
    assert calls == []


def test_dry_run_without_contact_email_still_runs(http_server, dataverse_server):
    """A dry run needs no contact email: the publish guard must not fire on it."""
    result, calls = _mirror(http_server, dataverse_server, contact_email='', dry_run=True)
    # The documented `--dry-run` command passes no contact email; it must still
    # download and return None rather than being refused by the publish guard.
    assert result is None
    assert calls == []


def test_nested_file_name_rejected(http_server, dataverse_server):
    """A nested Zenodo file name is refused rather than flattened on upload."""
    base_url, root = http_server
    dv_url, calls = dataverse_server
    (root / 'sub').mkdir()
    _serve_zenodo_record(root, 66, {'sub/f.dat': b'X\n'})
    with pytest.raises(ValueError, match='nested file names'):
        mirror_to_dataverse(
            '10.5281/zenodo.66',
            dataverse_url=dv_url,
            collection='C',
            token='t',
            contact_name='x',
            contact_email='y@z',
            api_base=f'{base_url}api/records',
            base_urls=[base_url],
        )
    assert calls == []


def test_rollback_failure_does_not_mask_the_original_error(http_server, dataverse_server):
    """If deleting the draft also fails, the original upload error still propagates."""
    from fwl_io.mirror import DataverseError

    _DataverseHandler.fail_on_add = True
    _DataverseHandler.fail_on_delete = True
    with pytest.raises(DataverseError, match='bad field'):
        _mirror(http_server, dataverse_server)
    _, calls = dataverse_server
    # Both the failed add and the attempted (also-failed) delete were issued;
    # the operator is told to clean up manually, but the original error wins.
    assert any(c['method'] == 'DELETE' for c in calls)


def test_missing_persistent_id_is_an_error(http_server, dataverse_server):
    """A create response without a persistentId fails instead of uploading to nowhere."""
    from fwl_io.mirror import DataverseError

    _DataverseHandler.omit_persistent_id = True
    with pytest.raises(DataverseError, match='no persistentId'):
        _mirror(http_server, dataverse_server)
    _, calls = dataverse_server
    # It failed at create, so no file upload was attempted.
    assert not any(c['path'].endswith('/add') for c in calls)


@pytest.mark.unit
def test_cli_mirror_requires_token_unless_dry_run(monkeypatch, capsys):
    """The mirror CLI refuses a real run without a token, and does not call the mirror."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    called = []
    monkeypatch.setattr(mirror_mod, 'mirror_to_dataverse', lambda *a, **k: called.append(1))
    monkeypatch.delenv('DATAVERSE_TOKEN', raising=False)

    rc = main(['mirror', '10.5281/zenodo.55', '--collection', 'Proteus_Fr'])
    assert rc == 1
    assert 'DATAVERSE_TOKEN' in capsys.readouterr().err
    # The guard fires before any mirror attempt, so no partial work runs.
    assert called == []


@pytest.mark.unit
def test_cli_mirror_dry_run_needs_no_token(monkeypatch, capsys):
    """A dry run is allowed without a token, reports completion, and forwards its flags."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    captured = {}
    monkeypatch.setattr(
        mirror_mod, 'mirror_to_dataverse', lambda doi, **k: captured.update(doi=doi, **k)
    )
    monkeypatch.delenv('DATAVERSE_TOKEN', raising=False)

    rc = main(['mirror', '10.5281/zenodo.55', '--collection', 'Proteus_Fr', '--dry-run'])
    assert rc == 0
    assert 'dry run complete' in capsys.readouterr().out
    # The CLI must forward the flags verbatim: a dropped --dry-run would turn a
    # requested preview into a live create+upload+publish in the token-holding job.
    assert captured['dry_run'] is True
    assert captured['collection'] == 'Proteus_Fr'
    assert captured['doi'] == '10.5281/zenodo.55'
    assert captured['publish'] is True  # not --no-publish


@pytest.mark.unit
def test_cli_mirror_forwards_no_publish(monkeypatch):
    """--no-publish reaches the mirror as publish=False, not silently dropped."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    captured = {}
    monkeypatch.setattr(mirror_mod, 'mirror_to_dataverse', lambda doi, **k: captured.update(**k))
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    main(['mirror', '10.5281/zenodo.55', '--collection', 'C', '--no-publish'])
    assert captured['publish'] is False
    assert captured['dry_run'] is False


@pytest.mark.unit
def test_cli_mirror_forwards_subject_and_contact_email(monkeypatch):
    """--subject and --contact-email reach the mirror, not silently dropped.

    --subject fails open: without forwarding, the argparse default is sent, the
    server accepts it, and a dataset is mirrored with the wrong subject and no
    error. The non-default values here make the assertions discriminate against
    the defaults, so a dropped forward turns the test red.
    """
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    captured = {}
    monkeypatch.setattr(mirror_mod, 'mirror_to_dataverse', lambda doi, **k: captured.update(**k))
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    main(
        [
            'mirror',
            '10.5281/zenodo.55',
            '--collection',
            'C',
            '--subject',
            'Physics',
            '--contact-email',
            'curator@example.org',
        ]
    )
    assert captured['subject'] == 'Physics'
    assert captured['subject'] != 'Astronomy and Astrophysics'  # not the argparse default
    assert captured['contact_email'] == 'curator@example.org'


@pytest.mark.unit
def test_cli_mirror_refuses_a_real_run_without_contact_email(monkeypatch, capsys):
    """A real run (token set, not --dry-run) with no --contact-email is refused at the CLI.

    The email guard lives in the library and fires before any Zenodo fetch. A
    stubbed fetch that raises if called keeps the test hermetic and enforces that
    ordering: reordering the guard after the fetch would trip the stub instead of
    silently issuing a live request.
    """
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    def _no_network(*args, **kwargs):
        raise AssertionError('the contact-email guard must fire before any Zenodo fetch')

    monkeypatch.setattr(mirror_mod, 'fetch_zenodo_record', _no_network)
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')
    rc = main(['mirror', '10.5281/zenodo.55', '--collection', 'Proteus_Fr'])
    assert rc == 1
    err = capsys.readouterr().err
    # Actionable in both contexts: the CLI flag and the API keyword are named.
    assert '--contact-email' in err
    assert 'contact_email' in err
    # Discrimination: this is the contact-email guard, not the token guard.
    assert 'contact email' in err.lower()
    assert 'DATAVERSE_TOKEN' not in err


@pytest.mark.unit
def test_cli_mirror_prints_manifest_ready_dataverse_doi(monkeypatch, capsys):
    """On success the CLI prints the mirror DOI without the doi: prefix for the manifest."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    monkeypatch.setattr(mirror_mod, 'mirror_to_dataverse', lambda *a, **k: 'doi:10.34894/DEMO01')
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    rc = main(['mirror', '10.5281/zenodo.55', '--collection', 'Proteus_Fr'])
    assert rc == 0
    out = capsys.readouterr().out
    # The manifest field takes a bare DOI, so the doi: prefix is stripped.
    assert 'dataverse = "10.34894/DEMO01"' in out
    assert 'doi:10.34894/DEMO01' not in out.split('add this to the manifest')[1]


@pytest.mark.unit
def test_cli_mirror_publish_requires_token(monkeypatch, capsys):
    """mirror-publish refuses to run without a token, and never calls the wrapper."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    called = []
    monkeypatch.setattr(
        mirror_mod, 'publish_existing_dataverse_draft', lambda *a, **k: called.append(1)
    )
    monkeypatch.delenv('DATAVERSE_TOKEN', raising=False)

    rc = main(['mirror-publish', 'doi:10.34894/DEMO01'])
    assert rc == 1
    assert 'DATAVERSE_TOKEN' in capsys.readouterr().err
    assert called == []


@pytest.mark.unit
def test_cli_mirror_publish_forwards_the_persistent_id_url_and_version_type(monkeypatch):
    """mirror-publish forwards the persistent id and its flags, unmodified."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    captured = {}
    monkeypatch.setattr(
        mirror_mod,
        'publish_existing_dataverse_draft',
        lambda pid, **k: captured.update(persistent_id=pid, **k),
    )
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    main(
        [
            'mirror-publish',
            'doi:10.34894/DEMO01',
            '--dataverse-url',
            'https://demo.dataverse.org',
            '--version-type',
            'minor',
        ]
    )
    assert captured['persistent_id'] == 'doi:10.34894/DEMO01'
    assert captured['dataverse_url'] == 'https://demo.dataverse.org'
    assert captured['version_type'] == 'minor'


@pytest.mark.unit
def test_cli_mirror_publish_prints_the_published_id_on_success(monkeypatch, capsys):
    """On success mirror-publish reports the persistent id it published."""
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main

    monkeypatch.setattr(mirror_mod, 'publish_existing_dataverse_draft', lambda *a, **k: None)
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    rc = main(['mirror-publish', 'doi:10.34894/DEMO01'])
    assert rc == 0
    assert 'published doi:10.34894/DEMO01' in capsys.readouterr().out


@pytest.mark.unit
def test_cli_mirror_publish_surfaces_a_dataverse_error(monkeypatch, capsys):
    """A DataverseError from the wrapper (e.g. already published) reaches the CLI as text.

    The CLI's top-level error boundary formats it as a one-line message rather
    than a traceback, so an already-published or unknown persistentId is
    reported clearly instead of crashing the Actions job with a stack trace.
    """
    import fwl_io.mirror as mirror_mod
    from fwl_io.cli import main
    from fwl_io.mirror import DataverseError

    def _raise(*args, **kwargs):
        raise DataverseError(
            'Dataverse POST /api/datasets/:persistentId/actions/:publish failed '
            '(403): Dataset already published'
        )

    monkeypatch.setattr(mirror_mod, 'publish_existing_dataverse_draft', _raise)
    monkeypatch.setenv('DATAVERSE_TOKEN', 'tok')

    rc = main(['mirror-publish', 'doi:10.34894/DEMO01'])
    assert rc == 1
    assert 'already published' in capsys.readouterr().err


@pytest.fixture()
def sleeps(monkeypatch):
    """Record the retry waits instead of sleeping."""
    import fwl_io.mirror as mirror

    waits = []
    monkeypatch.setattr(mirror, '_sleep', waits.append)
    return waits


def _adds(calls, name=None):
    adds = [c for c in calls if c['method'] == 'POST' and c['path'].endswith('/add')]
    return [c for c in adds if name is None or f'filename="{name}"'.encode() in c['body']]


def test_bot_check_page_on_add_is_retried_with_the_file_reopened(
    http_server, dataverse_server, sleeps
):
    """Two bot-check pages on an upload: listed, waited, then sent again in full."""
    _DataverseHandler.script = {('POST', '/add'): ['challenge', 'challenge']}
    result, calls = _mirror(http_server, dataverse_server)
    assert result == 'doi:10.34894/DEMO01'
    assert len(_adds(calls, 'a.dat')) == 3
    assert all(b'AAA\n' in c['body'] for c in _adds(calls, 'a.dat'))
    assert sleeps == [30.0, 60.0]
    # One listing before each resend, one for the file-set check.
    assert sum(c['path'].endswith('/versions/:draft/files') for c in calls) == 3
    assert not any(c['method'] == 'DELETE' for c in calls)


def test_an_upload_that_arrived_behind_a_bot_check_page_is_not_sent_again(
    http_server, dataverse_server, sleeps
):
    """The file reached the draft but the reply was the bot-check page: no second upload."""
    _DataverseHandler.script_after = {('POST', '/add'): ['challenge']}
    result, calls = _mirror(http_server, dataverse_server)
    assert result == 'doi:10.34894/DEMO01'
    assert len(_adds(calls, 'a.dat')) == 1
    assert [f['filename'] for f in _DataverseHandler.draft_files] == ['a.dat', 'b.dat']
    assert sleeps == [30.0]


def test_a_different_file_of_the_same_name_in_the_draft_stops_the_upload(
    http_server, dataverse_server, sleeps
):
    """A listed file with the same name but other bytes is an error, never overwritten."""
    _DataverseHandler.script = {('POST', '/add'): ['challenge']}
    _DataverseHandler.draft_files = [
        {'filename': 'a.dat', 'filesize': 4, 'checksum': {'type': 'MD5', 'value': '0' * 32}}
    ]
    with pytest.raises(DataverseError, match='holds a file named a.dat that is not the same file'):
        _mirror(http_server, dataverse_server)
    assert len(_adds(_DataverseHandler.calls, 'a.dat')) == 1
    assert _DataverseHandler.deleted


def test_a_gateway_error_is_retried_but_a_rejection_is_not(http_server, dataverse_server, sleeps):
    """A 504 on upload is repeated; a 400 fails at once and rolls the draft back."""
    _DataverseHandler.script = {('POST', '/add'): [504]}
    result, calls = _mirror(http_server, dataverse_server)
    assert result == 'doi:10.34894/DEMO01'
    assert len(_adds(calls, 'a.dat')) == 2
    assert sleeps == [30.0]

    sleeps.clear()
    _DataverseHandler.calls.clear()
    _DataverseHandler.fail_on_add = True
    with pytest.raises(DataverseError, match='400'):
        _mirror(http_server, dataverse_server)
    assert len(_adds(_DataverseHandler.calls)) == 1
    assert sleeps == []


def test_retries_run_out_with_a_message_naming_the_bot_check_page(
    http_server, dataverse_server, sleeps
):
    """Five bot-check pages in a row stop the mirror, name the page, and roll back."""
    from fwl_io.mirror import DataverseRetryableError

    _DataverseHandler.script = {('POST', '/add'): ['challenge'] * 5}
    with pytest.raises(DataverseRetryableError, match='sent 5 time.*in 5 attempts.*bot-check page'):
        _mirror(http_server, dataverse_server)
    assert sleeps == [30.0, 60.0, 120.0, 240.0]
    assert len(_adds(_DataverseHandler.calls, 'a.dat')) == 5
    assert _DataverseHandler.deleted


def test_backoff_is_capped(http_server, dataverse_server, sleeps, monkeypatch):
    """The wait doubles from 30 s and stops growing at 300 s."""
    import fwl_io.mirror as mirror

    monkeypatch.setattr(mirror, 'MAX_ATTEMPTS', 7)
    _DataverseHandler.script = {('POST', '/add'): ['challenge'] * 6}
    result, _calls = _mirror(http_server, dataverse_server)
    assert result == 'doi:10.34894/DEMO01'
    assert sleeps == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


def test_the_rollback_delete_is_retried(http_server, dataverse_server, sleeps, caplog):
    """A bot-check page on the rollback does not leave an orphan draft."""
    _DataverseHandler.fail_on_add = True
    _DataverseHandler.script = {('DELETE', '/api/datasets/:persistentId'): ['challenge']}
    with pytest.raises(DataverseError, match='400'):
        _mirror(http_server, dataverse_server)
    deletes = [c for c in _DataverseHandler.calls if c['method'] == 'DELETE']
    assert len(deletes) == 2
    assert _DataverseHandler.deleted
    assert 'could not roll back' not in caplog.text


def test_a_rollback_that_went_through_is_not_repeated(
    http_server, dataverse_server, sleeps, caplog
):
    """The draft was deleted behind a bot-check page: the 404 ends the retry."""
    _DataverseHandler.fail_on_add = True
    _DataverseHandler.script_after = {('DELETE', '/api/datasets/:persistentId'): ['challenge']}
    with pytest.raises(DataverseError, match='400'):
        _mirror(http_server, dataverse_server)
    deletes = [c for c in _DataverseHandler.calls if c['method'] == 'DELETE']
    assert len(deletes) == 1
    assert sleeps == [30.0]
    assert 'could not roll back' not in caplog.text


def test_a_publish_that_went_through_is_not_repeated(http_server, dataverse_server, sleeps):
    """The dataset was published behind a bot-check page: no second publish."""
    _DataverseHandler.script_after = {('POST', '/actions/:publish'): ['challenge']}
    result, calls = _mirror(http_server, dataverse_server, publish=True)
    assert result == 'doi:10.34894/DEMO01'
    publishes = [c for c in calls if c['path'].endswith('/actions/:publish')]
    assert len(publishes) == 1
    assert sleeps == [30.0]


@pytest.mark.unit
def test_an_html_body_without_the_bot_check_marker_is_not_retried(sleeps):
    """Only the bot-check page is retryable; another HTML body is a plain failure."""
    import requests

    from fwl_io.mirror import DataverseRetryableError

    response = _fake_response(200, b'<html><body>maintenance</body></html>')
    response.headers['Content-Type'] = 'text/html'
    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: response
    try:
        with pytest.raises(DataverseError, match='non-JSON body') as info:
            client.publish('doi:10.34894/X')
    finally:
        requests.request = orig
    assert not isinstance(info.value, DataverseRetryableError)
    assert sleeps == []


@pytest.mark.unit
def test_bot_check_detection_ignores_header_case_and_needs_html():
    """An upper-case text/html header is detected; the marker in a JSON body is not."""
    import requests

    from fwl_io.mirror import DataverseRetryableError

    page = _fake_response(403, b'<html><title>Oh noes!</title></html>')
    page.headers['Content-Type'] = 'TEXT/HTML; charset=UTF-8'
    json_body = _fake_response(200, b'{"status": "OK", "message": "Oh noes!"}')
    json_body.headers['Content-Type'] = 'application/json'
    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    try:
        requests.request = lambda *args, **kwargs: page
        with pytest.raises(DataverseRetryableError, match='bot-check page'):
            client._request('GET', '/api/x')
        requests.request = lambda *args, **kwargs: json_body
        assert client._request('GET', '/api/x') == {'status': 'OK', 'message': 'Oh noes!'}
    finally:
        requests.request = orig


@pytest.mark.unit
@pytest.mark.parametrize(
    ('kind', 'expected'),
    [('SHA-256', True), ('SHA-512', True), ('SHA-1', True), ('MD5', True), ('CRC32C', False)],
)
def test_same_file_needs_a_known_checksum_type(tmp_path, kind, expected):
    """A listed file matches only with its size and a checksum of a known type."""
    from fwl_io.mirror import _ALGORITHMS, _same_file

    path = tmp_path / 'a.dat'
    path.write_bytes(b'AAAA')
    algorithm = _ALGORITHMS.get(kind, 'md5')
    value = hashlib.new(algorithm, b'AAAA').hexdigest()
    entry = {'filesize': 4, 'checksum': {'type': kind, 'value': value}}
    assert _same_file(entry, path) is expected
    other = {
        'filesize': 4,
        'checksum': {'type': kind, 'value': hashlib.new(algorithm, b'ZZZZ').hexdigest()},
    }
    assert _same_file(other, path) is False
    if expected:
        assert _same_file({**entry, 'checksum': {'type': kind, 'value': value.upper()}}, path)
        assert _same_file({**entry, 'filesize': 5}, path) is False


@pytest.mark.unit
def test_a_timeout_is_retried(sleeps):
    """A read timeout may hide a request that went through, so it is retried."""
    import requests

    replies = [requests.Timeout('read timed out'), _fake_response(404, b'{"status": "ERROR"}')]

    def fake(*args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = fake
    try:
        client.delete_draft('doi:10.34894/X')
    finally:
        requests.request = orig
    assert sleeps == [30.0]
    assert replies == []


def test_a_bot_check_page_on_the_listing_counts_as_an_attempt(
    http_server, dataverse_server, sleeps
):
    """A challenged listing uses an attempt; the upload is sent again once the listing works."""
    _DataverseHandler.script = {
        ('POST', '/add'): ['challenge', 'challenge'],
        ('GET', '/versions/:draft/files'): ['challenge'],
    }
    result, calls = _mirror(http_server, dataverse_server)
    assert result == 'doi:10.34894/DEMO01'
    assert len(_adds(calls, 'a.dat')) == 3
    assert sleeps == [30.0, 60.0, 120.0]


def test_the_final_error_counts_the_calls_actually_sent(http_server, dataverse_server, sleeps):
    """A challenged listing uses an attempt, so 5 attempts sent the upload 4 times."""
    from fwl_io.mirror import DataverseRetryableError

    _DataverseHandler.script = {
        ('POST', '/add'): ['challenge'] * 4,
        ('GET', '/versions/:draft/files'): ['challenge'],
    }
    with pytest.raises(DataverseRetryableError, match=r'sent 4 time\(s\) in 5 attempts'):
        _mirror(http_server, dataverse_server)
    assert len(_adds(_DataverseHandler.calls, 'a.dat')) == 4


def test_create_is_not_retried(http_server, dataverse_server, sleeps):
    """A bot-check page on create stops at once: a repeat could mint a second draft."""
    from fwl_io.mirror import DataverseRetryableError

    _DataverseHandler.script = {('POST', '/datasets'): ['challenge']}
    with pytest.raises(DataverseRetryableError, match='bot-check page'):
        _mirror(http_server, dataverse_server)
    creates = [c for c in _DataverseHandler.calls if c['path'].endswith('/datasets')]
    assert len(creates) == 1
    assert sleeps == []
    assert not any(c['method'] == 'DELETE' for c in _DataverseHandler.calls)


def test_a_failed_deletion_check_is_reported_not_counted_as_deleted(
    http_server, dataverse_server, sleeps, caplog
):
    """Only a 404 means deleted; another error on the check leaves the draft to delete by hand."""
    _DataverseHandler.fail_on_add = True
    _DataverseHandler.script = {
        ('DELETE', '/api/datasets/:persistentId'): ['challenge'],
        ('GET', '/api/datasets/:persistentId'): [500],
    }
    with pytest.raises(DataverseError, match='400'):
        _mirror(http_server, dataverse_server)
    assert 'could not roll back' in caplog.text
    assert not _DataverseHandler.deleted


def test_publish_refuses_an_already_published_dataset(dataverse_server, sleeps):
    """A published dataset is reported, not published again or passed as success."""
    dv_url, calls = dataverse_server
    _DataverseHandler.released = True
    client = DataverseClient(dv_url, 'tok')
    with pytest.raises(DataverseError, match='already published'):
        client.publish('doi:10.34894/DEMO01')
    assert not any(c['path'].endswith('/actions/:publish') for c in calls)


def test_an_unconfirmed_publish_keeps_the_dataset(http_server, dataverse_server, sleeps, caplog):
    """When no publish reply gets through, the dataset may be public, so it is not deleted."""
    _DataverseHandler.script = {('POST', '/actions/:publish'): ['challenge'] * 5}
    with pytest.raises(DataversePublishUnconfirmed, match='publish of doi:10.34894/DEMO01'):
        _mirror(http_server, dataverse_server, publish=True)
    assert not any(c['method'] == 'DELETE' for c in _DataverseHandler.calls)
    assert 'not confirmed' in caplog.text


def test_an_extra_file_in_the_draft_stops_the_mirror(http_server, dataverse_server, sleeps):
    """A draft that holds a file Zenodo does not (e.g. a renamed second upload) is rolled back."""
    _DataverseHandler.draft_files = [
        {'filename': 'a-1.dat', 'filesize': 4, 'checksum': {'type': 'MD5', 'value': '0' * 32}}
    ]
    with pytest.raises(DataverseError, match=r"not expected \['a-1.dat'\]"):
        _mirror(http_server, dataverse_server, publish=True)
    assert not any(c['path'].endswith('/actions/:publish') for c in _DataverseHandler.calls)
    assert _DataverseHandler.deleted


def test_a_failed_state_check_rolls_the_draft_back(http_server, dataverse_server, sleeps, caplog):
    """No publish request went out, so the draft is known private and is deleted."""
    from fwl_io.mirror import DataverseRetryableError

    _DataverseHandler.script = {('GET', '/api/datasets/:persistentId'): ['challenge'] * 5}
    with pytest.raises(DataverseRetryableError, match='state check'):
        _mirror(http_server, dataverse_server, publish=True)
    assert not any(c['path'].endswith('/actions/:publish') for c in _DataverseHandler.calls)
    assert _DataverseHandler.deleted
    assert 'not confirmed' not in caplog.text


def test_a_rejection_after_a_lost_publish_reply_keeps_the_dataset(
    http_server, dataverse_server, sleeps, caplog
):
    """A 504 is not resent: the state is polled, and a dataset still in DRAFT is kept."""
    _DataverseHandler.script = {('POST', '/actions/:publish'): [504, 409]}
    with pytest.raises(DataversePublishUnconfirmed, match='504'):
        _mirror(http_server, dataverse_server, publish=True)
    assert not any(c['method'] == 'DELETE' for c in _DataverseHandler.calls)
    assert sum(c['path'].endswith('/actions/:publish') for c in _DataverseHandler.calls) == 1
    assert sleeps == [30.0, 60.0, 120.0, 240.0]
    assert 'not confirmed' in caplog.text


@pytest.mark.unit
def test_a_connection_error_is_retried_but_a_certificate_error_is_not(sleeps):
    """A dropped connection may hide a request that arrived; a bad certificate never succeeds."""
    import requests

    def run(first):
        replies = [first, _fake_response(404, b'{"status": "ERROR"}')]

        def fake(*args, **kwargs):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        orig = requests.request
        requests.request = fake
        try:
            DataverseClient('http://unused', 'tok').delete_draft('doi:10.34894/X')
        finally:
            requests.request = orig

    run(requests.ConnectionError('connection reset'))
    assert sleeps == [30.0]
    # A TLS error that is not a certificate failure (here an EOF) is retried.
    run(requests.exceptions.SSLError('EOF occurred in violation of protocol'))
    assert sleeps == [30.0, 30.0]
    # A connection that breaks in the middle of the reply body is retried too.
    run(requests.exceptions.ChunkedEncodingError('connection broken mid-body'))
    assert sleeps == [30.0, 30.0, 30.0]
    # The chain requests raises for a failed certificate check is final.
    reason = urllib3.exceptions.SSLError(
        ssl.SSLCertVerificationError(1, 'certificate verify failed')
    )
    cert = requests.exceptions.SSLError(
        urllib3.exceptions.MaxRetryError(None, 'https://dataverse.nl', reason=reason)
    )
    with pytest.raises(DataverseError, match='certificate'):
        run(cert)
    assert sleeps == [30.0, 30.0, 30.0]


def test_a_draft_file_in_a_folder_or_without_a_name_counts_as_extra(
    http_server, dataverse_server, sleeps
):
    """Listed files are keyed by folder and name, and an unnamed entry is reported, not a crash."""
    _DataverseHandler.draft_files = [
        {'filename': 'a.dat', 'directoryLabel': 'sub', 'filesize': 4},
        {'filesize': 1},
    ]
    with pytest.raises(
        DataverseError, match=r"not expected \['', 'sub/a.dat'\].*types listed: \['MD5', 'None'\]"
    ):
        _mirror(http_server, dataverse_server)
    assert _DataverseHandler.deleted


def _raise(exc):
    def reply(*args, **kwargs):
        raise exc

    return reply


@pytest.mark.unit
@pytest.mark.parametrize(
    'post_reply',
    [
        _raise(requests_exceptions.SSLError('EOF occurred in violation of protocol')),
        _raise(requests_exceptions.ChunkedEncodingError('connection broken mid-body')),
        lambda *args, **kwargs: _fake_response(200, b'<html>accepted</html>'),
        lambda *args, **kwargs: _fake_response(500, b'{"status": "ERROR"}'),
    ],
    ids=['ssl-eof', 'chunked', 'non-json-2xx', 'server-error'],
)
def test_a_publish_reply_other_than_a_4xx_leaves_the_publish_unconfirmed(sleeps, post_reply):
    """Only a 4xx shows a publish had no effect; any other reply or failure is unconfirmed."""
    import requests

    seen = []
    orig = requests.request
    requests.request = _publish_route(seen, post_reply)
    try:
        with pytest.raises(DataversePublishUnconfirmed, match=r'request sent \d time'):
            DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert 'POST' in seen


@pytest.mark.unit
def test_a_429_then_a_rejection_is_a_known_rejection(sleeps):
    """A 429 means the request was not processed, so a 400 after it is a plain rejection."""
    import requests

    replies = [_fake_response(429, b'{}'), _fake_response(400, b'{"message": "rejected"}')]
    replies[0].headers['Retry-After'] = '7'
    seen = []
    orig = requests.request
    requests.request = _publish_route(seen, lambda *args, **kwargs: replies.pop(0))
    try:
        with pytest.raises(DataverseError, match='rejected') as info:
            DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert not isinstance(info.value, DataversePublishUnconfirmed)
    assert sleeps == [7.0]


@pytest.mark.unit
@pytest.mark.parametrize(
    ('status', 'wait', 'expected'),
    [
        (429, '7', 7.0),
        (429, '0', 0.0),
        (429, '900', 300.0),
        (429, 'soon', 30.0),
        (429, '\u00b2', 30.0),
        (503, '1', 30.0),
    ],
)
def test_a_429_waits_as_told_up_to_the_cap(sleeps, status, wait, expected):
    """A 429's Retry-After in seconds sets the wait (cap 300 s); anything else uses the backoff."""
    import requests

    first = _fake_response(status, b'{}')
    first.headers['Retry-After'] = wait
    replies = [first, _fake_response(404, b'{"status": "ERROR"}')]
    orig = requests.request
    requests.request = lambda *args, **kwargs: replies.pop(0)
    try:
        DataverseClient('http://unused', 'tok').delete_draft('doi:10.34894/X')
    finally:
        requests.request = orig
    assert sleeps == [expected]


@pytest.mark.unit
@pytest.mark.parametrize('state', [b'{"data": null}', b'{"data": {"latestVersion": null}}'])
def test_a_state_without_a_version_counts_as_unpublished(state):
    """A state reply without data or latestVersion does not crash the publish."""
    import requests

    seen = []

    released = b'{"data": {"latestVersion": {"versionState": "RELEASED"}}}'

    def route(method, *args, **kwargs):
        seen.append(method)
        if method == 'GET':
            return _fake_response(200, released if 'POST' in seen else state)
        return _fake_response(200, b'{"status": "OK"}')

    orig = requests.request
    requests.request = route
    try:
        DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert seen == ['GET', 'POST', 'GET']


@pytest.mark.unit
def test_an_unconfirmed_publish_says_how_often_it_was_sent(sleeps):
    """One lost publish reply and five failed state checks: the error says it was sent once."""
    import requests

    page = _fake_response(200, b'<html><title>Oh noes!</title></html>')
    page.headers['Content-Type'] = 'text/html'
    draft = _fake_response(200, b'{"data": {"latestVersion": {"versionState": "DRAFT"}}}')
    replies = [draft, _fake_response(504, b'{}')] + [page] * 5
    orig = requests.request
    requests.request = lambda *args, **kwargs: replies.pop(0)
    try:
        with pytest.raises(DataversePublishUnconfirmed, match=r'request sent 1 time\(s\)'):
            DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert replies == []


@pytest.mark.unit
def test_any_error_after_an_unclear_publish_keeps_it_unconfirmed(sleeps, monkeypatch):
    """After a lost publish reply, even an unexpected error in the state check is unconfirmed."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    checks = [False]

    def released(persistent_id):
        if checks:
            return checks.pop()
        raise KeyError('versionState')

    monkeypatch.setattr(client, '_released', released)
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(504, b'{}')
    try:
        with pytest.raises(DataversePublishUnconfirmed, match='versionState'):
            client.publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert sleeps == [30.0, 60.0, 120.0, 240.0]


@pytest.mark.unit
@pytest.mark.timeout(10)
def test_the_certificate_check_ends_on_a_cyclic_exception_chain():
    """An exception chain that loops back on itself is walked once, not forever."""
    from fwl_io.mirror import _cert_failure

    first = ValueError('outer')
    second = KeyError(first)
    first.__context__ = second
    assert _cert_failure(first) is False
    second.__cause__ = ssl.SSLCertVerificationError(1, 'certificate verify failed')
    assert _cert_failure(first) is True


@pytest.mark.unit
def test_an_unexpected_error_after_the_publish_request_keeps_it_unconfirmed(sleeps):
    """A reply that breaks the JSON parser in an unexpected way is not a known rejection."""
    import requests

    def post_reply(*args, **kwargs):
        raise RecursionError('maximum recursion depth exceeded')

    seen = []
    orig = requests.request
    requests.request = _publish_route(seen, post_reply)
    try:
        with pytest.raises(DataversePublishUnconfirmed, match='recursion'):
            DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    finally:
        requests.request = orig
    assert seen == ['GET', 'POST'] + ['GET'] * 5


def _state(state):
    body = {'status': 'OK', 'data': {'latestVersion': {'versionState': state}}}
    return _fake_response(200, json.dumps(body).encode())


@pytest.mark.unit
@pytest.mark.parametrize(
    'reply',
    [
        (200, b''),
        (204, b''),
        (200, b'{"status": "ERROR"}'),
        (202, b'{"status": "WORKFLOW_IN_PROGRESS"}'),
    ],
)
def test_an_accepted_publish_is_polled_until_released(sleeps, monkeypatch, reply):
    """A 2xx of any body is not a publish until the state is RELEASED; the POST is sent once."""
    import requests

    seen = []
    route = _publish_route(
        seen,
        lambda *args, **kwargs: _fake_response(*reply),
        ('DRAFT', 'DRAFT', 'DRAFT', 'RELEASED'),
    )
    monkeypatch.setattr(requests, 'request', route)
    DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    assert seen == ['GET', 'POST', 'GET', 'GET', 'GET']
    assert sleeps == [30.0, 60.0]


@pytest.mark.unit
def test_an_accepted_publish_that_stays_a_draft_is_unconfirmed(sleeps, monkeypatch):
    """A 2xx and a DRAFT state until the wait limit: unconfirmed, the POST sent once."""
    import requests

    seen = []
    route = _publish_route(seen, lambda *args, **kwargs: _fake_response(200, b'{"status": "OK"}'))
    monkeypatch.setattr(requests, 'request', route)
    with pytest.raises(DataversePublishUnconfirmed, match=r'sent 1 time\(s\).*450 s'):
        DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    assert seen == ['GET', 'POST'] + ['GET'] * 5


def test_an_accepted_publish_that_stays_a_draft_keeps_the_dataset(
    http_server, dataverse_server, sleeps
):
    """A 202 with an error body and no RELEASED state: the mirror keeps the dataset."""
    _DataverseHandler.script = {('POST', '/actions/:publish'): [202]}
    with pytest.raises(DataversePublishUnconfirmed):
        _mirror(http_server, dataverse_server, publish=True)
    calls = _DataverseHandler.calls
    assert not any(c['method'] == 'DELETE' for c in calls)
    assert sum(c['path'].endswith('/actions/:publish') for c in calls) == 1


@pytest.mark.unit
def test_a_timeout_on_publish_is_polled_not_resent(sleeps, monkeypatch):
    """A publish timeout, then RELEASED on the third state check: done, the POST sent once."""
    import requests

    def post_reply(*args, **kwargs):
        raise requests_exceptions.ReadTimeout('read timed out')

    seen = []
    route = _publish_route(seen, post_reply, ('DRAFT', 'DRAFT', 'DRAFT', 'RELEASED'))
    monkeypatch.setattr(requests, 'request', route)
    DataverseClient('http://unused', 'tok').publish('doi:10.34894/DEMO01')
    assert seen == ['GET', 'POST', 'GET', 'GET', 'GET']
    assert sleeps == [30.0, 60.0]


def test_a_publish_answered_by_the_bot_check_page_is_sent_again(
    http_server, dataverse_server, sleeps
):
    """The bot-check page shows the publish was not processed, so it is sent again."""
    _DataverseHandler.script = {('POST', '/actions/:publish'): ['challenge']}
    result, calls = _mirror(http_server, dataverse_server, publish=True)
    assert result == 'doi:10.34894/DEMO01'
    assert sum(c['path'].endswith('/actions/:publish') for c in calls) == 2
    assert sleeps == [30.0]


@pytest.mark.parametrize('page', ['challenge 403', 'challenge'])
def test_a_publish_that_only_gets_the_bot_check_page_keeps_the_dataset(
    http_server, dataverse_server, sleeps, page
):
    """The bot-check page on every attempt, at any status: unconfirmed, never deleted."""
    _DataverseHandler.script = {('POST', '/actions/:publish'): [page] * 5}
    with pytest.raises(DataversePublishUnconfirmed, match='bot-check page'):
        _mirror(http_server, dataverse_server, publish=True)
    calls = _DataverseHandler.calls
    assert not any(c['method'] == 'DELETE' for c in calls)
    assert sum(c['path'].endswith('/actions/:publish') for c in calls) == 5


@pytest.mark.parametrize('page', ['proxy 404', 'challenge 404'])
def test_a_404_page_after_a_failed_delete_is_not_a_rollback(
    http_server, dataverse_server, sleeps, caplog, page
):
    """A 404 page from a proxy or the bot-check page does not show that the draft is gone."""
    _DataverseHandler.fail_on_add = True
    _DataverseHandler.fail_on_delete = True
    _DataverseHandler.script = {
        ('DELETE', '/api/datasets/:persistentId'): ['challenge'],
        ('GET', '/api/datasets/:persistentId'): [page] * 5,
    }
    with pytest.raises(DataverseError):
        _mirror(http_server, dataverse_server)
    assert 'rolled back the draft' not in caplog.text
    assert 'could not roll back' in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize(
    ('content_type', 'body', 'gone'),
    [
        ('application/json', b'{"status": "ERROR", "message": "not found"}', True),
        ('application/json', b'{"message": "not found"}', False),
        ('text/html', b'<html><title>Not Found</title></html>', False),
    ],
)
def test_only_a_dataverse_404_counts_as_deleted(monkeypatch, content_type, body, gone):
    """_deleted accepts a 404 only with a Dataverse JSON error body."""
    import requests

    response = _fake_response(404, body)
    response.headers['Content-Type'] = content_type
    monkeypatch.setattr(requests, 'request', lambda *args, **kwargs: response)
    client = DataverseClient('http://unused', 'tok')
    if gone:
        assert client._deleted('doi:10.34894/DEMO01')
    else:
        with pytest.raises(DataverseError, match='404'):
            client._deleted('doi:10.34894/DEMO01')


def test_an_already_published_dataset_is_not_deleted(http_server, dataverse_server, caplog):
    """A dataset found RELEASED before the publish request is kept."""
    from fwl_io.mirror import DataverseAlreadyPublished

    _DataverseHandler.released = True
    with pytest.raises(DataverseAlreadyPublished):
        _mirror(http_server, dataverse_server, publish=True)
    calls = _DataverseHandler.calls
    assert not any(c['method'] == 'DELETE' for c in calls)
    assert not any(c['path'].endswith('/actions/:publish') for c in calls)
    assert 'check its state by hand' in caplog.text


@pytest.mark.unit
def test_a_file_listed_twice_in_the_draft_is_a_fault(tmp_path, monkeypatch):
    """A draft that lists a path twice fails the check, even when both entries match."""
    import requests

    target = tmp_path / 'a.dat'
    target.write_bytes(b'AAA\n')
    entry = {
        'filename': 'a.dat',
        'filesize': 4,
        'checksum': {'type': 'MD5', 'value': hashlib.md5(b'AAA\n').hexdigest()},
    }
    body = json.dumps({'status': 'OK', 'data': [{'dataFile': entry}, {'dataFile': entry}]})
    monkeypatch.setattr(
        requests, 'request', lambda *args, **kwargs: _fake_response(200, body.encode())
    )
    with pytest.raises(DataverseError, match=r"\['a.dat'\] more than once"):
        DataverseClient('http://unused', 'tok').check_draft(
            'doi:10.34894/DEMO01', {'a.dat': target}
        )


@pytest.mark.unit
def test_a_proxy_error_is_retried(sleeps, monkeypatch):
    """A proxy failure is a lost connection: the call is tried again."""
    import requests

    replies = [
        requests_exceptions.ProxyError('proxy refused the connection'),
        _fake_response(200, b''),
    ]

    def route(method, *args, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = DataverseClient('http://unused', 'tok')
    monkeypatch.setattr(requests, 'request', route)
    monkeypatch.setattr(client, '_deleted', lambda persistent_id: False)
    client.delete_draft('doi:10.34894/DEMO01')
    assert replies == []
    assert sleeps == [30.0]


def test_the_created_dataset_is_logged_as_a_warning(http_server, dataverse_server, caplog):
    """The DOI of the created dataset reaches the default log handler."""
    import logging

    _mirror(http_server, dataverse_server, publish=False)
    created = [r for r in caplog.records if 'created Dataverse dataset' in r.getMessage()]
    assert [r.levelno for r in created] == [logging.WARNING]
    assert 'doi:10.34894/DEMO01' in created[0].getMessage()


def test_an_interrupted_mirror_keeps_the_dataset(
    http_server, dataverse_server, caplog, monkeypatch
):
    """An interrupt after the create logs the DOI and deletes nothing."""

    def interrupt(self, persistent_id, files):
        raise KeyboardInterrupt

    monkeypatch.setattr(DataverseClient, 'check_draft', interrupt)
    with pytest.raises(KeyboardInterrupt):
        _mirror(http_server, dataverse_server, publish=True)
    assert not any(c['method'] == 'DELETE' for c in _DataverseHandler.calls)
    assert 'doi:10.34894/DEMO01 interrupted' in caplog.text
    assert 'check its state by hand' in caplog.text
