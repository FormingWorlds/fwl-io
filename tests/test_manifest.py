import pytest

from fwl_io.manifest import discover_manifests, load_manifest, shared_manifest_path

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


@pytest.mark.parametrize('subdir', ['../outside', 'a/../../b', 'a\\\\b'])
def test_traversal_subdir_rejected(tmp_path, subdir):
    bad = f'[g.d]\nsubdir = "{subdir}"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError):
        load_manifest(_write(tmp_path, bad))


def test_dataset_without_zenodo_rejected(tmp_path):
    bad = '[g.d]\nsubdir = "g/d"\ndataverse = "10.34894/XYZ"\n'
    with pytest.raises(ValueError, match='Zenodo version DOI is required'):
        load_manifest(_write(tmp_path, bad))


@pytest.mark.parametrize(
    'value', ['https://zenodo.org/records/1', '10.1234/other.repo', '10.5281/zenodo.abc']
)
def test_non_zenodo_doi_rejected(tmp_path, value):
    bad = f'[g.d]\nsubdir = "g/d"\nzenodo = "{value}"\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))


def test_bad_dataverse_doi_rejected(tmp_path):
    bad = '[g.d]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\ndataverse = "not-a-doi"\n'
    with pytest.raises(ValueError, match='is not a DOI'):
        load_manifest(_write(tmp_path, bad))


def test_dataset_with_subtable_rejected_not_silently_dropped(tmp_path):
    bad = '[g.d]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\n[g.d.meta]\nauthor = "someone"\n'
    with pytest.raises(ValueError, match='must not contain sub-tables'):
        load_manifest(_write(tmp_path, bad))


def test_array_of_tables_rejected_not_silently_dropped(tmp_path):
    bad = '[[g.d]]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='arrays of tables'):
        load_manifest(_write(tmp_path, bad))


def test_missing_registry_gives_actionable_error(tmp_path):
    ds = load_manifest(_write(tmp_path, GOOD))[0]
    with pytest.raises(FileNotFoundError, match='fwl-io sync'):
        ds.registry()


@pytest.mark.smoke
def test_shared_manifest_ships_and_parses():
    path = shared_manifest_path()
    assert path.is_file()
    datasets = {ds.key: ds for ds in load_manifest(path)}
    baraffe = datasets['stellar_evolution_tracks.Baraffe']
    assert baraffe.subdir == 'stellar_evolution_tracks/Baraffe'
    assert baraffe.required_by == ('mors',)
    registry = baraffe.registry()  # the committed registry ships with the package
    assert len(registry) == 31
    assert registry['BHAC15-M0p010.txt'] == 'md5:7b2927f8cb983680692280344eee1d9a'


class _FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


def test_discovery_isolates_broken_providers(tmp_path, monkeypatch):
    good_manifest = _write(tmp_path, GOOD)

    def broken():
        raise ImportError('provider package is broken')

    eps = [
        _FakeEntryPoint('good-model', lambda: good_manifest),
        _FakeEntryPoint('broken-model', broken),
    ]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)

    found = discover_manifests()
    assert set(found) == {'good-model'}
    assert len(found['good-model']) == 2
