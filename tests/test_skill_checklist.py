"""The code-map skill's own verification checklist, made executable.

The skill at `~/.claude/skills/code-map` ships three fixtures and a checklist
of what a good map of each one looks like. Those are the closest thing to a
ground truth recce has, so they are the acceptance test — and the same fixtures
are what a local-model stage would have to be scored against later, which is
the reason to keep the checklist mechanical rather than a thing to read.

The tests skip rather than fail when the skill is not checked out, because the
skill lives in a different repository and recce has to stay usable without it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from recce.extract import discover
from recce.graph import resolve
from recce.rank import plan
from recce.render import render

FIXTURES = Path.home() / '.claude/skills/code-map/testing/fixtures'
pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir(), reason='code-map skill fixtures not checked out here'
)


def map_of(target: Path, **kwargs) -> str:
    project = discover(target)
    graph = resolve(project)
    return render(project, plan(project, graph, **kwargs), base=str(target))


def bracket_labels(text: str) -> set:
    """Every label inside a `[...]` annotation, including grouped ones.

    A collapsed helper row carries the brackets of everything it swallowed, so
    one annotation can read `[argparse, pathlib]`. Matching the literal
    `[pathlib]` misses those, which says nothing about the map and everything
    about the assertion.
    """
    labels = set()
    for group in re.findall(r'\[([^\]]+)\]', text):
        labels.update(part.strip() for part in group.split(','))
    return labels


def tree_lines(text: str) -> list:
    """Only the rows inside fences, which is what the budget counts."""
    rows, inside = [], False
    for line in text.splitlines():
        if line == '```':
            inside = not inside
        elif inside:
            rows.append(line)
    return rows


@pytest.fixture(scope='module')
def with_docstring():
    return map_of(FIXTURES / 'with_docstring.py')


@pytest.fixture(scope='module')
def without_docstring():
    return map_of(FIXTURES / 'without_docstring.py')


@pytest.fixture(scope='module')
def multi_module():
    return map_of(FIXTURES / 'multi_module')


class TestWithDocstring:
    def test_purpose_line_comes_from_the_docstring(self, with_docstring):
        assert with_docstring.splitlines()[0] == (
            '# with_docstring.py — Parse and summarize webserver access logs'
        )

    def test_main_is_the_entry_point_and_comes_first(self, with_docstring):
        assert tree_lines(with_docstring)[0].startswith('main(')

    def test_children_are_indented_under_main(self, with_docstring):
        assert any(line.startswith(' ├─ ') for line in tree_lines(with_docstring))

    def test_something_is_starred(self, with_docstring):
        assert '★' in with_docstring

    def test_the_named_externals_are_bracketed(self, with_docstring):
        assert {'argparse', 'pathlib', 'collections'} <= bracket_labels(with_docstring)

    def test_the_three_trivial_helpers_get_no_row(self, with_docstring):
        for name in ('_parse_args', '_parse_bytes', '_human_bytes'):
            for line in tree_lines(with_docstring):
                assert not line.strip().startswith(('├─ ' + name, '└─ ' + name))

    def test_it_fits_on_a_screen(self, with_docstring):
        assert len(tree_lines(with_docstring)) <= 40

    def test_the_data_shapes_are_named(self, with_docstring):
        assert '## Data' in with_docstring
        assert 'LOG_PATTERN' in with_docstring
        assert 'ip, method, path, status, bytes' in with_docstring

    def test_functions_are_not_listed_alphabetically(self, with_docstring):
        names = [
            line.strip().lstrip('├└─ ').split('(')[0]
            for line in tree_lines(with_docstring)
            if '(' in line
        ]
        assert names != sorted(names)


class TestWithoutDocstring:
    def test_there_is_no_purpose_line(self, without_docstring):
        """The skill's sharpest requirement: do not invent one."""
        assert without_docstring.splitlines()[0] == '# without_docstring.py'

    def test_the_map_is_otherwise_the_same_shape(
        self, with_docstring, without_docstring
    ):
        assert tree_lines(without_docstring) == tree_lines(with_docstring)


class TestMultiModule:
    def test_the_split_is_announced(self, multi_module):
        assert 'Splitting by module for fit' in multi_module

    def test_one_block_per_file(self, multi_module):
        for name in ('parser.py', 'summary.py', 'cli.py'):
            assert '## [' in multi_module and name in multi_module

    def test_each_block_carries_its_own_purpose(self, multi_module):
        headings = [x for x in multi_module.splitlines() if x.startswith('## [')]
        assert len(headings) == 3
        assert all(' — ' in heading for heading in headings)

    def test_blocks_run_leaves_first(self, multi_module):
        order = [x for x in multi_module.splitlines() if x.startswith('## [')]
        names = [h.split(']')[1].split('—')[0].strip() for h in order]
        assert names.index('cli.py') > names.index('parser.py')
        assert names.index('cli.py') > names.index('summary.py')

    def test_cross_file_calls_are_references(self, multi_module):
        assert 'parser.parse_line()' in multi_module
        assert 'summary.summarize()' in multi_module

    def test_the_empty_init_is_omitted(self, multi_module):
        assert '__init__' not in multi_module

    def test_a_spine_section_tells_you_where_to_start(self, multi_module):
        assert '## Spine to read first' in multi_module
        assert 'cli.py:' in multi_module
