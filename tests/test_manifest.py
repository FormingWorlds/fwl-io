import json

import pooch
import pytest

from fwl_io.manifest import (
    Dataset,
    discover_manifests,
    fetch_for,
    load_manifest,
    shared_manifest_path,
)

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


def test_unknown_extract_kind_rejected(tmp_path):
    """Only the archive kinds the fetcher can unpack are accepted at load time."""
    bad = '[g.d]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\nextract = "rar"\n'
    with pytest.raises(ValueError, match='extract value'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: a supported kind loads and is carried onto the dataset.
    good = '[g.d]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\nextract = "tar"\n'
    assert load_manifest(_write(tmp_path, good))[0].extract == 'tar'


def test_missing_registry_gives_actionable_error(tmp_path):
    ds = load_manifest(_write(tmp_path, GOOD))[0]
    with pytest.raises(FileNotFoundError, match='fwl-io sync'):
        ds.registry()


@pytest.mark.smoke
def test_shared_manifest_ships_and_parses_empty():
    """The shared manifest ships with the package and parses cleanly.

    It declares no datasets yet: nothing is consumed by several models, and the
    Baraffe tracks now ship with the MORS package. A comment-only manifest is a
    valid one, and parsing it must yield an empty dataset list rather than
    raising.
    """
    path = shared_manifest_path()
    assert path.is_file()
    # The file still carries content (the machinery header), so an empty parse
    # is a deliberate no-datasets result, not a truncated or missing file.
    assert path.read_text().strip()
    datasets = load_manifest(path)
    assert datasets == []


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


def _seed_versioned_dataset(root, subdir, recid, files, required_by):
    """Place files under root/subdir/r<recid>/ and return a matching Dataset."""
    version_dir = root / subdir / f'r{recid}'
    version_dir.mkdir(parents=True)
    registry = {}
    for name, payload in files.items():
        (version_dir / name).write_bytes(payload)
        registry[name] = 'sha256:' + pooch.file_hash(str(version_dir / name), alg='sha256')
    registry_path = root / f'{subdir.replace("/", ".")}.registry.txt'
    registry_path.write_text('\n'.join(f'{n} {h}' for n, h in registry.items()) + '\n')
    return version_dir, Dataset(
        key=subdir.replace('/', '.'),
        name=subdir,
        subdir=subdir,
        zenodo=f'10.5281/zenodo.{recid}',
        required_by=required_by,
        registry_path=registry_path,
    )


def test_fetch_for_stamps_each_required_dataset_and_skips_others(tmp_path, monkeypatch):
    """fetch_for fetches and stamps only the datasets a model requires."""
    data_root = tmp_path / 'data'
    wanted_dir, wanted = _seed_versioned_dataset(
        data_root, 'star/tracks/demo', '111', {'a.dat': b'A\n', 'b.dat': b'BB\n'}, ('mymodel',)
    )
    _, other = _seed_versioned_dataset(
        data_root, 'interior/eos/demo', '222', {'c.dat': b'C\n'}, ('someone_else',)
    )

    monkeypatch.setattr('fwl_io.manifest.discover_manifests', lambda: {'prov': [wanted, other]})
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')  # all files pre-seeded; no network

    fetched = fetch_for('mymodel', data_root=data_root)

    # Only the required dataset is returned, at its versioned paths.
    assert set(fetched) == {wanted.key}
    assert sorted(p.name for p in fetched[wanted.key]) == ['a.dat', 'b.dat']
    # The production path writes the stamp for the required dataset only.
    stamp = wanted_dir / '.fwl-io.json'
    assert stamp.is_file()
    assert json.loads(stamp.read_text())['record_id'] == '111'
    assert not (data_root / 'interior/eos/demo/r222/.fwl-io.json').exists()
