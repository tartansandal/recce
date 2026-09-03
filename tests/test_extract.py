"""Parsing: what recce reads off a file before anything is decided."""

from __future__ import annotations

import ast

from recce.extract import compress_type, discover, first_sentence


def parse_annotation(text):
    return ast.parse(text, mode='eval').body


def test_compresses_generic_noise_to_the_shape_that_matters():
    cases = {
        'Optional[List[Tuple[str, float]]]': '[(str, float)]?',
        'Dict[str, int]': '{str: int}',
        'Union[str, int]': 'str|int',
        'typing.Optional[Path]': 'Path?',
        'list[str]': '[str]',
    }
    for written, wanted in cases.items():
        assert compress_type(parse_annotation(written)) == wanted


def test_first_sentence_stops_at_the_first_full_stop():
    doc = 'Parse and summarize logs.\n\nSecond paragraph is not the summary.'
    assert first_sentence(doc) == 'Parse and summarize logs'


def test_module_docstring_becomes_the_purpose(build):
    project, _, _, text = build(
        {'a.py': '"""Do the thing."""\n\ndef main():\n    pass\n'}, 'a.py'
    )
    assert project.modules['a'].doc == 'Do the thing'
    assert text.startswith('# a.py — Do the thing')


def test_a_file_with_no_stated_purpose_gets_no_purpose_line(build):
    """The three permitted sources are silent, so the map must be too.

    This is the case that matters most. Inferring a purpose from the function
    names would be easy, plausible, and unfalsifiable by the reader.
    """
    _, _, _, text = build(
        {'a.py': 'def parse_log_line(line):\n    return line.split()\n'}, 'a.py'
    )
    assert text.startswith('# a.py\n')
    assert '—' not in text.splitlines()[0]


def test_a_readme_one_level_up_is_not_a_files_purpose(build):
    """A README describes the project a file sits in, not the file."""
    _, _, _, text = build(
        {
            'README.md': 'This describes the whole project, not one file.\n',
            'pkg/a.py': 'def go():\n    pass\n',
        },
        'pkg/a.py',
    )
    assert text.startswith('# a.py\n')


def test_header_comment_is_a_purpose_when_there_is_no_docstring(build):
    _, _, _, text = build(
        {
            'a.py': '#!/usr/bin/env python3\n# Convert exports into the report format.\n\ndef go():\n    pass\n'
        },
        'a.py',
    )
    assert text.startswith('# a.py — Convert exports into the report format')


def test_relative_imports_resolve_to_the_sibling_module(tmp_path):
    """The regression that hid every cross-file edge in a package.

    `from .parser import parse_line` inside `pkg.cli` was resolving to
    `pkg.cli.parser`, which matches nothing, so the call was silently called
    external and the edge never appeared.
    """
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('')
    (tmp_path / 'pkg' / 'parser.py').write_text(
        'def parse_line(line):\n    return line\n'
    )
    (tmp_path / 'pkg' / 'cli.py').write_text(
        'from .parser import parse_line\n\n\ndef main():\n    return parse_line("x")\n'
    )
    project = discover(tmp_path / 'pkg')
    assert project.modules['pkg.cli'].imports['parse_line'] == 'pkg.parser.parse_line'


def test_dict_literal_returns_are_recorded_as_a_data_shape(build):
    project, _, _, _ = build(
        {'a.py': 'def record(m):\n    return {"ip": m, "status": 1, "bytes": 2}\n'},
        'a.py',
    )
    func = project.modules['a'].funcs[0]
    assert func.returns_keys == ['ip', 'status', 'bytes']


def test_calls_come_out_in_source_order_not_walk_order(build):
    """`ast.walk` is breadth-first; the map has to read top to bottom."""
    project, _, _, _ = build(
        {'a.py': 'def go():\n    first()\n    if 1:\n        second()\n    third()\n'},
        'a.py',
    )
    calls = [c.attr for c in project.modules['a'].funcs[0].calls]
    assert calls == ['first', 'second', 'third']


def test_a_file_that_will_not_parse_costs_only_that_file(build):
    project, _, _, _ = build(
        {'good.py': 'def go():\n    pass\n', 'bad.py': 'def (:\n'}, '.'
    )
    assert project.modules['bad'].parse_error
    assert project.modules['good'].funcs


def test_ellipsis_in_an_annotation_compresses_rather_than_crashing():
    """`ast.Ellipsis` was removed in 3.12; `Callable[..., X]` reaches this path.

    The reference to it passed every test on the 3.9 floor and raised
    AttributeError on 3.14, which is why `./check` runs both interpreters.
    """
    assert compress_type(parse_annotation('Callable[..., int]')) == 'Callable[..., int]'
    assert compress_type(parse_annotation('Tuple[int, ...]')) == '(int, ...)'


def test_a_rest_title_is_not_a_purpose(build):
    """`requests` opens every module this way, and it broke seven of eight blocks.

    Taking the first paragraph of `requests.cookies\\n~~~~~~~~~~~~~~~~\\n\\nreal
    prose` yields the title and its underline, which then gets printed to the
    reader as a statement of what the module is for.
    """
    doc = '"""\nrequests.cookies\n~~~~~~~~~~~~~~~~\n\nCompatibility code for cookie jars.\n"""'
    project, _, _, text = build({'a.py': doc + '\n\n\ndef go():\n    pass\n'}, 'a.py')
    assert project.modules['a'].doc == 'Compatibility code for cookie jars'
    assert '~~~' not in text


def test_an_overlined_rest_title_is_also_skipped():
    from recce.extract import first_sentence

    doc = '====\nTitle\n====\n\nThe actual summary here.'
    assert first_sentence(doc) == 'The actual summary here'


def test_an_underline_needs_to_be_one_repeated_mark():
    """`a - b` under a line is prose, not a rule; only a run of one mark counts."""
    from recce.extract import first_sentence

    assert first_sentence('Summary line\n- a bullet, not an underline') == (
        'Summary line - a bullet, not an underline'
    )


def test_console_scripts_are_read_from_pyproject(build, tmp_path):
    """The one place a package states its entry points instead of implying them."""
    from recce.extract import declared_entry_points

    (tmp_path / 'pyproject.toml').write_text(
        "[project]\nname = 'x'\n\n[project.scripts]\nx = 'pkg.cli:main'\n"
    )
    assert declared_entry_points(tmp_path) == ['pkg.cli:main']


def test_a_pyproject_above_a_src_layout_is_still_found(tmp_path):
    """flask's manifest sits two levels above `src/flask`."""
    from recce.extract import declared_entry_points

    (tmp_path / '.git').mkdir()
    (tmp_path / 'pyproject.toml').write_text(
        "[project]\nname = 'x'\n\n[project.scripts]\nx = 'pkg.cli:main'\n"
    )
    deep = tmp_path / 'src' / 'pkg'
    deep.mkdir(parents=True)
    assert declared_entry_points(deep) == ['pkg.cli:main']


def test_a_broken_pyproject_costs_nothing(tmp_path):
    from recce.extract import declared_entry_points

    (tmp_path / 'pyproject.toml').write_text('[project\nbroken = ')
    assert declared_entry_points(tmp_path) == []


def test_imports_inside_type_checking_are_recorded(build):
    """httpx binds its entry point this way, and nothing else names its home."""
    project, _, _, _ = build(
        {
            'pkg/__init__.py': 'import typing\n\nif typing.TYPE_CHECKING:\n    from ._main import main\n',
            'pkg/_main.py': 'def main():\n    return 1\n',
        },
        'pkg',
    )
    assert project.modules['pkg'].imports.get('main') == 'pkg._main.main'


def test_imports_in_a_try_except_fallback_are_recorded(build):
    project, _, _, _ = build(
        {
            'pkg/__init__.py': 'try:\n    from .fast import go\nexcept ImportError:\n    from .slow import go\n',
            'pkg/fast.py': 'def go():\n    return 1\n',
            'pkg/slow.py': 'def go():\n    return 0\n',
        },
        'pkg',
    )
    assert project.modules['pkg'].imports.get('go') in (
        'pkg.fast.go',
        'pkg.slow.go',
    )


def test_the_function_index_is_built_once_and_reused(build):
    """Mapping the stdlib rebuilt a 55,710-entry dict 166 times."""
    project, _, _, _ = build({'a.py': '"""P."""\n\n\ndef go():\n    pass\n'}, 'a.py')
    first = project.by_id()
    assert project.by_id() is first


def test_the_index_notices_a_module_arriving_late(build):
    """The guard is O(1), so it has to catch the one mutation that matters."""
    from pathlib import Path

    from recce.extract import extract_module

    project, _, _, _ = build({'a.py': '"""P."""\n\n\ndef go():\n    pass\n'}, 'a.py')
    assert 'a::go' in project.by_id()
    extra = extract_module(Path(project.modules['a'].path), 'b')
    project.modules['b'] = extra
    assert 'b::go' in project.by_id()


def test_overload_stubs_are_not_functions(build):
    """requests writes three `HTTPBasicAuth.__init__`s: two overloads and one body.

    Emitting a row for each says the class has three constructors, and gives
    three `Func` objects the same `node_id` — so the index kept one while the
    renderer walked a list holding all three.
    """
    project, _, _, _ = build(
        {
            'a.py': (
                '"""P."""\n\nfrom typing import overload\n\n\nclass C:\n'
                '    @overload\n    def go(self, x: int) -> int: ...\n\n'
                '    @overload\n    def go(self, x: str) -> str: ...\n\n'
                '    def go(self, x):\n        if x:\n            return x\n        return None\n'
            )
        },
        'a.py',
    )
    goes = [f for f in project.funcs() if f.qualname == 'C.go']
    assert len(goes) == 1
    assert goes[0].n_branches == 1  # the real body, not a stub


def test_every_function_has_a_distinct_node_id(build):
    """A duplicate id makes the index and the render disagree about what exists."""
    project, _, _, _ = build(
        {
            'a.py': (
                '"""P."""\n\nimport sys\n\n\n'
                'if sys.platform == "win32":\n'
                '    def go():\n        return 1\n'
                'else:\n'
                '    def go():\n        return 2\n\n\n'
                'def other():\n    return go()\n'
            )
        },
        'a.py',
    )
    ids = [f.node_id for f in project.funcs()]
    assert len(ids) == len(set(ids)), ids


def test_a_property_pair_keeps_the_getter(build):
    """A getter and its setter share a qualified name; only one row is right.

    httpx's `Headers.encoding` is a five-branch getter beside a two-line
    setter, and keeping whichever came last kept the setter.
    """
    project, _, _, _ = build(
        {
            'a.py': (
                '"""P."""\n\n\nclass C:\n'
                '    @property\n    def enc(self):\n'
                '        for name in ("utf-8", "latin-1"):\n'
                '            if name:\n                return name\n'
                '        return None\n\n'
                '    @enc.setter\n    def enc(self, value):\n        self._enc = value\n'
            )
        },
        'a.py',
    )
    kept = [f for f in project.funcs() if f.qualname == 'C.enc']
    assert len(kept) == 1
    assert kept[0].n_branches == 2  # the getter, not the one-line setter
