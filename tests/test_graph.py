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
