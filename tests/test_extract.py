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
