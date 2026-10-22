"""Tests of the actions/cache key for an FWL data tree (``fwl_io.cache``, ``fwl-io cache``)."""

import hashlib
from pathlib import Path

import pytest

import fwl_io
import fwl_io.cache as ck
from fwl_io.cache import cache_key
from fwl_io.cli import main
from fwl_io.manifest import Dataset, load_manifest

DATA = Path(__file__).parent / 'data' / 'cache_key'
JANUS_SHARED = ['atmos_clim.spectral_files.oak.318', 'star.spectra.named']


class _EP:
    def __init__(self, name, path):
        self.name, self._path = name, path

    def load(self):
        return lambda: self._path


@pytest.fixture()
def providers(monkeypatch):
    """Install the fwl-mors 26.10.6 manifest and the two shared datasets JANUS reads."""
    eps = [
        _EP('mors', DATA / 'mors' / 'mors_manifest.toml'),
        _EP('fwl-io-shared', DATA / 'shared' / 'shared_manifest.toml'),
    ]
    monkeypatch.setattr('fwl_io.manifest.entry_points', lambda group: eps)
    monkeypatch.setattr(fwl_io, '__version__', '26.10.16')


def _dataset(tmp_path, record='1234567', digest='sha256:' + 'a' * 64, extract=None):
    """One manifest dataset with a one-file registry, loaded through load_manifest."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'g.demo.registry.txt').write_text(f'a.tar {digest}\n')
    lines = ['[g.demo]', f'zenodo = "10.5281/zenodo.{record}"']
    if extract:
        lines.append(f'extract = "{extract}"')
    (tmp_path / 'manifest.toml').write_text('\n'.join(lines) + '\n')
    return load_manifest(tmp_path / 'manifest.toml')


@pytest.mark.integration
@pytest.mark.parametrize(
    ('args', 'key'),
    [
        (
            ['--prefix', 'fwl-data-', '--provider', 'mors']
            + [a for k in JANUS_SHARED for a in ('--key', k)],
            'fwl-data-b28a1aa254d3a84bee829594e099ec2b909b739aa2d2a659e9ec61eab8d3ac6d',
        ),
        (
            ['--prefix', 'fwl-data-nightly-', '--key', 'star.tracks.spada_2013'],
            'fwl-data-nightly-13ff73482a7bde56f444bdb0893c69c3fee19709f512fa9f9eb0a9f7fee70d56',
        ),
        (
            ['--prefix', 'mors-fwl-data-', '--provider', 'mors'],
            'mors-fwl-data-caa4fc2270532056159c531864ba5af2488ba690b2a22ac34411fcd21769ad8c',
        ),
    ],
    ids=['janus', 'zephyrus', 'mors'],
)
def test_the_keys_of_the_three_nightly_selections(providers, tmp_path, monkeypatch, args, key):
    """The JANUS key equals the one its nightly resolved at fwl-io 26.10.16 and
    fwl-mors 26.10.6; ZEPHYRUS and MORS gain the extract kind and the fwl-io release."""
    out = tmp_path / 'github_output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))
    assert main(['cache', 'key', *args]) == 0
    prefix = args[1]
    assert out.read_text().splitlines() == [f'key={key}', f'restore-keys={prefix}']


@pytest.mark.unit
def test_the_key_moves_with_every_part_of_the_pin(tmp_path, monkeypatch):
    """A new record, checksum, extract kind or fwl-io release gives a new key."""
    monkeypatch.setattr(fwl_io, '__version__', '26.10.16')
    base = cache_key(_dataset(tmp_path / 'base'), 'k-').key
    moved = [
        cache_key(_dataset(tmp_path / 'record', record='1234568'), 'k-').key,
        cache_key(_dataset(tmp_path / 'digest', digest='sha256:' + 'b' * 64), 'k-').key,
        cache_key(_dataset(tmp_path / 'extract', extract='tar'), 'k-').key,
    ]
    monkeypatch.setattr(fwl_io, '__version__', '26.11.2')
    moved.append(cache_key(_dataset(tmp_path / 'release'), 'k-').key)
    assert len({base, *moved}) == 5


@pytest.mark.unit
def test_the_key_is_stable_for_the_same_pins(tmp_path, monkeypatch):
    """Same pins, another data root and another fwl-io patch release: the same key."""
    monkeypatch.setattr(fwl_io, '__version__', '26.10.16')
    first = cache_key(_dataset(tmp_path / 'a'), 'k-')
    monkeypatch.setenv('FWL_DATA', str(tmp_path / 'elsewhere'))
    monkeypatch.setattr(fwl_io, '__version__', '26.10.30.dev3+gabc')
    assert cache_key(_dataset(tmp_path / 'b'), 'k-') == first
    assert not (tmp_path / 'elsewhere').exists()


@pytest.mark.unit
def test_an_unversioned_dataset_gets_no_restore_keys(tmp_path):
    """A dataset without a Zenodo pin resolves into its bare location, where a stale
    tree from a prefix restore sits at the same path, so no restore-keys are given."""
    versioned = _dataset(tmp_path)[0]
    (tmp_path / 'g.bare.registry.txt').write_text(f'b.dat sha256:{"c" * 64}\n')
    bare = Dataset(
        key='g.bare',
        name='g.bare',
        dataverse='10.34894/ABCDEF',
        registry_path=tmp_path / 'g.bare.registry.txt',
    )
    assert cache_key([versioned], 'k-').restore_keys == 'k-'
    mixed = cache_key([versioned, bare], 'k-')
    assert mixed.restore_keys == '' and mixed.unversioned == ('g.bare',)


@pytest.mark.integration
def test_the_key_command_names_an_unversioned_dataset(tmp_path, monkeypatch, capsys):
    """The key step writes an empty restore-keys line and says why on stderr."""
    monkeypatch.setattr(
        'fwl_io.cli._select', lambda args: [Dataset(key='g.bare', name='', dataverse='10.34894/X')]
    )
    monkeypatch.setattr(
        'fwl_io.cache._material', lambda ds: ['dir\tg/bare', 'file\tb.dat\tsha256:0']
    )
    out = tmp_path / 'github_output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))
    assert main(['cache', 'key', '--prefix', 'k-', '--key', 'g.bare']) == 0
    assert out.read_text().splitlines()[1] == 'restore-keys='
    assert 'no restore-keys: g.bare has no version folder' in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.parametrize(
    ('args', 'cause'),
    [
        ([], 'no dataset selected'),
        (['--key', 'g.absent'], "no installed manifest declares the dataset 'g.absent'"),
        (['--provider', 'absent'], "no installed manifest provider 'absent'"),
        (['--provider', 'mors', '--key', 'star.tracks.spada_2013'], 'selected twice'),
    ],
)
def test_a_selection_that_resolves_nothing_or_twice_exits_1(providers, capsys, args, cause):
    """Every bad selection stops the key step with its cause, never a partial key."""
    assert main(['cache', 'key', '--prefix', 'k-', *args]) == 1
    assert cause in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize(
    ('prefix', 'ok'),
    [
        ('k-', True),
        ('fwl-data-nightly-', True),
        ('a.b_c-9', True),
        ('-', True),
        ('', False),
        (' ', False),
        ('fwl data-', False),
        ('fwl-data-\n', False),
        ('a\nkey=x', False),
        ('daten-\u00e4', False),
        ('k-\t', False),
        ('k-*', False),
    ],
)
def test_a_prefix_is_kept_verbatim_or_refused(monkeypatch, prefix, ok):
    """A prefix of key characters starts the key; anything else (empty, white space,
    non-ASCII, a newline that would split GITHUB_OUTPUT) is refused."""
    monkeypatch.setattr(ck, '_material', lambda ds: ['dir\tg/demo/r1', 'file\ta\tsha256:0'])
    datasets = [Dataset(key='g.demo', name='', zenodo='10.5281/zenodo.1')]
    if ok:
        key = cache_key(datasets, prefix).key
        assert key.startswith(prefix) and len(key) == len(prefix) + 64
    else:
        with pytest.raises(ValueError, match='prefix'):
            cache_key(datasets, prefix)


@pytest.mark.integration
def test_fetch_and_check_cover_the_selection_the_key_hashes(providers, tmp_path, monkeypatch):
    """fetch runs fetch_all on each selected dataset in the key's order; check then
    reads the same datasets and fails while one is incomplete."""
    fetched = []
    monkeypatch.setattr(
        'fwl_io.fetch.Fetcher.fetch_all', lambda self, offline=None: fetched.append(self.rel_dir)
    )
    root = tmp_path / 'data'
    args = ['--provider', 'mors', '--key', JANUS_SHARED[0], '--data-root', str(root)]
    assert main(['cache', 'fetch', *args]) == 0
    assert fetched == [
        'star/tracks/baraffe_2015/r15729114',
        'star/tracks/spada_2013/r15729101',
        'atmos_clim/spectral_files/oak/318/r15743843',
    ]
    assert main(['cache', 'check', *args]) == 1


@pytest.mark.integration
def test_check_passes_on_a_complete_tree(tmp_path, monkeypatch, capsys):
    """A plain dataset whose files match the registry checks clean."""
    body = b'demo'
    (tmp_path / 'g.demo.registry.txt').write_text(
        f'a.dat sha256:{hashlib.sha256(body).hexdigest()}\n'
    )
    (tmp_path / 'manifest.toml').write_text('[g.demo]\nzenodo = "10.5281/zenodo.7"\n')
    monkeypatch.setattr(
        'fwl_io.manifest.entry_points', lambda group: [_EP('demo', tmp_path / 'manifest.toml')]
    )
    root = tmp_path / 'data'
    (root / 'g/demo/r7').mkdir(parents=True)
    (root / 'g/demo/r7/a.dat').write_bytes(body)
    assert main(['cache', 'check', '--provider', 'demo', '--data-root', str(root)]) == 0
    assert 'g/demo/r7: 1/1 files ok' in capsys.readouterr().out


@pytest.mark.integration
def test_check_never_creates_the_data_root(providers, tmp_path, capsys):
    """A missing data root is reported, not created: a check leaves the disk as it found it."""
    root = tmp_path / 'absent'
    assert main(['cache', 'check', '--provider', 'mors', '--data-root', str(root)]) == 1
    assert not root.exists()
    assert 'does not exist' in capsys.readouterr().err
