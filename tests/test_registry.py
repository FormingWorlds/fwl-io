import pytest

from fwl_io.registry import load_registry, write_registry

pytestmark = pytest.mark.unit


def test_round_trip_is_sorted_and_stable(tmp_path):
    path = tmp_path / 'reg.txt'
    entries = {'b.dat': 'sha256:bbb', 'a.dat': 'md5:aaa'}
    write_registry(path, entries)
    assert path.read_text() == 'a.dat md5:aaa\nb.dat sha256:bbb\n'
    assert load_registry(path) == entries


def test_comments_and_blank_lines_ignored(tmp_path):
    path = tmp_path / 'reg.txt'
    path.write_text('# header\n\na.dat sha256:aaa\n')
    assert load_registry(path) == {'a.dat': 'sha256:aaa'}


def test_malformed_line_raises_with_location(tmp_path):
    path = tmp_path / 'reg.txt'
    path.write_text('a.dat sha256:aaa extra-token\n')
    with pytest.raises(ValueError, match='reg.txt:1'):
        load_registry(path)


def test_nested_names_allowed(tmp_path):
    path = tmp_path / 'reg.txt'
    write_registry(path, {'sub/nested.dat': 'sha256:aaa'})
    assert load_registry(path) == {'sub/nested.dat': 'sha256:aaa'}


@pytest.mark.parametrize('name', ['../escape.dat', '/etc/passwd', 'a/../../b.dat', 'a\\b.dat'])
def test_traversal_names_rejected_on_load(tmp_path, name):
    path = tmp_path / 'reg.txt'
    path.write_text(f'{name} sha256:aaa\n')
    with pytest.raises(ValueError):
        load_registry(path)


@pytest.mark.parametrize('name', ['../escape.dat', '/abs.dat'])
def test_traversal_names_rejected_on_write(tmp_path, name):
    with pytest.raises(ValueError):
        write_registry(tmp_path / 'reg.txt', {name: 'sha256:aaa'})
