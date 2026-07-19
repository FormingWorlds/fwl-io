import pytest

from fwl_io.manifest import load_manifest, shared_manifest_path

pytestmark = pytest.mark.unit

GOOD = """
[interior_lookup_tables.demo_eos]
name = "Demo equation of state"
subdir = "interior_lookup_tables/demo_eos"
zenodo = "10.5281/zenodo.1234567"
dataverse = "10.34894/ABCDEF"
required_by = ["aragog", "spider"]

[spectral_files.demo_band]
subdir = "spectral_files/demo_band"
zenodo = "10.5281/zenodo.7654321"
"""


def _write(tmp_path, text):
    path = tmp_path / 'manifest.toml'
    path.write_text(text)
    return path


def test_nested_tables_load_with_dotted_keys(tmp_path):
    datasets = {ds.key: ds for ds in load_manifest(_write(tmp_path, GOOD))}
    assert set(datasets) == {'interior_lookup_tables.demo_eos', 'spectral_files.demo_band'}
    demo = datasets['interior_lookup_tables.demo_eos']
    assert demo.name == 'Demo equation of state'
    assert demo.required_by == ('aragog', 'spider')
    assert demo.registry_path.name == 'interior_lookup_tables.demo_eos.registry.txt'
    assert datasets['spectral_files.demo_band'].name == 'spectral_files.demo_band'


def test_missing_subdir_rejected(tmp_path):
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='missing required field "subdir"'):
        load_manifest(_write(tmp_path, bad))


def test_absolute_subdir_rejected(tmp_path):
    bad = '[g.d]\nsubdir = "/etc/data"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='must be relative'):
        load_manifest(_write(tmp_path, bad))


def test_dataset_without_any_doi_rejected(tmp_path):
    bad = '[g.d]\nsubdir = "g/d"\n'
    with pytest.raises(ValueError, match='at least one of'):
        load_manifest(_write(tmp_path, bad))


def test_non_doi_value_rejected(tmp_path):
    bad = '[g.d]\nsubdir = "g/d"\nzenodo = "https://zenodo.org/records/1"\n'
    with pytest.raises(ValueError, match='is not a DOI'):
        load_manifest(_write(tmp_path, bad))


def test_missing_registry_gives_actionable_error(tmp_path):
    ds = load_manifest(_write(tmp_path, GOOD))[0]
    with pytest.raises(FileNotFoundError, match='fwl-io sync'):
        ds.registry()


@pytest.mark.smoke
def test_shared_manifest_ships_and_parses():
    path = shared_manifest_path()
    assert path.is_file()
    assert load_manifest(path) == []
