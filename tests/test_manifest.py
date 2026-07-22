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
zenodo = "10.5281/zenodo.1234567"
dataverse = "10.34894/ABCDEF"
required_by = ["aragog", "spider"]

[spectral_files.demo_band]
zenodo = "10.5281/zenodo.7654321"
"""


def _write(tmp_path, text):
    path = tmp_path / 'manifest.toml'
    path.write_text(text)
    return path


def test_nested_tables_load_with_dotted_keys(tmp_path):
    """A dataset table is identified by its Zenodo pin and keyed by its full path."""
    datasets = {ds.key: ds for ds in load_manifest(_write(tmp_path, GOOD))}
    assert set(datasets) == {'interior_lookup_tables.demo_eos', 'spectral_files.demo_band'}
    demo = datasets['interior_lookup_tables.demo_eos']
    assert demo.name == 'Demo equation of state'
    assert demo.required_by == ('aragog', 'spider')
    assert demo.registry_path.name == 'interior_lookup_tables.demo_eos.registry.txt'
    assert datasets['spectral_files.demo_band'].name == 'spectral_files.demo_band'


def test_subdir_is_derived_from_the_dotted_key(tmp_path):
    """The dataset location comes from the key, at the key's own depth."""
    manifest = '[star.tracks.baraffe_2015]\nzenodo = "10.5281/zenodo.15729114"\n'
    ds = load_manifest(_write(tmp_path, manifest))[0]
    assert ds.subdir == 'star/tracks/baraffe_2015'
    # Discrimination: a grouping level dropped, or only the leaf kept, would put
    # the data in a different directory, so both wrong derivations are excluded.
    assert ds.subdir != 'star/baraffe_2015'
    assert ds.subdir != 'baraffe_2015'


def test_explicit_subdir_rejected(tmp_path):
    """A manifest cannot declare a location that could drift from its key."""
    bad = '[g.d]\nsubdir = "somewhere/else"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, bad))


def test_subdir_on_a_grouping_table_rejected(tmp_path):
    """A location lifted up to a group level is refused, not quietly ignored."""
    bad = '[star]\nsubdir = "somewhere/else"\n[star.tracks_x]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: without the stray field the same shape loads at its key path.
    good = '[star]\n[star.tracks_x]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'star/tracks_x'


def test_keys_differing_only_in_case_rejected(tmp_path):
    """Two such keys share one directory and one registry file on macOS."""
    bad = '[g.Demo]\nzenodo = "10.5281/zenodo.1"\n[g.demo]\nzenodo = "10.5281/zenodo.2"\n'
    with pytest.raises(ValueError, match='differ only in case'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: keys that differ by more than case are independent datasets.
    good = '[g.demo_a]\nzenodo = "10.5281/zenodo.1"\n[g.demo_b]\nzenodo = "10.5281/zenodo.2"\n'
    assert len(load_manifest(_write(tmp_path, good))) == 2


def test_explicit_subdir_rejected_even_when_it_matches_the_key(tmp_path):
    """The field is refused outright, so no manifest can reintroduce the drift."""
    bad = '[g.d]\nsubdir = "g/d"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, bad))


@pytest.mark.parametrize(
    'segment',
    ['..', '.', 'a/b', 'a\\\\b', 'a.b', '', ' ', '-lead', 'naïve', 'demo\\n', '\\ttab', 'a+b'],
)
def test_unsafe_key_segment_rejected(tmp_path, segment):
    """A key segment that is not a plain directory name never reaches the data root."""
    bad = f'[g."{segment}"]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='table segment'):
        load_manifest(_write(tmp_path, bad))


def test_key_segment_rejects_a_trailing_control_character(tmp_path):
    """A newline is not a directory name, however the key spells it."""
    bad = '[g."demo\\n"]\nzenodo = "10.5281/zenodo.1234567"\n'
    with pytest.raises(ValueError, match='not a valid directory name'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: the same key without the control character loads, so the
    # rejection is about the newline and not about the segment 'demo'.
    good = '[g.demo]\nzenodo = "10.5281/zenodo.1234567"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'g/demo'


def test_quoted_dotted_segment_does_not_silently_deepen_the_path(tmp_path):
    """A dot inside a quoted key would add a directory level, so it is refused."""
    bad = '[g."a.b"]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='not a valid directory name'):
        load_manifest(_write(tmp_path, bad))
    # A directory name carrying a dot has no spelling: the unquoted key below is a
    # different structure, two nested directories rather than one named 'a.b'.
    nested = '[g.a.b]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, nested))[0].subdir == 'g/a/b'


def test_dataset_without_zenodo_rejected(tmp_path):
    bad = '[g.d]\ndataverse = "10.34894/XYZ"\n'
    with pytest.raises(ValueError, match='Zenodo version DOI is required'):
        load_manifest(_write(tmp_path, bad))


def test_empty_zenodo_value_rejected_as_a_missing_pin(tmp_path):
    """An empty pin is a missing pin, and says so rather than naming a bad DOI."""
    bad = '[g.d]\nzenodo = ""\n'
    with pytest.raises(ValueError, match='Zenodo version DOI is required') as excinfo:
        load_manifest(_write(tmp_path, bad))
    # Discrimination: the empty value takes the missing-pin branch, not the
    # malformed-DOI branch, so the message tells the author to add a pin.
    assert 'not a Zenodo DOI' not in str(excinfo.value)


@pytest.mark.parametrize(
    'value', ['https://zenodo.org/records/1', '10.1234/other.repo', '10.5281/zenodo.abc']
)
def test_non_zenodo_doi_rejected(tmp_path, value):
    bad = f'[g.d]\nzenodo = "{value}"\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))


def test_non_string_zenodo_rejected(tmp_path):
    """A bare number is not a DOI, and must fail as a manifest error."""
    bad = '[g.d]\nzenodo = 15729114\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))


@pytest.mark.parametrize('value', ['"not-a-doi"', 'false', '0', '""'])
def test_bad_dataverse_doi_rejected(tmp_path, value):
    """A mirror DOI is checked by type first, so a falsy value cannot slip past."""
    bad = f'[g.d]\nzenodo = "10.5281/zenodo.1"\ndataverse = {value}\n'
    with pytest.raises(ValueError, match='is not a DOI'):
        load_manifest(_write(tmp_path, bad))


def test_dataset_without_a_mirror_still_loads(tmp_path):
    """An absent mirror is the normal case and stays absent, not falsified."""
    ds = load_manifest(_write(tmp_path, '[g.d]\nzenodo = "10.5281/zenodo.1"\n'))[0]
    assert ds.dataverse is None
    assert ds.zenodo == '10.5281/zenodo.1'


def test_trailing_newline_in_a_doi_rejected(tmp_path):
    """A DOI carrying a line break is not a DOI, and never reaches the fetcher."""
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1234567\\n"\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))


def test_dataset_rejects_positional_construction():
    """Fields are keyword-only, so a stale positional call cannot rebind them."""
    with pytest.raises(TypeError):
        Dataset('g.d', 'demo', '10.5281/zenodo.1')
    # The keyword form is the supported one and derives the location from the key.
    ds = Dataset(key='g.d', name='demo', zenodo='10.5281/zenodo.1')
    assert ds.subdir == 'g/d'


def test_required_by_string_rejected(tmp_path):
    """A bare string would split into characters and match no model at all."""
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nrequired_by = "mors"\n'
    with pytest.raises(ValueError, match='must be a list of model names'):
        load_manifest(_write(tmp_path, bad))


def test_dataset_with_subtable_rejected_not_silently_dropped(tmp_path):
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\n[g.d.meta]\nauthor = "someone"\n'
    with pytest.raises(ValueError, match='must not contain sub-tables'):
        load_manifest(_write(tmp_path, bad))


def test_array_of_tables_rejected_not_silently_dropped(tmp_path):
    bad = '[[g.d]]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='arrays of tables'):
        load_manifest(_write(tmp_path, bad))


def test_top_level_array_of_tables_rejected(tmp_path):
    """An array at the outermost level is refused too, not just a nested one."""
    bad = '[[d]]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='arrays of tables'):
        load_manifest(_write(tmp_path, bad))


def test_top_level_scalars_are_ignored(tmp_path):
    """A manifest may carry its own settings beside its dataset tables."""
    manifest = 'schema_version = 1\n[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    datasets = load_manifest(_write(tmp_path, manifest))
    # The scalar is skipped rather than read as a malformed dataset table.
    assert [ds.key for ds in datasets] == ['g.d']
    assert datasets[0].subdir == 'g/d'


def test_unknown_extract_kind_rejected(tmp_path):
    """Only the archive kinds the fetcher can unpack are accepted at load time."""
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nextract = "rar"\n'
    with pytest.raises(ValueError, match='extract value'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: a supported kind loads and is carried onto the dataset.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\nextract = "tar"\n'
    assert load_manifest(_write(tmp_path, good))[0].extract == 'tar'


def test_missing_registry_gives_actionable_error(tmp_path):
    ds = load_manifest(_write(tmp_path, GOOD))[0]
    with pytest.raises(FileNotFoundError, match='fwl-io sync'):
        ds.registry()


@pytest.mark.smoke
def test_shared_manifest_ships_and_parses_empty():
    """The shared manifest ships with the package and parses cleanly.

    It declares no datasets: nothing is consumed by several models, and the
    Baraffe tracks ship with the MORS package. A comment-only manifest is a
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


def test_fetch_for_reports_an_unreadable_manifest_instead_of_nothing(tmp_path, monkeypatch):
    """An empty result while a manifest is unreadable names that manifest."""
    stale = _write(
        tmp_path, '[star.tracks.demo]\nsubdir = "star/tracks/demo"\nzenodo = "10.5281/zenodo.1"\n'
    )
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_FakeEntryPoint('stale-model', lambda: stale)],
    )
    with pytest.raises(RuntimeError, match='stale-model') as excinfo:
        fetch_for('anymodel', data_root=tmp_path / 'data')
    # The provider's own diagnosis is carried through, not just the provider name.
    assert '"subdir" is not a manifest field' in str(excinfo.value)


def test_fetch_for_reports_an_unreadable_manifest_beside_the_data_it_did_fetch(
    tmp_path, monkeypatch
):
    """A model served by two manifests hears about the one that failed."""
    data_root = tmp_path / 'data'
    _, wanted = _seed_versioned_dataset(
        data_root, 'star/tracks/demo', '111', {'a.dat': b'A\n'}, ('mymodel',)
    )
    monkeypatch.setattr(
        'fwl_io.manifest._discover',
        lambda: ({'shared': [wanted]}, {'mymodel': 'unreadable manifest'}),
    )
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')  # the file is pre-seeded; no network

    with pytest.raises(RuntimeError, match='unreadable manifest') as excinfo:
        fetch_for('mymodel', data_root=data_root)
    # The partial result is reported too, so the user knows what did arrive.
    assert '1 dataset(s) arrived' in str(excinfo.value)
    # Discrimination: the same call without the broken provider returns the data.
    monkeypatch.setattr('fwl_io.manifest._discover', lambda: ({'shared': [wanted]}, {}))
    fetched = fetch_for('mymodel', data_root=data_root)
    assert [p.name for p in fetched[wanted.key]] == ['a.dat']


def test_fetch_for_reports_a_dataset_failure_and_an_unreadable_manifest_together(
    tmp_path, monkeypatch
):
    """Both error classes reach the user in one report, not one per run."""
    _, broken = _seed_versioned_dataset(
        tmp_path / 'data', 'star/tracks/demo', '111', {'a.dat': b'A\n'}, ('mymodel',)
    )
    broken.registry_path.unlink()  # the dataset now fails on its missing registry
    monkeypatch.setattr(
        'fwl_io.manifest._discover',
        lambda: ({'shared': [broken]}, {'mymodel': 'unreadable manifest'}),
    )
    with pytest.raises(RuntimeError) as excinfo:
        fetch_for('mymodel', data_root=tmp_path / 'data')
    message = str(excinfo.value)
    assert 'no registry file' in message
    assert 'unreadable manifest' in message


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

    monkeypatch.setattr('fwl_io.manifest._discover', lambda: ({'prov': [wanted, other]}, {}))
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
