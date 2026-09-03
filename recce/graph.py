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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .model import EXTERNAL, PROJECT, Call, Class, Func, Project

_BUILTINS = frozenset(dir(builtins))

# Names that are almost always plumbing rather than a destination. They pass
# the import test and would otherwise fill the map with rows nobody navigates
# to. Logging is the worst offender: it appears in every function and tells the
# reader nothing about the shape of the code.
_NOISE_LABELS = frozenset({'logging', 'typing', 'warnings', '__future__'})


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
                    display = _external_display(call)
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


def _external_display(call: Call) -> str:
    """How an external call is written in the tree.

    The last two components carry the meaning — `AudioSegment.export` says more
    than `export` and less than the full dotted path, which would repeat the
    bracket label sitting at the end of the same row.
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
            if label not in _NOISE_LABELS:
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
