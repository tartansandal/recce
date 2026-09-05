"""Resolve call sites to their targets and build the call graph.

Resolution is name-based and deliberately conservative. Every call is decided
by the root of its dotted chain against three tables — the file's imports, the
module's own top-level names, and the enclosing class's method resolution
order — and a call none of them explain is dropped rather than guessed at.

Dropping is the important half. A map that omits an edge costs the reader a
`grep`; a map that invents one sends them somewhere the code never goes, and
they have no way to tell the two apart from the map alone. So `getattr`
dispatch, callbacks stored in dicts, and methods on objects of unknown type all
come out as nothing, and the limitation is documented rather than papered over.

What survives the pass is a classification on every `Call`:

- `PROJECT` with a `target` node id, which becomes an edge in the tree
- `EXTERNAL` with a `label`, which becomes a `[bracket]` annotation
- `UNRESOLVED`, which the renderer never shows
"""

from __future__ import annotations

import builtins
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .model import EXTERNAL, PROJECT, Call, Class, Func, Project

_BUILTINS = frozenset(dir(builtins))

# Every top-level module the standard library ships, from 3.10 on. Used to
# rank, never to hide: a call into `pathlib` and a call into `pydub` are both
# external surface and both get a bracket. But when the budget forces externals
# out of a tree, the third-party ones are the ones worth keeping — they say
# what this code depends on, where `os` and `sys` say only that it is Python.
_STDLIB = frozenset(sys.stdlib_module_names)


def is_stdlib(label: str) -> bool:
    return label.split('.')[0] in _STDLIB


# Names that are almost always plumbing rather than a destination. They pass
# the import test and would otherwise fill the map with rows nobody navigates
# to. Logging is the worst offender: it appears in every function and tells the
# reader nothing about the shape of the code.
#
# Note what this is and is not. `_STDLIB` above ranks and never hides, because
# being standard library is not grounds for hiding anything — `pathlib` and
# `pydub` are both surface. The grounds here are different: these calls are pure
# operations on values the caller already has, so the row says what the code is
# built out of rather than what it does. That criterion, not stdlib membership,
# is what admits a name to this list.
#
# The second group was measured rather than guessed. Over the corpus these
# accounted for 73 rendered rows, and reading them back not one said anything
# about the behaviour of the code containing it: `partial`, `chain`,
# `itemgetter` and `zip_longest` describe the plumbing a function is assembled
# from. `gettext` is the same case as logging — `_()` wraps every user-facing
# string in argparse and marks nothing.
#
# `collections` was in this list and came out, which is the useful part of the
# record. It looks identical from here — `defaultdict(int)` is a container
# choice the way `partial` is a call choice — but the skill checklist asserts
# `Counter()` brackets in the log-summariser fixture, and it is right to. A
# `Counter` says the function counts things, which is a fact about the data the
# code builds rather than about how it was assembled. `chain` says only that
# something was iterated.
_NOISE_LABELS = frozenset(
    {
        'logging',
        'typing',
        'warnings',
        '__future__',
        'itertools',
        'functools',
        'operator',
        'gettext',
    }
)

# Path-name manipulation: computing a path from a path. `os` cannot go in the
# list above because it is the one module that genuinely mixes the two kinds —
# it was the largest single label in the corpus at 76 rendered rows, and half of
# them were these. The other half are `os.stat`, `os.remove`, `os.write`,
# `path.exists`, `environ.get`, and those stay, because a reader orienting in
# unfamiliar code does need to know it touches the filesystem and reads the
# environment. Only the arithmetic goes.
#
# Keyed on the full dotted name so every import spelling resolves to the same
# entry: `os.path.join(...)`, `from os import path` then `path.join(...)`, and
# `from os.path import join` then `join(...)` are one call, written three ways.
#
# `pathlib` looks like the same case and is not, which is worth recording
# because it was tried. Constructing a `Path` reads as plumbing, but `Path` is
# routinely the head of a chain that does the work — `Path(p).read_text()` is
# one call site recce can name and one it cannot, so dropping the construction
# drops the only evidence the function touches a disk. `os.path.join` is never
# the head of anything. The measured prize was seven rows in the whole corpus
# against losing that, so pathlib keeps its bracket.
_PLUMBING_CALLS = frozenset(
    {
        'os.path.join',
        'os.path.basename',
        'os.path.dirname',
        'os.path.split',
        'os.path.splitext',
        'os.path.abspath',
        'os.path.normpath',
        'os.path.realpath',
        'os.path.relpath',
        'os.path.expanduser',
        'os.path.expandvars',
        'os.path.isabs',
        'os.fspath',
    }
)


def _is_plumbing(call: Call, label: str, bound: str) -> bool:
    """Whether this external call is assembly rather than behaviour."""
    if label in _NOISE_LABELS:
        return True
    # `bound` is where the root name came from and `call.dotted` is how this
    # file wrote the chain, so replacing the root with its binding rebuilds the
    # full name whichever way the import was spelled.
    return bound + call.dotted[len(call.root or '') :] in _PLUMBING_CALLS


@dataclass
class Graph:
    """The resolved call graph, keyed by `Func.node_id` throughout."""

    out: Dict[str, List[str]] = field(default_factory=dict)
    fan_in: Dict[str, int] = field(default_factory=dict)
    externals: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    entry_calls: Dict[str, List[str]] = field(default_factory=dict)

    def callees(self, node_id: str) -> List[str]:
        return self.out.get(node_id, [])


class _Resolver:
    """Indexes over a `Project`, built once and queried per call site."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.modules = project.modules
        self.funcs = project.by_id()
        self.classes: Dict[str, Class] = {}
        for module in self.modules.values():
            for cls in module.classes:
                self.classes['{}::{}'.format(module.name, cls.name)] = cls

    def module_of(self, dotted: str) -> Optional[str]:
        """Longest project module name that is a prefix of a dotted path.

        `pkg.parser.parse_line` has to resolve against `pkg.parser` even though
        `pkg` is also a module, so the longest match is the right one.
        """
        if dotted in self.modules:
            return dotted
        best = None
        for name in self.modules:
            if dotted.startswith(name + '.'):
                if best is None or len(name) > len(best):
                    best = name
        return best

    def func_in(self, module: str, qualname: str) -> Optional[Func]:
        return self.funcs.get('{}::{}'.format(module, qualname))

    def method_via_mro(self, module: str, cls_name: str, attr: str) -> Optional[Func]:
        """Find `attr` on a class or its project-visible ancestors.

        Bases are followed only while they stay inside the project. An
        inherited method from a framework base class is not a place in this
        codebase, so the search stops there and the call goes unresolved.
        """
        seen: Set[str] = set()
        pending = [(module, cls_name)]
        while pending:
            mod, name = pending.pop(0)
            key = '{}::{}'.format(mod, name)
            if key in seen:
                continue
            seen.add(key)
            found = self.func_in(mod, '{}.{}'.format(name, attr))
            if found is not None:
                return found
            cls = self.classes.get(key)
            if cls is None:
                continue
            for base in cls.bases:
                base_mod, base_name = self._locate_class(mod, base)
                if base_mod is not None:
                    pending.append((base_mod, base_name))
        return None

    def _locate_class(self, module: str, dotted: str) -> Tuple[Optional[str], str]:
        """Where a base-class expression points, if it points into the project."""
        tail = dotted.split('.')[-1]
        if '{}::{}'.format(module, tail) in self.classes:
            return module, tail
        imports = self.modules[module].imports if module in self.modules else {}
        root = dotted.split('.')[0]
        bound = imports.get(root)
        if bound:
            owner = self.module_of(bound)
            if owner and '{}::{}'.format(owner, tail) in self.classes:
                return owner, tail
            if owner and owner != bound:
                return owner, tail
        return None, tail


def resolve(project: Project) -> Graph:
    """Classify every call site in the project and return the call graph."""
    resolver = _Resolver(project)
    graph = Graph()

    for module in project.modules.values():
        for func in module.funcs:
            targets: List[str] = []
            externals: List[Tuple[str, str]] = []
            seen_targets: Set[str] = set()
            seen_externals: Set[str] = set()
            for call in func.calls:
                _classify(call, resolver, module.name, func.cls)
                if call.kind == PROJECT and call.target:
                    if call.target != func.node_id and call.target not in seen_targets:
                        seen_targets.add(call.target)
                        targets.append(call.target)
                elif call.kind == EXTERNAL and call.label:
                    display = external_display(call)
                    if display not in seen_externals:
                        seen_externals.add(display)
                        externals.append((display, call.label))
            graph.out[func.node_id] = targets
            graph.externals[func.node_id] = externals

        for call in module.main_calls:
            _classify(call, resolver, module.name, None)
        graph.entry_calls[module.name] = [
            c.target for c in module.main_calls if c.kind == PROJECT and c.target
        ]

    for callees in graph.out.values():
        for callee in callees:
            graph.fan_in[callee] = graph.fan_in.get(callee, 0) + 1
    for func in project.funcs():
        func.fan_in = graph.fan_in.get(func.node_id, 0)

    return graph


def external_display(call: Call) -> str:
    """How an external call is written, wherever it is written.

    The last two components carry the meaning — `AudioSegment.export` says more
    than `export` and less than the full dotted path, which would repeat the
    bracket label sitting at the end of the same row.

    This is the single owner of that rule. The renderer shows the same call in
    two places — as a tree row, and folded onto a collapsed helper row — and
    when each had its own copy of the `parts[-2:]` slice they were one edit
    away from disagreeing about what the same call is called.
    """
    parts = call.dotted.split('.')
    return '.'.join(parts[-2:]) if len(parts) > 1 else call.dotted


def _classify(call: Call, resolver: _Resolver, module: str, cls: Optional[str]) -> None:
    """Decide what one call site points at, writing the answer onto the Call."""
    if call.root is None:
        return  # chain did not bottom out in a name; nothing to key on

    mod = resolver.modules[module]

    # A method on self or cls: search the class and its project ancestors.
    if call.root in ('self', 'cls') and cls:
        found = resolver.method_via_mro(module, cls, call.attr)
        if found is not None:
            call.kind = PROJECT
            call.target = found.node_id
        return

    # A name this file imported. Whether it is ours depends on where it came
    # from, which is the one question the import table answers.
    bound = mod.imports.get(call.root)
    if bound is not None:
        owner = resolver.module_of(bound)
        if owner is not None:
            _resolve_project_import(call, resolver, owner, bound)
        else:
            label = bound.split('.')[0]
            if not _is_plumbing(call, label, bound):
                call.kind = EXTERNAL
                call.label = label
        return

    # A name this module defines at the top level.
    if _resolve_local(call, resolver, module):
        return

    if call.root in _BUILTINS:
        return  # `len`, `str`, `open`: noise, not surface

    return


def _resolve_project_import(
    call: Call, resolver: _Resolver, owner: str, bound: str
) -> None:
    """Resolve a call whose root was imported from another project module."""
    if bound == owner:
        # `from . import parser` then `parser.parse_line()`.
        found = resolver.func_in(owner, call.attr)
        if found is not None:
            call.kind = PROJECT
            call.target = found.node_id
            return
        if '{}::{}'.format(owner, call.attr) in resolver.classes:
            call.kind = PROJECT
            call.label = call.attr  # a constructor; no body to descend into
        return

    # `from .parser import parse_line` then `parse_line()`, or a method on an
    # imported class: `Parser.build()`.
    name = bound[len(owner) + 1 :]
    if call.attr == call.root:
        found = resolver.func_in(owner, name)
    else:
        found = resolver.func_in(owner, '{}.{}'.format(name, call.attr))
        if found is None:
            found = resolver.method_via_mro(owner, name, call.attr)
    if found is not None:
        call.kind = PROJECT
        call.target = found.node_id
        return
    if '{}::{}'.format(owner, name) in resolver.classes:
        call.kind = PROJECT
        call.label = name


def _resolve_local(call: Call, resolver: _Resolver, module: str) -> bool:
    """Resolve a call against the names the calling module defines itself."""
    if call.attr == call.root:
        found = resolver.func_in(module, call.root)
        if found is not None:
            call.kind = PROJECT
            call.target = found.node_id
            return True
        if '{}::{}'.format(module, call.root) in resolver.classes:
            call.kind = PROJECT
            call.label = call.root
            return True
        return False

    found = resolver.func_in(module, '{}.{}'.format(call.root, call.attr))
    if found is None:
        found = resolver.method_via_mro(module, call.root, call.attr)
    if found is not None:
        call.kind = PROJECT
        call.target = found.node_id
        return True
    return False
