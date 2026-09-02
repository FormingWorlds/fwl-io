"""Tests for the Zenodo-to-Dataverse mirror.

The Zenodo side is exercised against the local ``http_server`` fixture (record
JSON plus the actual files, checksum-verified by the real fetcher). The
Dataverse side runs against a mock native-API server that records every
request, so the create/upload/publish orchestration and the request shapes are
asserted without touching a real Dataverse installation.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pooch
import pytest

from fwl_io.mirror import (
    DataverseClient,
    DataverseError,
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

    def _reply(self, status, payload):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        parsed = self._record('POST')
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
                self._reply(200, {'status': 'OK', 'data': {'files': [{'label': 'ok'}]}})
        elif parsed.path.endswith('/actions/:publish'):
            if self.fail_on_publish:
                self._reply(400, {'status': 'ERROR', 'message': 'publish rejected'})
                return
            self._reply(200, {'status': 'OK', 'data': {'id': 7}})
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):  # noqa: N802 -- BaseHTTPRequestHandler API
        self._record('DELETE')
        if self.fail_on_delete:
            self._reply(500, {'status': 'ERROR', 'message': 'delete failed'})
        else:
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
    assert paths.index('/api/datasets/:persistentId/actions/:publish') == len(paths) - 1
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
    """publish does not raise when Dataverse returns 2xx with an empty body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'')
    try:
        client.publish('doi:10.34894/DEMO01')  # must not raise
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_raises_with_the_status_and_body_on_a_non_json_success_body():
    """publish raises on a 2xx non-JSON body, naming the status and body."""
    import requests

    client = DataverseClient('http://unused', 'tok')
    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'this is not json')
    try:
        with pytest.raises(DataverseError, match='this is not json') as exc_info:
            client.publish('doi:10.34894/DEMO01')
        assert '200' in str(exc_info.value)
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
    requests.request = lambda *args, **kwargs: _fake_response(
        403, b'{"status": "ERROR", "message": "Dataset already published"}'
    )
    try:
        with pytest.raises(DataverseError, match='already published') as exc_info:
            publish_existing_dataverse_draft(
                'doi:10.34894/DEMO01', dataverse_url='http://unused', token='tok'
            )
        assert '403' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
def test_publish_existing_draft_raises_with_the_status_and_body_on_a_non_json_success_body():
    """A 2xx publish response with a non-JSON body raises, naming the status and body."""
    import requests

    orig = requests.request
    requests.request = lambda *args, **kwargs: _fake_response(200, b'this is not json')
    try:
        with pytest.raises(DataverseError, match='this is not json') as exc_info:
            publish_existing_dataverse_draft(
                'doi:10.34894/DEMO01', dataverse_url='http://unused', token='tok'
            )
        assert '200' in str(exc_info.value)
    finally:
        requests.request = orig


@pytest.mark.unit
@pytest.mark.parametrize(
    'persistent_id',
    ['', '10.34894/DEMO01', 'doi:', 'doi:noSlashHere', 'doi:10.34894/', 'doi:/DEMO01'],
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
