import json

import pytest

from fwl_io.manifest import load_manifest
from fwl_io.registry import load_registry
from fwl_io.sync import fetch_zenodo_registry, sync_manifest, zenodo_record_id

pytestmark = pytest.mark.integration


def _serve_record(root, recid, payload):
    api_dir = root / 'api' / 'records'
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / str(recid)).write_text(json.dumps(payload))


VERSION_RECORD = {
    'id': 1234567,
    'conceptrecid': '1234566',
    'files': [
        {'key': 'alpha.dat', 'checksum': 'md5:aaa111'},
        {'key': 'beta.dat', 'checksum': 'md5:bbb222'},
    ],
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ('doi', 'recid'),
    [('10.5281/zenodo.42', '42'), ('doi:10.5281/zenodo.1234567', '1234567')],
)
def test_record_id_extraction(doi, recid):
    assert zenodo_record_id(doi) == recid


@pytest.mark.unit
def test_non_zenodo_doi_rejected():
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        zenodo_record_id('10.34894/ABCDEF')


def test_registry_fetched_from_version_doi(http_server):
    base_url, root = http_server
    _serve_record(root, 1234567, VERSION_RECORD)
    registry = fetch_zenodo_registry('10.5281/zenodo.1234567', api_base=f'{base_url}api/records')
    assert registry == {'alpha.dat': 'md5:aaa111', 'beta.dat': 'md5:bbb222'}


def test_inveniordm_files_entries_shape_supported(http_server):
    base_url, root = http_server
    record = {
        'id': 42,
        'conceptrecid': '41',
        'files': {'entries': {'gamma.dat': {'checksum': 'md5:ccc333'}}},
    }
    _serve_record(root, 42, record)
    registry = fetch_zenodo_registry('10.5281/zenodo.42', api_base=f'{base_url}api/records')
    assert registry == {'gamma.dat': 'md5:ccc333'}


def test_concept_doi_rejected_by_conceptrecid(http_server):
    base_url, root = http_server
    concept = dict(VERSION_RECORD, id=1234566, conceptrecid='1234566')
    _serve_record(root, 1234566, concept)
    with pytest.raises(ValueError, match='concept DOI'):
        fetch_zenodo_registry('10.5281/zenodo.1234566', api_base=f'{base_url}api/records')


def test_concept_doi_rejected_by_id_mismatch(http_server):
    # The live API answers a concept-recid query with a redirect to the newest
    # version, so the returned id differs from the requested one.
    base_url, root = http_server
    _serve_record(root, 7777, dict(VERSION_RECORD, id=8888, conceptrecid='7777x'))
    with pytest.raises(ValueError, match='concept DOI'):
        fetch_zenodo_registry('10.5281/zenodo.7777', api_base=f'{base_url}api/records')


def test_record_without_files_rejected(http_server):
    base_url, root = http_server
    _serve_record(root, 99, {'id': 99, 'conceptrecid': '98', 'files': []})
    with pytest.raises(ValueError, match='no files'):
        fetch_zenodo_registry('10.5281/zenodo.99', api_base=f'{base_url}api/records')


def test_sync_manifest_writes_committed_registry(http_server, tmp_path):
    base_url, root = http_server
    _serve_record(root, 1234567, VERSION_RECORD)
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text('[interior_lookup_tables.demo]\nzenodo = "10.5281/zenodo.1234567"\n')
    written = sync_manifest(manifest, api_base=f'{base_url}api/records')
    assert written == [tmp_path / 'interior_lookup_tables.demo.registry.txt']
    assert load_registry(written[0]) == {'alpha.dat': 'md5:aaa111', 'beta.dat': 'md5:bbb222'}
    # the regenerated registry is what the dataset object now resolves
    assert load_manifest(manifest)[0].registry() == load_registry(written[0])


def test_sync_manifest_partial_failure_writes_the_rest(http_server, tmp_path):
    base_url, root = http_server
    _serve_record(root, 1234567, VERSION_RECORD)  # record 999 is NOT served -> 404
    manifest = tmp_path / 'manifest.toml'
    manifest.write_text(
        '[g.good]\nzenodo = "10.5281/zenodo.1234567"\n[g.bad]\nzenodo = "10.5281/zenodo.999"\n'
    )
    with pytest.raises(RuntimeError, match=r'g\.bad'):
        sync_manifest(manifest, api_base=f'{base_url}api/records')
    assert (tmp_path / 'g.good.registry.txt').is_file(), 'good dataset still synced'
    assert not (tmp_path / 'g.bad.registry.txt').exists()
