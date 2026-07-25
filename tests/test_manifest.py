import io
import json
import pathlib
import tarfile

import pooch
import pytest

from fwl_io.fetch import create_fetcher
from fwl_io.manifest import (
    Dataset,
    ManifestSchemaError,
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
    # A key of a different depth follows the same rule, so the derivation tracks
    # the key rather than assuming a fixed group/dataset nesting.
    shallow = load_manifest(_write(tmp_path, '[star.demo]\nzenodo = "10.5281/zenodo.1"\n'))[0]
    assert shallow.subdir == 'star/demo'
    assert shallow.registry_path.name == 'star.demo.registry.txt'


def test_explicit_subdir_rejected(tmp_path):
    """A manifest cannot declare a location that could drift from its key."""
    bad = '[g.d]\nsubdir = "somewhere/else"\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, bad))
    # The same table without the field loads, so the rejection is the field's doing.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'g/d'


@pytest.mark.parametrize('value', ['"x"', '1', 'true', '["a", "b"]', '[]'])
def test_any_non_table_subdir_value_rejected(tmp_path, value):
    """Whatever type it is given, a declared location is still a declared location."""
    bad = f'[g.d]\nsubdir = {value}\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, bad))
    # Discrimination: dropping the line loads the same dataset at its key path,
    # so the value never quietly decides the location either.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'g/d'


@pytest.mark.parametrize('value', ['"somewhere/else"', '1', 'true', '["a", "b"]', '[]'])
def test_subdir_at_the_manifest_root_rejected(tmp_path, value):
    """The field is refused outside any table too, where it would be dropped."""
    bad = f'subdir = {value}\n[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='the manifest root') as at_root:
        load_manifest(_write(tmp_path, bad))
    # The root has no key of its own, so it gets the general sentence rather than
    # the derived path a table is told to compare against.
    assert 'derived from its own table key' in str(at_root.value)
    in_table = f'[g.d]\nsubdir = {value}\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError) as at_table:
        load_manifest(_write(tmp_path, in_table))
    assert 'derived from its own table key' not in str(at_table.value)
    assert "'g/d'" in str(at_table.value)
    # A root-level scalar that is not "subdir" is still fine.
    good = 'schema_version = 1\n[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].key == 'g.d'


def test_a_table_named_subdir_is_an_ordinary_directory_level(tmp_path):
    """The rejection is about a declared location, not about the word itself."""
    at_root = load_manifest(_write(tmp_path, '[subdir.demo]\nzenodo = "10.5281/zenodo.1"\n'))
    assert at_root[0].subdir == 'subdir/demo'
    nested = load_manifest(_write(tmp_path, '[g.d]\n[g.d.subdir]\nzenodo = "10.5281/zenodo.1"\n'))
    assert nested[0].subdir == 'g/d/subdir'
    # Discrimination: the same word as a field, not a table, is still refused.
    with pytest.raises(ValueError, match='"subdir" is not a manifest field'):
        load_manifest(_write(tmp_path, '[g.d]\nsubdir = "x"\nzenodo = "10.5281/zenodo.1"\n'))


@pytest.mark.parametrize(
    'manifest',
    [
        '[[subdir]]\nzenodo = "10.5281/zenodo.1"\n',
        '[g.d]\n[[g.d.subdir]]\nzenodo = "10.5281/zenodo.1"\n',
    ],
)
def test_array_of_tables_named_subdir_reports_the_structure(tmp_path, manifest):
    """A structural mistake is named as one, whatever the table is called."""
    with pytest.raises(ValueError, match='arrays of tables') as excinfo:
        load_manifest(_write(tmp_path, manifest))
    # Discrimination: the reader is not sent looking for a "subdir" field to
    # delete, since none was declared.
    assert 'not a manifest field' not in str(excinfo.value)


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
    with pytest.raises(ValueError, match='"subdir" is not a manifest field') as excinfo:
        load_manifest(_write(tmp_path, bad))
    # The message names the derived location, which is what the author should
    # compare against before deleting the line.
    assert "'g/d'" in str(excinfo.value)


@pytest.mark.parametrize(
    ('segment', 'nearest_safe'),
    [
        ('a/b', 'a_b'),
        ('a\\\\b', 'a_b'),
        ('a.b', 'a_b'),
        ('', 'a'),
        (' ', '_'),
        ('a b', 'a_b'),
        ('-lead', '_lead'),
        ('naïve', 'naive'),
        ('demo\\n', 'demo'),
        ('\\ttab', 'tab'),
        ('a+b', 'a-b'),
    ],
)
def test_unsafe_key_segment_rejected(tmp_path, segment, nearest_safe):
    """A key segment that is not a plain directory name never reaches the data root."""
    bad = f'[g."{segment}"]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='not a valid directory name'):
        load_manifest(_write(tmp_path, bad))
    # A safe spelling of this very segment loads, so the rule bites on the
    # offending character rather than on the shape of the key around it. An
    # empty segment has no spelling of its own, so a bare letter stands in.
    good = f'[g.{nearest_safe}]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == f'g/{nearest_safe}'


@pytest.mark.parametrize('segment', ['.', '..'])
def test_relative_path_segment_named_as_such(tmp_path, segment):
    """A path component as a key gets its own diagnosis, not the character rule."""
    bad = f'[g."{segment}"]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='relative-path component') as excinfo:
        load_manifest(_write(tmp_path, bad))
    # Discrimination: the generic character message would leave the reader
    # hunting for an illegal character in a segment made only of dots.
    assert 'not a valid directory name' not in str(excinfo.value)


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
    """A mirror alone does not make a dataset: the pin is what identifies one."""
    bad = '[g.d]\ndataverse = "10.34894/XYZ"\n'
    with pytest.raises(ValueError, match='has no "zenodo" key') as excinfo:
        load_manifest(_write(tmp_path, bad))
    # Discrimination: the table is not treated as a dataset with a bad pin, which
    # is what a discriminator keyed on "dataverse" would report instead.
    assert 'is not a Zenodo DOI' not in str(excinfo.value)
    # Adding the pin turns the same table into a dataset.
    good = '[g.d]\ndataverse = "10.34894/XYZ"\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].dataverse == '10.34894/XYZ'


def test_empty_zenodo_value_rejected_as_an_empty_pin(tmp_path):
    """An empty pin says the value is empty rather than naming a bad DOI."""
    bad = '[g.d]\nzenodo = ""\n'
    with pytest.raises(ValueError, match='the "zenodo" value is empty') as excinfo:
        load_manifest(_write(tmp_path, bad))
    # Discrimination: the empty value takes neither the malformed-DOI branch nor
    # the missing-key branch, so the message points at the value itself.
    assert 'is not a Zenodo DOI' not in str(excinfo.value)
    assert 'has no "zenodo" key' not in str(excinfo.value)


@pytest.mark.parametrize(
    'value', ['https://zenodo.org/records/1', '10.1234/other.repo', '10.5281/zenodo.abc']
)
def test_non_zenodo_doi_rejected(tmp_path, value):
    """Only a Zenodo version DOI pins a deposit, so nothing else is accepted."""
    bad = f'[g.d]\nzenodo = "{value}"\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))
    # The well-formed pin of the same shape loads and keeps its record id.
    good = '[g.d]\nzenodo = "10.5281/zenodo.15729114"\n'
    assert load_manifest(_write(tmp_path, good))[0].zenodo.endswith('15729114')


@pytest.mark.parametrize('value', ['15729114', 'false', '0', '["a"]', '1.5'])
def test_non_string_zenodo_rejected(tmp_path, value):
    """A bare number is not a DOI, and must fail as a manifest error."""
    bad = f'[g.d]\nzenodo = {value}\n'
    with pytest.raises(ValueError, match='not a Zenodo DOI'):
        load_manifest(_write(tmp_path, bad))
    # Quoting the same digits as a full DOI is the accepted spelling.
    good = '[g.d]\nzenodo = "10.5281/zenodo.15729114"\n'
    assert load_manifest(_write(tmp_path, good))[0].key == 'g.d'


@pytest.mark.parametrize('value', ['42', 'true', '""', '"   "', '["a"]'])
def test_unusable_name_rejected(tmp_path, value):
    """A dataset label is author-supplied text, checked when the manifest loads."""
    bad = f'[g.d]\nname = {value}\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='"name" must be non-empty text'):
        load_manifest(_write(tmp_path, bad))
    # An absent name falls back to the key rather than to an empty string, and a
    # name with text in it is kept as written.
    assert load_manifest(_write(tmp_path, '[g.d]\nzenodo = "10.5281/zenodo.1"\n'))[0].name == 'g.d'
    named = '[g.d]\nname = "Demo tracks"\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, named))[0].name == 'Demo tracks'


@pytest.mark.parametrize(
    'value',
    ['"not-a-doi"', 'false', '0', '""', '"10.34894/AB junk"', r'"10.34894/AB\n"'],
)
def test_bad_dataverse_doi_rejected(tmp_path, value):
    """A mirror DOI is checked by type and in full, whitespace and all."""
    bad = f'[g.d]\nzenodo = "10.5281/zenodo.1"\ndataverse = {value}\n'
    with pytest.raises(ValueError, match='is not a DOI'):
        load_manifest(_write(tmp_path, bad))
    # The clean form of the same DOI loads and reaches the dataset unchanged.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\ndataverse = "10.34894/ABCDEF"\n'
    assert load_manifest(_write(tmp_path, good))[0].dataverse == '10.34894/ABCDEF'


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
    # The same DOI without the break is accepted, so the rule is the whitespace.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1234567"\n'
    assert load_manifest(_write(tmp_path, good))[0].zenodo == '10.5281/zenodo.1234567'


def test_dataset_rejects_positional_construction():
    """Fields are keyword-only, so a stale positional call cannot rebind them."""
    with pytest.raises(TypeError):
        Dataset('g.d', 'demo', '10.5281/zenodo.1')
    # The keyword form is the supported one and derives the location from the key.
    ds = Dataset(key='g.d', name='demo', zenodo='10.5281/zenodo.1')
    assert ds.subdir == 'g/d'


@pytest.mark.parametrize('value', ['"mors"', '[1, 2]', '["ok", 7]', '42'])
def test_bad_required_by_rejected(tmp_path, value):
    """Model names are text in a list: anything else matches no model at load."""
    bad = f'[g.d]\nzenodo = "10.5281/zenodo.1"\nrequired_by = {value}\n'
    with pytest.raises(ValueError, match='must be a list of model names'):
        load_manifest(_write(tmp_path, bad))
    # A well-formed list survives, so the guard is about the shape of the value.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\nrequired_by = ["mors"]\n'
    assert load_manifest(_write(tmp_path, good))[0].required_by == ('mors',)


def test_dataset_with_subtable_rejected_not_silently_dropped(tmp_path):
    """A pinned table is a leaf, so nesting below it is a structural mistake."""
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\n[g.d.meta]\nauthor = "someone"\n'
    with pytest.raises(ValueError, match='must not contain sub-tables'):
        load_manifest(_write(tmp_path, bad))
    # The same nesting without a pin on the parent is a grouping level.
    good = '[g.d]\n[g.d.meta]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'g/d/meta'


def test_array_of_tables_rejected_not_silently_dropped(tmp_path):
    """An array of tables would hide several datasets behind one key."""
    bad = '[[g.d]]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='arrays of tables'):
        load_manifest(_write(tmp_path, bad))
    # The single-table spelling of the same intent is what loads.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].key == 'g.d'


def test_top_level_array_of_tables_rejected(tmp_path):
    """An array at the outermost level is refused too, not just a nested one."""
    bad = '[[d]]\nzenodo = "10.5281/zenodo.1"\n'
    with pytest.raises(ValueError, match='arrays of tables'):
        load_manifest(_write(tmp_path, bad))
    # The single-table spelling at the same level is what loads.
    good = '[d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].subdir == 'd'


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
    """A dataset whose registry was never generated names the command to run."""
    ds = load_manifest(_write(tmp_path, GOOD))[0]
    with pytest.raises(FileNotFoundError, match='fwl-io sync'):
        ds.registry()
    # Writing the registry beside the manifest is what makes the same dataset
    # resolvable, so the error is about the missing file and nothing else.
    ds.registry_path.write_text('alpha.dat sha256:abc\n')
    assert ds.registry() == {'alpha.dat': 'sha256:abc'}


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
    """One package with a broken manifest cannot hide every other package's data."""
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


def test_fetch_for_resolves_an_archive_dataset_at_the_key_derived_path(
    http_server, tmp_path, monkeypatch
):
    """An extract= dataset lands under the path its manifest key derives."""
    base_url, served = http_server
    archive = pathlib.Path(served) / 'tracks.tar'
    with tarfile.open(archive, 'w') as tf:
        for member, payload in (('m0p1.txt', b'0.1\n'), ('nested/m1p0.txt', b'1.0\n')):
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    checksum = 'sha256:' + pooch.file_hash(str(archive), alg='sha256')

    manifest = _write(
        tmp_path,
        '[star.tracks.demo]\nzenodo = "10.5281/zenodo.1234567"\n'
        'extract = "tar"\nrequired_by = ["demo"]\n',
    )
    (tmp_path / 'star.tracks.demo.registry.txt').write_text(f'tracks.tar {checksum}\n')
    ds = load_manifest(manifest)[0]
    assert ds.extract == 'tar'

    # Extract once through the low-level API at the derived location, then let
    # fetch_for resolve the same dataset offline: no network, and the paths it
    # returns are the proof that the key alone decided where the tree lives.
    data_root = tmp_path / 'data'
    create_fetcher(
        subdir=ds.subdir,
        registry=ds.registry(),
        base_urls=[base_url],
        zenodo=ds.zenodo,
        data_root=data_root,
        extract='tar',
    ).fetch_all()
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points',
        lambda group: [_FakeEntryPoint('demo-model', lambda: manifest)],
    )
    monkeypatch.setenv('FWL_IO_OFFLINE', '1')

    fetched = fetch_for('demo', data_root=data_root)

    version_dir = data_root / 'star/tracks/demo/r1234567'
    assert sorted(p.relative_to(version_dir).as_posix() for p in fetched['star.tracks.demo']) == [
        'm0p1.txt',
        'nested/m1p0.txt',
    ]
    # The archive itself is not kept, and no unversioned copy is left behind.
    assert not (version_dir / 'tracks.tar').exists()
    assert not (data_root / 'star/tracks/demo/tracks.tar').exists()


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


def test_unknown_dataset_field_reports_both_readings(tmp_path):
    """A field this fwl-io does not know is a schema disagreement, not a
    malformed file, and the error offers both readings of it.

    A model ships its manifest with its own code, so the manifest can be newer
    than the installed fwl-io. Ignoring the field silently would leave the
    manifest asking for something it never gets. The two causes, a misspelt
    field and a newer schema, need different actions, so the message names
    both rather than asserting one.
    """
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nchecksum_algorithm = "sha256"\nmirror_priority = 2\n'

    with pytest.raises(ManifestSchemaError) as excinfo:
        load_manifest(_write(tmp_path, bad))

    message = str(excinfo.value)
    # Every unknown field is named, not just the first one found.
    assert "'checksum_algorithm'" in message
    assert "'mirror_priority'" in message
    # The accepted set is spelled out in full, so the reader can see what was
    # expected rather than a sample of it.
    known = message.split('known fields:')[1]
    for accepted in ('dataverse', 'extract', 'name', 'required_by', 'zenodo'):
        assert accepted in known
    # Both actions are offered, because either cause is plausible.
    assert 'check the spelling' in message
    assert 'upgrade fwl-io' in message
    # Discrimination: the same table without those fields loads, so the error
    # is the fields and not the table.
    good = '[g.d]\nzenodo = "10.5281/zenodo.1"\n'
    assert load_manifest(_write(tmp_path, good))[0].key == 'g.d'


def test_error_reports_the_schema_the_running_code_implements(tmp_path):
    """The message identifies the code doing the reading by its schema number.

    An editable checkout keeps the version recorded at install time, so the
    distribution version can name a release that contains none of the code
    actually running. The schema number lives in the source and travels with
    it, so it cannot go stale that way.
    """
    import fwl_io
    from fwl_io.manifest import _MANIFEST_SCHEMA

    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nunknown_field = 1\n'

    with pytest.raises(ManifestSchemaError) as excinfo:
        load_manifest(_write(tmp_path, bad))

    message = str(excinfo.value)
    # The schema number, pinned to a literal so a silent renumber is caught.
    assert _MANIFEST_SCHEMA == 1
    assert 'manifest schema 1' in message
    # The packaging version is reported alongside it and labelled as what it
    # is, so the two are not confused for each other.
    assert f'distribution {fwl_io.__version__}' in message


def test_schema_error_reaches_a_caller_catching_value_error(tmp_path):
    """A caller that already handles a malformed manifest receives the typed
    error unchanged, so a consumer needs no new except clause to keep working.
    """
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nunknown_field = 1\n'

    with pytest.raises(ValueError) as excinfo:
        load_manifest(_write(tmp_path, bad))

    # Caught as ValueError, delivered as the specific type.
    assert isinstance(excinfo.value, ManifestSchemaError)
    assert type(excinfo.value) is not ValueError


def test_every_declared_dataset_field_is_accepted(tmp_path):
    """The accepted set is exactly the model the loader fills in.

    Discrimination: a field dropped from the set makes this manifest fail, and
    a field added to the set without a home on the dataset is caught by the
    comparison against the model rather than by loading.
    """
    import dataclasses

    from fwl_io.manifest import _DATASET_FIELDS

    manifest = (
        '[g.d]\n'
        'name = "Demo"\n'
        'zenodo = "10.5281/zenodo.1234567"\n'
        'dataverse = "10.34894/ABCDEF"\n'
        'required_by = ["mors"]\n'
        'extract = "tar"\n'
    )

    dataset = load_manifest(_write(tmp_path, manifest))[0]

    assert dataset.name == 'Demo'
    assert dataset.zenodo == '10.5281/zenodo.1234567'
    assert dataset.dataverse == '10.34894/ABCDEF'
    assert dataset.required_by == ('mors',)
    assert dataset.extract == 'tar'
    # The set cannot drift open: it is the dataset model minus the two fields
    # the loader derives rather than reads.
    derived = {'key', 'registry_path'}
    assert _DATASET_FIELDS == {f.name for f in dataclasses.fields(Dataset)} - derived
    # The location is derived, so re-admitting it as a field would undo that.
    assert 'subdir' not in _DATASET_FIELDS


def test_declared_subdir_is_reported_as_a_dropped_field(tmp_path):
    """A manifest still declaring `subdir` is the other direction of the same
    disagreement: the manifest is older than the fwl-io reading it, so the
    action is to delete the line, not to upgrade.
    """
    bad = '[g.d]\nzenodo = "10.5281/zenodo.1"\nsubdir = "somewhere/else"\n'

    with pytest.raises(ManifestSchemaError) as excinfo:
        load_manifest(_write(tmp_path, bad))

    message = str(excinfo.value)
    assert 'subdir' in message
    # The location is derived, and the message says where to.
    assert 'g/d' in message
    assert 'remove the line' in message
    # Discrimination: upgrading fwl-io is the wrong action here, and offering
    # it would send the reader in the opposite direction.
    assert 'upgrade fwl-io' not in message


def test_dataset_field_written_one_level_too_high_is_rejected(tmp_path):
    """A dataset field on a grouping table is refused rather than dropped.

    A `required_by` on the grouping table would leave the dataset claiming no
    model needs it, so `fwl-io fetch <model>` would fetch nothing while the
    manifest said otherwise. It is refused instead.
    """
    misplaced = '[star]\nrequired_by = ["mors"]\n[star.tracks]\nzenodo = "10.5281/zenodo.1"\n'

    with pytest.raises(ManifestSchemaError) as excinfo:
        load_manifest(_write(tmp_path, misplaced))

    message = str(excinfo.value)
    assert "'required_by'" in message
    assert "'star'" in message
    # The action is to move the line, since the field is spelled correctly and
    # no fwl-io reads it where it sits.
    assert 'move the line into the dataset table' in message
    assert 'check the spelling' not in message
    assert 'upgrade fwl-io' not in message
    # Discrimination: the same field inside the dataset table is read, so the
    # rejection is about where it sits, not about the field itself.
    correct = '[star.tracks]\nzenodo = "10.5281/zenodo.1"\nrequired_by = ["mors"]\n'
    assert load_manifest(_write(tmp_path, correct))[0].required_by == ('mors',)


def test_unknown_name_on_a_grouping_level_keeps_the_two_readings(tmp_path):
    """A name that is not a dataset field at all gets the spelling-or-upgrade
    advice wherever it appears, because either cause remains possible.

    Discrimination against the misplaced-field case: that one names an action
    that only applies to a field this fwl-io does read.
    """
    bad = '[star]\nchecksum_algorithm = "sha256"\n[star.tracks]\nzenodo = "10.5281/zenodo.1"\n'

    with pytest.raises(ManifestSchemaError) as excinfo:
        load_manifest(_write(tmp_path, bad))

    message = str(excinfo.value)
    assert "'checksum_algorithm'" in message
    assert 'check the spelling' in message
    assert 'move the line' not in message
