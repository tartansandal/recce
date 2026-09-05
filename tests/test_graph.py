"""Call resolution: which edges recce is willing to draw, and which it refuses."""

from __future__ import annotations

from recce.model import EXTERNAL, UNRESOLVED


def calls_of(project, module, qualname):
    func = next(f for f in project.modules[module].funcs if f.qualname == qualname)
    return {c.attr: c for c in func.calls}


def test_a_call_into_another_module_becomes_an_edge(build):
    project, graph, _, _ = build(
        {
            'pkg/__init__.py': '',
            'pkg/parser.py': 'def parse(line):\n    return line\n',
            'pkg/cli.py': 'from .parser import parse\n\n\ndef main():\n    return parse("x")\n',
        },
        'pkg',
    )
    assert graph.callees('pkg.cli::main') == ['pkg.parser::parse']


def test_a_stdlib_call_is_bracketed_by_its_top_level_package(build):
    project, _, _, _ = build(
        {
            'a.py': 'from pathlib import Path\n\n\ndef go(p):\n    return Path(p).read_text()\n'
        },
        'a.py',
    )
    call = calls_of(project, 'a', 'go')['Path']
    assert (call.kind, call.label) == (EXTERNAL, 'pathlib')


def test_a_method_on_self_resolves_through_the_class(build):
    project, graph, _, _ = build(
        {
            'a.py': 'class C:\n    def go(self):\n        return self.helper()\n\n    def helper(self):\n        return 1\n'
        },
        'a.py',
    )
    assert graph.callees('a::C.go') == ['a::C.helper']


def test_a_method_inherited_from_a_project_base_still_resolves(build):
    project, graph, _, _ = build(
        {
            'a.py': 'class Base:\n    def helper(self):\n        return 1\n\n\nclass C(Base):\n    def go(self):\n        return self.helper()\n'
        },
        'a.py',
    )
    assert graph.callees('a::C.go') == ['a::Base.helper']


def test_a_call_on_an_unknown_receiver_is_dropped_not_guessed(build):
    """The whole conservatism argument, in one assertion.

    `thing` is a parameter, so its type is unknowable statically. Inventing an
    edge here would send a reader somewhere the code may never go, and they
    would have no way to tell that row from a real one.
    """
    project, graph, _, _ = build(
        {'a.py': 'def go(thing):\n    return thing.save()\n'}, 'a.py'
    )
    assert calls_of(project, 'a', 'go')['save'].kind == UNRESOLVED
    assert graph.callees('a::go') == []


def test_builtins_are_not_treated_as_external_surface(build):
    project, _, _, text = build(
        {'a.py': 'def go(xs):\n    return len(sorted(xs))\n'}, 'a.py'
    )
    assert calls_of(project, 'a', 'go')['len'].kind == UNRESOLVED
    assert '[builtins]' not in text


def test_logging_is_filtered_out_as_plumbing(build):
    project, _, _, text = build(
        {
            'a.py': 'import logging\n\n\ndef go():\n    logging.info("hi")\n    return 1\n'
        },
        'a.py',
    )
    assert '[logging]' not in text


def test_the_measured_noise_modules_lose_their_bracket(build):
    """The list in `graph.py`, asserted rather than re-argued.

    Membership was decided by measuring what the freed rows bought, not by a
    rule about what these calls are; the constants carry that evidence.
    """
    project, _, _, text = build(
        {
            'a.py': (
                'import itertools\n'
                'from functools import partial\n'
                'from collections import Counter\n'
                '\n\n'
                'def go(xs):\n'
                '    counts = Counter(xs)\n'
                '    f = partial(go, xs)\n'
                '    return list(itertools.chain(xs, [f, counts]))\n'
            )
        },
        'a.py',
    )
    assert '[itertools]' not in text
    assert '[functools]' not in text
    # `collections` was in that list until it was measured. Dropping it removes
    # 44 rows across the corpus and buys back repeat markers and duplicate
    # constructors, so it keeps its bracket. It is asserted here because it is
    # the case most likely to be added back on the strength of how similar it
    # looks to the two above.
    assert '[collections]' in text


def test_path_arithmetic_loses_its_bracket_but_filesystem_access_keeps_one(build):
    """`os` is judged one call at a time, because as a module it splits.

    Dropping `os.path.join` and its neighbours paid for itself in the corpus.
    Dropping `os.stat` would cost a reader the fact that this code goes to the
    filesystem, which is the sort of thing they are reading the map to find.
    """
    project, _, _, text = build(
        {
            'a.py': (
                'import os\n\n\n'
                'def go(d, name):\n'
                '    p = os.path.join(d, os.path.basename(name))\n'
                '    return os.stat(p)\n'
            )
        },
        'a.py',
    )
    calls = calls_of(project, 'a', 'go')
    assert calls['join'].kind == UNRESOLVED
    assert calls['basename'].kind == UNRESOLVED
    assert (calls['stat'].kind, calls['stat'].label) == (EXTERNAL, 'os')
    assert 'path.join' not in text


def test_path_arithmetic_is_recognised_however_it_was_imported(build):
    """One call written three ways resolves to one entry in the table."""
    project, _, _, _ = build(
        {
            'a.py': (
                'import os\n'
                'from os import path\n'
                'from os.path import join\n'
                '\n\n'
                'def go(d):\n'
                '    return os.path.join(d, path.join(d, join(d, "x")))\n'
            )
        },
        'a.py',
    )
    func = next(f for f in project.modules['a'].funcs if f.qualname == 'go')
    spellings = [c.dotted for c in func.calls if c.attr == 'join']
    assert sorted(spellings) == ['join', 'os.path.join', 'path.join']
    assert {c.kind for c in func.calls if c.attr == 'join'} == {UNRESOLVED}


def test_the_stdlib_is_recognised_for_ranking(build):
    """Used to rank externals, never to hide them; both kinds still bracket."""
    from recce.graph import is_stdlib

    assert is_stdlib('pathlib')
    assert is_stdlib('os')
    assert not is_stdlib('pydub')
    assert not is_stdlib('boto3')
