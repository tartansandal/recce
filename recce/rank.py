"""Decide what goes in the map: the spine, the noise, and what will not fit.

This is where recce earns or loses its keep. Producing the call graph is
mechanical, and a tool that dumps all of it is no better than `pyan`. The
filtering is the product, so the choices here are the ones to argue with:

- **What counts as trivial.** A one-line wrapper given its own row costs the
  same vertical space as the function doing the work, so it is collapsed. The
  test is structural (statement count, no branching, nothing called), not a
  guess about importance.
- **What earns a star.** Branch count is the strongest available proxy for
  "the interesting logic" — the reader is looking for the function that holds
  the decisions. Size, fan-in and fan-out break ties.
- **When to split.** The budget is a real constraint, not a suggestion, and a
  map that quietly runs to three screens has failed at its one job. So the tree
  is pruned down a fixed ladder, and when the bottom of that ladder still does
  not fit, the map splits instead of shrinking further. Past that a tree is cut
  to size and says how much went: the ladder only trades depth, and a wide tree
  reaches the bottom of it still over budget, so without the cut the constraint
  held everywhere except where it was binding.

Nothing here reads a function body's meaning. `Func.note` stays empty; that is
the slot a model fills in later.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .graph import Graph, external_display, is_stdlib
from .model import EXTERNAL, KEEP, PROJECT, SKIM, SPINE, TRIVIAL, Func, Project

# Decorators that mean "a framework calls this", so the function is a way in
# even though nothing in the project calls it.
_ENTRY_DECORATORS = (
    'route',
    'get',
    'post',
    'put',
    'patch',
    'delete',
    'command',
    'group',
    'task',
    'callback',
    'main',
    'handler',
    'entry_point',
)

# Decorators that mean the opposite: the function is machinery, and starting a
# read there would be a wrong turn.
_NON_ENTRY_DECORATORS = ('fixture', 'property', 'cached_property', 'setter', 'deleter')

# The depth caps tried in turn, deepest first.
_DEPTH_LADDER = (6, 5, 4, 3)

# The pruning ladder: what recce gives up to fit its budget, cheapest
# concession first. The priority lives here rather than in the nesting of
# loops, so it is something you can read and argue with — reorder these four
# lines and you have changed what recce sacrifices.
#
# The order says what recce believes is worth least. A repeated reference goes
# first, ahead even of notes: it is the only row on the page that carries
# nothing at all, being the second or third appearance of an edge the reader has
# already met, and dropping it hides no call. Notes go next, because a row the
# reader cannot see is a call they will not know about while a missing note only
# costs them a sentence they can get by opening the file. Then externals, then
# depth for the same reason — a reader four levels down has already left the
# flow the map is describing. Giving up a whole flow is dearer than any of them
# and is not on this list: `_fit` does that only once every rung here is spent.
#
# Dropping repeats pays for itself twice over. Measured across the corpus it
# shows 29 more distinct functions than leaving them in, because the rows it
# frees are spent on code the map had not reached, and it leaves fewer `… more`
# markers than before rather than more.
#
# A width concession — capping children per node, so a wide tree narrows
# instead of being cut at the end by `_truncate` — was written, measured and
# rejected. It sounds like the graceful version of the same idea and is the
# noisy one. Tight enough to matter (16, 8, 4) it removed every hard cut and
# took the corpus from 14 `… more` markers to 64, spreading an announcement of
# what is missing across every wide node, while showing 21 fewer distinct
# functions than dropping repeats alone. Loose enough to stay quiet (20, 12) it
# halved the hard cuts and changed little else, for triple the rungs. The
# premise was wrong: since `_truncate` learned to cut into a subtree rather than
# stop at it, one hard cut costs one marker in one place, and that is the
# quieter failure.
_CONCESSION_ORDER = (
    ('drop_repeat_refs', (False, True)),
    ('notes', ('all', 'spine', 'none')),
    ('external_depth', (99, 2)),
    ('depth_cap', _DEPTH_LADDER),
    ('drop_skim', (False, True)),
)


def _rungs():
    """Every combination of `_CONCESSION_ORDER`, cheapest concession first.

    Its first entry varies fastest, so every cheaper combination is tried
    before a dearer one is touched at all.
    """
    names = [name for name, _ in _CONCESSION_ORDER]
    grids = [values for _, values in _CONCESSION_ORDER]
    for combo in itertools.product(*reversed(grids)):
        yield dict(zip(reversed(names), combo, strict=True))


@dataclass
class Node:
    """One row in a rendered tree.

    A row is either a project function (`func` set) or an external call
    (`bracket` set), never both. The collapsed-helpers row is a third case,
    carrying the names it folded in `label` — and a `bracket` too, when those
    helpers call externals, so a set `bracket` does not mean the row is one.
    Read `label` first; `_row_tokens` had this backwards and quietly stopped
    matching the collapsed rows it was written to match.
    """

    label: str
    func: Optional[Func] = None
    ret: Optional[str] = None
    marker: str = ''
    bracket: Optional[str] = None
    children: List[Node] = field(default_factory=list)
    repeat: bool = False
    note: Optional[str] = None

    def line_count(self) -> int:
        # A note is a line, and the budget is about lines. Counting the row but
        # not its note is how a map with notes turned on quietly runs a third
        # longer than the budget it claims to keep.
        return (
            1
            + (1 if self.note else 0)
            + sum(child.line_count() for child in self.children)
        )


@dataclass
class Block:
    """One `##` section of the finished map: a heading and its own tree."""

    title: str
    purpose: Optional[str]
    roots: List[Node] = field(default_factory=list)
    # Drawn across module boundaries rather than being one module's block, so
    # it is not a module the omission counts should account for.
    spanning: bool = False

    def line_count(self) -> int:
        return sum(root.line_count() for root in self.roots)


@dataclass
class Plan:
    """Everything the renderer needs, with every judgement already made."""

    blocks: List[Block] = field(default_factory=list)
    strategy: str = 'single'  # 'single' | 'module' | 'entry'
    spine: List[Func] = field(default_factory=list)
    entries: List[Func] = field(default_factory=list)
    omitted_modules: int = 0
    # Test modules left out because this is a map of the source. Counted apart
    # from `omitted_modules`, which is source that did not fit.
    omitted_tests: int = 0


def annotate(project: Project, graph: Graph) -> List[Func]:
    """Score every function and assign it a role. Returns the entry points."""
    funcs = project.funcs()
    entries = _entry_points(project, graph)
    _assign_depth(funcs, graph, entries)
    _score(funcs, graph)
    _assign_roles(funcs, graph, entries)
    return entries


# The first tier that is a reading of the graph rather than a fact about the
# code. Everything at or past it is ordered by reach instead of by tier.
_INFERRED = 3

# The one tier that is a statement rather than a reading: a `[project.scripts]`
# target names an executable, in a manifest, on purpose. It is the only evidence
# the spanning block will build on.
_DECLARED = 0

# The `__main__`-guard tier. Trusted for naming a way in and not for leading a
# document, because in library code a guard usually marks a demo rather than
# the program: rich carries 43 of them, one at the foot of each module it can
# show off, and building the spanning block on those picked a private method of
# the traceback renderer as how rich fits together.
_GUARD = 1

# How deep a flow is followed when measuring what an entry point leads to.
# Matches the deepest rung of `_DEPTH_LADDER`, so reach measures the tree a
# reader could actually be shown rather than one the budget would never render.
_REACH_DEPTH = 6


def _reach_sets(funcs: Sequence[Func], graph: Graph) -> Dict[str, Set[str]]:
    """For each function, everything a reader following it would arrive at.

    Bounded rather than transitive-closed: past `_REACH_DEPTH` the reader has
    left the flow the block is describing, so counting further would rank an
    entry by code it leads to only in principle.
    """
    sets: Dict[str, Set[str]] = {}
    for func in funcs:
        seen: Set[str] = set()
        frontier = [(func.node_id, 0)]
        while frontier:
            node_id, depth = frontier.pop(0)
            if node_id in seen or depth > _REACH_DEPTH:
                continue
            seen.add(node_id)
            for callee in graph.callees(node_id):
                frontier.append((callee, depth + 1))
        sets[func.node_id] = seen
    return sets


def declared_ways_in(project: Project, graph: Graph) -> Dict[str, int]:
    """Ways in the code states, keyed by node id, valued by which tier said so.

    A `[project.scripts]` target, a `__main__` guard and a framework decorator
    are facts: the code is telling you how it is run. Everything else recce
    calls an entry point is read off the graph, where "nothing calls it" cannot
    be told from "nothing calls it yet".

    The distinction is worth its own function because the two are not
    interchangeable. Ordering a list of candidates tolerates a wrong guess —
    the reader sees a slightly odd second row. Leading the whole document with
    one does not, so `_spanning_block` will only build on these.
    """
    tiers: Dict[str, int] = {}

    def offer(func: Optional[Func], rank: int) -> None:
        if func is not None and func.node_id not in tiers:
            tiers[func.node_id] = rank

    for declared in project.declared_entries:
        offer(_resolve_declared(declared, project), 0)

    by_id = project.by_id()
    for module in project.modules.values():
        for target in graph.entry_calls.get(module.name, []):
            offer(by_id.get(target), 1)

    for func in by_id.values():
        tails = func.decorator_tails
        if any(d in _NON_ENTRY_DECORATORS for d in tails):
            continue
        if any(d in _ENTRY_DECORATORS for d in tails):
            offer(func, 2)
    return tiers


def _entry_points(project: Project, graph: Graph) -> List[Func]:
    """Find the ways in, best evidence first.

    A `__main__` guard is a fact about the file rather than an inference, so it
    outranks everything. A framework decorator is nearly as good. Only when
    neither exists do we fall back to graph shape, where "nothing in the
    project calls it" is suggestive but routinely wrong — an unused helper
    looks identical to an entry point from the graph alone.

    Which is exactly why the three inference tiers are ordered by reach and not
    by their own shape. They used to break ties on branch count, and on library
    code that picks the wrong thing every time: an uncalled utility with six
    branches outranks `api.get`, whose body is one delegation. requests came out
    as `help.main` and six `utils` helpers, with `api.get` tenth and
    `Session.request` absent for the crime of having callers. Reach is the
    measure that separates the two cases the docstring above admits look
    identical — a way in leads into the system, an unused helper leads nowhere —
    and it puts `Session.get` first, reaching 37 functions across 7 modules.

    Evidence still beats inference: a declared script, a `__main__` guard and a
    framework decorator keep their tiers, because those are facts about the code
    rather than readings of its shape. Reach only orders what is left.
    """
    by_id = project.by_id()
    ranked: List[Tuple[int, str, Func]] = []
    seen: Set[str] = set()

    def offer(func: Optional[Func], rank: int) -> None:
        if func is not None and func.node_id not in seen:
            seen.add(func.node_id)
            ranked.append((rank, func.node_id, func))

    # The tiers where the code says how it is run, rather than where recce
    # reads it off the graph. Kept apart because more than one caller needs to
    # know which of the two a way in came from.
    for node_id, rank in declared_ways_in(project, graph).items():
        offer(by_id.get(node_id), rank)

    for func in by_id.values():
        tails = func.decorator_tails
        if any(d in _NON_ENTRY_DECORATORS for d in tails):
            continue
        if func.node_id in seen:
            continue
        if func.name == 'main' and not func.is_method:
            offer(func, 3)
            continue
        if func.fan_in or not func.is_public or func.name.startswith('test_'):
            continue
        # A method with no callers is only a way in if the class is a real
        # object rather than a record; a dataclass field accessor is not.
        offer(func, 4 if not func.is_method else 5)

    reach = _reach_sets([func for _, _, func in ranked], graph)
    # `min(rank, _INFERRED)` collapses the three graph-shape tiers into one.
    # Ranking a public function above a public method was never evidence about
    # which is the way in, and it is what buried `Session.get` beneath every
    # uncalled helper in `utils`.
    ranked.sort(
        key=lambda item: (
            min(item[0], _INFERRED),
            -len(reach[item[2].node_id]),
            item[1],
        )
    )
    return [func for _, _, func in ranked]


def _resolve_declared(target: str, project: Project) -> Optional[Func]:
    """Find the function a `module:function` console-script target names.

    The module part is matched on a suffix, because `[project.scripts]` names
    it from the distribution root while recce may have been pointed at the
    package directory itself, giving the two different ideas of where names
    start.
    """
    module_part, _, func_part = target.partition(':')
    func_part = func_part.split('.')[0].strip() or 'main'
    module_part = module_part.strip()
    by_id = project.by_id()
    exact = by_id.get('{}::{}'.format(module_part, func_part))
    if exact is not None:
        return exact

    # A package usually re-exports its entry point: httpx declares
    # `httpx:main`, and `main` lives in `httpx._main` with the package
    # `__init__` importing it. The import table already records where the name
    # came from, so the indirection costs one lookup.
    owner = project.modules.get(module_part)
    if owner is not None:
        bound = owner.imports.get(func_part)
        if bound:
            real_module, _, real_name = bound.rpartition('.')
            found = by_id.get('{}::{}'.format(real_module, real_name))
            if found is not None:
                return found

    for name in project.modules:
        if (
            name == module_part
            or module_part.endswith('.' + name)
            or name.endswith('.' + module_part)
        ):
            found = by_id.get('{}::{}'.format(name, func_part))
            if found is not None:
                return found
    return None


def _assign_depth(funcs: Sequence[Func], graph: Graph, entries: Sequence[Func]) -> None:
    """Breadth-first call depth from the nearest entry point."""
    for func in funcs:
        func.depth = None
    frontier = [(func.node_id, 0) for func in entries]
    by_id = {func.node_id: func for func in funcs}
    while frontier:
        node_id, depth = frontier.pop(0)
        func = by_id.get(node_id)
        if func is None or (func.depth is not None and func.depth <= depth):
            continue
        func.depth = depth
        for callee in graph.callees(node_id):
            frontier.append((callee, depth + 1))


def _norm(values: Sequence[float]) -> Dict[int, float]:
    """Min-max normalise, treating an all-equal set as all-zero."""
    if not values:
        return {}
    low, high = min(values), max(values)
    span = high - low
    return {i: ((v - low) / span if span else 0.0) for i, v in enumerate(values)}


def _score(funcs: Sequence[Func], graph: Graph) -> None:
    """Rank functions by how likely they are to be worth reading first.

    The weights say branching matters most. That is the claim this scoring
    rests on, and it holds up because a reader opening unfamiliar code is
    looking for where the decisions live — a long function with no branches is
    usually a table or a builder, and skimming it loses nothing.
    """
    if not funcs:
        return
    branches = _norm([_effective_branches(f) for f in funcs])
    sizes = _norm([f.loc for f in funcs])
    fan_ins = _norm([f.fan_in for f in funcs])
    fan_outs = _norm([len(graph.callees(f.node_id)) for f in funcs])
    for index, func in enumerate(funcs):
        func.score = (
            (
                0.45 * branches[index]
                + 0.25 * sizes[index]
                + 0.15 * fan_ins[index]
                + 0.15 * fan_outs[index]
            )
            * _presentation_factor(func)
            * _constructor_factor(func)
        )


# A helper this size is met inside its caller and not navigated to. The bounds
# were set against the three helpers the code-map skill's fixtures name as
# trivial — `_parse_args`, `_parse_bytes`, `_human_bytes` — the largest of
# which is six lines with a loop and a branch in it.
_TRIVIAL_LOC = 8
_TRIVIAL_BRANCHES = 2


# Dunders that are wiring rather than behaviour. A constructor branches once
# per optional argument, which is the same shape as branching once per case in
# a dispatch table and means something entirely different.
_WIRING_METHODS = frozenset(
    {'__init__', '__new__', '__post_init__', '__repr__', '__str__', '__eq__'}
)


def _effective_branches(func: Func) -> float:
    """Branch count with conditional expressions worth half a decision each.

    `value if value is not None else default` is a defaulting idiom, not a
    fork the reader has to hold in their head, and a constructor with six of
    them is doing less thinking than a loop with two real branches. Counting
    them equally is one of the three ways a constructor came to outrank the
    function its module exists for; `_constructor_factor` has the case.

    Half rather than zero: a ternary inside a comprehension really is a
    decision, and the point is to stop them dominating, not to stop them
    counting.
    """
    return func.n_branches - 0.5 * func.n_ternaries


def _constructor_factor(func: Func) -> float:
    """Discount setup methods, which branch a lot and decide little.

    Found on `httpx`: `Client.__init__` and `AsyncClient.__init__` outscored
    every other method in `_client.py` and took the whole block's line budget,
    leaving `Client.send` — the function the module exists for — off the map.
    Argument wrangling looks like logic to a branch counter.

    A discount rather than a veto, because a constructor that really does the
    work should still be able to win.
    """
    return 0.6 if func.name in _WIRING_METHODS else 1.0


def _presentation_factor(func: Func) -> float:
    """Discount functions that are building output rather than deciding things.

    A report writer and an aggregator have the same shape to a branch counter:
    both are a loop over a collection with a couple of conditionals. The
    difference is what is inside the loop, and string literals are the cheapest
    reliable tell — roughly one per line means the body is text, not logic.

    This is a discount and not a veto on purpose. A formatter with genuinely
    intricate branching still outscores a plain one, which is the right
    outcome; it just no longer outscores the code that computes what it prints.
    """
    if func.loc < 4 or func.n_strings < 3:
        return 1.0
    density = func.n_strings / float(func.loc)
    return 0.65 if density >= 0.5 else 0.8


def _is_trivial(func: Func, graph: Graph) -> bool:
    """Whether a function is too small to deserve a row of its own.

    The test is structural, and the load-bearing part is that it is *not* the
    branch count. An earlier version required zero branches and let a six-line
    byte formatter with a loop in it through, because a `for` over four unit
    suffixes counts the same as a `for` over a million records.

    What separates a helper from a step is three things together:

    - it is a leaf in the project, calling nothing of ours
    - it is short, under `_TRIVIAL_LOC` lines
    - it is either private, or too small to hide anything at two statements

    All three have to hold. A public function is somebody's interface even when
    it is short, and a function that calls into the project is a step in the
    flow whatever its size, because the reader following that flow has to pass
    through it.
    """
    if graph.callees(func.node_id):
        return False
    if func.n_branches > _TRIVIAL_BRANCHES:
        return False
    if func.loc > _TRIVIAL_LOC:
        return False
    return not func.is_public or func.n_stmts <= 2


def _assign_roles(funcs: Sequence[Func], graph: Graph, entries: Sequence[Func]) -> None:
    """Write `role` onto every function: spine, skim, trivial, or plain."""
    entry_ids = {func.node_id for func in entries}
    for func in funcs:
        if func.node_id not in entry_ids and _is_trivial(func, graph):
            func.role = TRIVIAL
        else:
            func.role = KEEP

    kept = [f for f in funcs if f.role != TRIVIAL]
    if kept:
        cutoff = sorted(f.score for f in kept)[max(len(kept) // 3 - 1, 0)]
        for func in kept:
            if (
                func.node_id not in entry_ids
                and func.n_branches == 0
                and func.score <= cutoff
            ):
                func.role = SKIM

    for func in _pick_spine(funcs, entries, graph):
        func.role = SPINE


def _pick_spine(
    funcs: Sequence[Func], entries: Sequence[Func], graph: Graph
) -> List[Func]:
    """Choose the one to three functions to star.

    The first entry point is always one of them: it is where the reader starts
    whatever else is true, and a map whose star is buried three levels down
    tells them to start in the middle. The rest go to the highest-scoring
    functions that are actually reachable, because starring dead code is worse
    than starring nothing.

    Score here, where `_module_roots` and `_ensure_block_spine` use reach, and
    the difference is deliberate. A block root answers "what does the rest of
    this block hang off"; the project spine answers "where is the logic worth
    reading", and those are not the same question. Filling this list by reach
    was tried and broke two defences at once: `format_summary` reaches
    `_human_bytes` and so outranks `summarize`, undoing `_presentation_factor`,
    and on flat code every candidate reaches one function, so the tie-break
    decides and a two-line `click.main()` shim reaches the spine.

    The cost is that a block can lead with one function and star another —
    `rank.py` leads with `plan` and stars `_build_tree` inside it. That reads as
    "start here, the weight is there", which is worth more than making one
    marker mean two things.
    """
    chosen: List[Func] = []
    # The first entry point is where execution starts, which is not always
    # where reading should. A console script is very often a two-line shim —
    # flask declares `flask.cli:main`, whose whole body is `cli.main()` — and
    # starring it points the reader at a forwarding address. When the way in
    # has nothing in it, the star goes to something that does.
    for entry in entries[:2]:
        if not _is_trivial(entry, graph):
            chosen.append(entry)
            break
    reachable = [
        f
        for f in funcs
        if f.depth is not None and f.role != TRIVIAL and f not in chosen
    ]
    reachable.sort(key=lambda f: (-f.score, f.module, f.lineno))
    for func in reachable:
        if len(chosen) >= 3:
            break
        # A second star next door to the first says less than one further out.
        if func.n_branches == 0 and len(chosen) >= 2:
            continue
        chosen.append(func)
    if not chosen and funcs:
        chosen = [max(funcs, key=lambda f: f.score)]
    return chosen[:3]


def _module_order(project: Project) -> List[str]:
    """Project modules, leaves first, so a reader meets callees before callers.

    Kahn's algorithm with a deterministic tie-break. Import cycles are common
    enough in real code that a `CycleError` would be a bad outcome, so a cycle
    is broken by taking the alphabetically first remaining module and carrying
    on. The order degrades; nothing fails.
    """
    names = sorted(project.modules)
    deps: Dict[str, Set[str]] = {name: set() for name in names}
    for name, module in project.modules.items():
        for bound in module.imports.values():
            for other in names:
                if other != name and (bound == other or bound.startswith(other + '.')):
                    deps[name].add(other)

    ordered: List[str] = []
    remaining = set(names)
    while remaining:
        ready = sorted(n for n in remaining if not (deps[n] & remaining))
        if not ready:
            ready = [sorted(remaining)[0]]
        for name in ready:
            ordered.append(name)
            remaining.discard(name)
    return ordered


def _build_tree(
    root: Func,
    project: Project,
    graph: Graph,
    depth_cap: int = 6,
    emitted: Optional[Set[str]] = None,
    referenced: Optional[Set[str]] = None,
    members: Optional[Set[str]] = None,
    drop_skim: bool = False,
    external_depth: int = 99,
    notes: str = 'all',
    drop_repeat_refs: bool = False,
) -> Node:
    """Expand one entry point into a row tree, honouring the current budget."""
    by_id = project.by_id()
    if emitted is None:
        emitted = set()
    if referenced is None:
        referenced = set()

    def expand(func: Func, depth: int, path: Set[str]) -> Node:
        node = Node(
            label=func.qualname,
            func=func,
            ret=func.returns,
            marker=_marker_for(func),
            note=_note_for(func, notes),
        )
        emitted.add(func.node_id)
        if depth >= depth_cap or func.node_id in path:
            return node

        trivial: List[str] = []
        trivial_labels: List[str] = []
        for call in func.calls:
            if call.kind == PROJECT and call.target:
                callee = by_id.get(call.target)
                if callee is None or callee.node_id == func.node_id:
                    continue
                if members is not None and callee.node_id not in members:
                    # In a split map the edge still happened, and saying so is
                    # the point of splitting by module rather than by accident.
                    # It becomes a reference leaf — named the way the calling
                    # file writes it — and the callee's own block expands it.
                    #
                    # Marked `↑` on every appearance after the first, the way a
                    # repeated call inside the block is. Without it the same
                    # name arrives looking like news each time: four of
                    # requests' rows in one block are `_types.is_prepared()`,
                    # and nothing distinguished them from four different calls.
                    #
                    # This runs before the trivial check, and the asymmetry is
                    # deliberate. Collapsing a one-line helper into `…` is right
                    # inside a module, where the row would say nothing; a row
                    # naming another file says which file, which is the one
                    # thing a per-module block cannot otherwise tell you.
                    # Folding those in too was tried and saves four rows across
                    # the whole corpus, which does not pay for an edge out of
                    # the block going unnamed.
                    reference = '{}.{}()'.format(
                        callee.module.rsplit('.', 1)[-1], callee.qualname
                    )
                    if not any(c.label == reference for c in node.children):
                        seen_before = callee.node_id in referenced
                        referenced.add(callee.node_id)
                        if not (seen_before and drop_repeat_refs):
                            node.children.append(
                                Node(label=reference, repeat=seen_before)
                            )
                    continue
                if any(c.func is callee for c in node.children):
                    continue
                if callee.role == TRIVIAL:
                    # Collapsing the row must not lose what the helper touches.
                    # `_parse_args` is not worth a line, but the fact that this
                    # flow reaches argparse is worth keeping, so the helper's
                    # brackets move up onto the collapsed row.
                    if callee.name not in trivial:
                        trivial.append(callee.name)
                        for _, label in graph.externals.get(callee.node_id, []):
                            if label not in trivial_labels:
                                trivial_labels.append(label)
                    continue
                if (
                    drop_skim
                    and callee.role == SKIM
                    and not graph.callees(callee.node_id)
                ):
                    continue
                if callee.node_id in emitted:
                    node.children.append(
                        Node(
                            label=callee.qualname,
                            func=callee,
                            ret=callee.returns,
                            marker=_marker_for(callee),
                            repeat=True,
                        )
                    )
                    continue
                node.children.append(expand(callee, depth + 1, path | {func.node_id}))
            elif call.kind == PROJECT and call.label:
                label = '{}()'.format(call.label)
                if not any(c.label == label for c in node.children):
                    node.children.append(Node(label=label))
            elif (
                call.kind == EXTERNAL
                and call.label
                and depth < _external_cutoff(call.label, external_depth)
            ):
                display = '{}()'.format(external_display(call))
                if not any(c.label == display and c.bracket for c in node.children):
                    node.children.append(Node(label=display, bracket=call.label))

        if trivial:
            node.children.append(
                Node(
                    label='… {}'.format(', '.join(sorted(trivial)[:6])),
                    bracket=', '.join(trivial_labels[:3]) or None,
                )
            )
        return node

    return expand(root, 0, set())


def _external_cutoff(label: str, external_depth: int) -> int:
    """How deep a bracket of this kind is allowed to go.

    When the budget starts pushing externals out of the tree, the standard
    library goes first. `os.path.join` at depth three tells a reader nothing
    they did not already assume; `boto3.client` at the same depth tells them
    what this code talks to, which is one of the questions the map exists to
    answer.
    """
    if external_depth >= 99 or not is_stdlib(label):
        return external_depth + 2
    return external_depth


def _note_for(func: Func, mode: str) -> Optional[str]:
    """Whether this function's note survives the current budget rung."""
    if not func.note or mode == 'none':
        return None
    if mode == 'spine' and func.role != SPINE:
        return None
    return func.note


def _marker_for(func: Func) -> str:
    if func.role == SPINE:
        return '◆'
    if func.role == SKIM:
        return '~'
    return ''


def _mark_lead(roots: List[Node]) -> List[Node]:
    """Star the first row of a block: the row to start reading at.

    Two markers because the block's way in and its densest function are not
    reliably the same row, and one marker cannot say both. Where they are the
    same — 41 of the 49 starred blocks in the corpus — this overwrites the
    diamond and the block looks exactly as it always did. Where they differ it
    is the case worth telling apart: `rank.py` leads with `plan` and its weight
    is in `_build_tree` four levels down, and a single star had to choose
    between sending the reader to a row they cannot read first and saying
    nothing about where the work is.
    """
    if roots and roots[0].func is not None:
        roots[0].marker = '★'
    return roots


def _fit(
    roots: Sequence[Func],
    project: Project,
    graph: Graph,
    max_lines: int,
    members: Optional[Set[str]] = None,
    truncate: bool = True,
) -> List[Node]:
    """Expand roots into trees, pruning down a ladder until they fit.

    The rungs are the product of `_CONCESSION_ORDER`, cheapest concession
    first, so a map only pays for the constraint it actually hits. Each rung
    re-runs the cheaper ones beneath it, which is why four dimensions come to
    48 rungs rather than four steps.

    Only when every one is spent does the map show fewer flows, by keeping the
    prefix of root trees that fits. Dropping a whole flow is dearer than
    anything on the ladder, so it is genuinely last.

    Past that the last root is cut to fit rather than shipped over budget. It
    used to ship, on the grounds that a map four lines too long beats no map,
    and the overage turned out not to be four lines: every concession on the
    ladder trades away depth, and the trees that reach the bottom of it are
    wide, not deep. All fifteen blocks that came out over budget across the
    corpus were at the tightest depth already, and yt-dlp's `_real_extract`
    rendered 114 rows against a budget of 40. A budget missed by that much is
    not a budget.

    `truncate=False` is for a caller that needs the natural size rather than a
    fitted one — `_spanning_block` sizes a flow by how many block slots it
    would take, and a tree cut to one slot always looks like it takes one.
    """
    attempt: List[Node] = []
    for rung in _rungs():
        emitted: Set[str] = set()
        referenced: Set[str] = set()
        attempt = [
            _build_tree(
                root,
                project,
                graph,
                emitted=emitted,
                referenced=referenced,
                members=members,
                **rung,
            )
            for root in roots
        ]
        if sum(n.line_count() for n in attempt) <= max_lines:
            return attempt
    # Free once the trees exist: roots are already in score order, so the
    # prefix that fits drops the least interesting flows without rebuilding
    # anything. Rebuilding once per root count is what made this quadratic, and
    # on a package the size of asyncio it cost twenty-odd seconds.
    trimmed = _prefix_within(attempt, max_lines)
    if trimmed:
        return trimmed
    if not truncate:
        return attempt[:1]
    return [_truncate(attempt[0], max_lines)]


def _truncate(node: Node, budget: int) -> Node:
    """Cut a tree down to `budget` rows, saying how many went.

    Whole child subtrees, from the end, rather than a clean slice through the
    rows: a tree missing its last three branches is still a tree, where one cut
    mid-branch leaves rows indented under a parent that is no longer there.

    The count is exact and the row saying it is inside the budget, because a
    reader who cannot see that something was dropped has been misled rather
    than economised on — the same reason a module that did not fit is counted
    in the note at the top of the map.
    """
    if node.line_count() <= budget:
        return node
    own = 1 + (1 if node.note else 0)
    if own >= budget:
        node.children = []
        return node

    # Something is going to be dropped, so the row that says so is reserved up
    # front rather than clawed back afterwards.
    room = budget - own - 1
    kept: List[Node] = []
    rest = list(node.children)
    while rest and room > 0:
        child = rest.pop(0)
        if child.line_count() > room:
            # Cut into it rather than stopping at it. Keeping only whole
            # subtrees sounds tidier and spends the budget badly: yt-dlp's
            # widest block has two one-line children and then a large one, so
            # stopping gave three rows of a possible forty and a note saying 42
            # things were dropped.
            child = _truncate(child, room)
        kept.append(child)
        room -= child.line_count()

    node.children = kept
    if rest:
        node.children.append(Node(label='… {} more'.format(len(rest))))
    return node


def _prefix_within(nodes: Sequence[Node], max_lines: int) -> List[Node]:
    """The longest run of leading trees whose combined length fits."""
    total = 0
    kept: List[Node] = []
    for node in nodes:
        length = node.line_count()
        if total + length > max_lines:
            break
        total += length
        kept.append(node)
    return kept


def plan(
    project: Project, graph: Graph, max_lines: int = 40, kind: Optional[str] = None
) -> Plan:
    """Build the whole map structure, splitting if it will not fit.

    Whatever comes out of the bottom of `_fit`'s ladder still over budget gets
    split instead — one block
    per module when there is more than one, otherwise one block per entry
    point.
    """
    entries = annotate(project, graph)
    spine = [f for f in project.funcs() if f.role == SPINE]
    spine.sort(key=lambda f: (f.depth if f.depth is not None else 99, -f.score))
    result = Plan(strategy='single', spine=spine, entries=entries)

    roots = _roots_for(entries, project, graph)
    if _wants_module_split(project):
        result.strategy = 'module'
        result.blocks = _blocks_with_spanning(project, graph, max_lines, kind)
        result.omitted_modules, result.omitted_tests = _omissions(project, result, kind)
        return result

    nodes = _fit(roots, project, graph, max_lines)
    if sum(n.line_count() for n in nodes) <= max_lines:
        # A single-block map has no `##` heading, so this block's title and
        # purpose are never rendered — `render._heading` owns the document
        # heading and is the only thing that applies the purpose-provenance
        # rule. Filling them in here meant picking an arbitrary module out of
        # a dict and calling its docstring the whole map's purpose, which was
        # invisible only because nothing read it back.
        result.blocks = [Block(title='', purpose=None, roots=_mark_lead(nodes))]
        return result

    result.strategy = 'module' if len(project.modules) > 1 else 'entry'
    result.blocks = (
        _blocks_with_spanning(project, graph, max_lines, kind)
        if result.strategy == 'module'
        else _entry_blocks(project, graph, roots, max_lines)
    )
    if result.strategy == 'module':
        result.omitted_modules, result.omitted_tests = _omissions(project, result, kind)
    return result


# Past eight blocks the document stops being a map and becomes a listing.
_MAX_BLOCKS = 8

# What a spanning block has to be worth to take one of those eight slots from a
# module. Fewer modules than this and it is a module block wearing a different
# heading; fewer rows and it is a heading with nothing under it. Three rather
# than two because httpx's flow crossed exactly one boundary and led the map
# with a tree that a module block would have shown nearly as well.
_SPANNING_MIN_MODULES = 3
_SPANNING_MIN_ROWS = 5

# The most block slots one flow may buy. Past half the document the map has
# stopped being a map of the package and become one flow with offcuts.
_SPANNING_MAX_SLOTS = 4

# How much fragmentation has to be on the page before a flow drawn across the
# modules is worth the slot it takes from one. Set from the gap in the measured
# distribution: the code-map fixture cuts 2 calls, networkx 3 and json 4, and
# none of them is fragmented — networkx's blocks are independent algorithm
# modules that barely call each other. The next value up is recce at 10, then
# toolz 12, requests 40, black 60, yt-dlp 150, all of which are spending real
# rows on calls they cannot follow.
_SPANNING_MIN_STUBS = 10


def _iter_nodes(node: Node):
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _blocks_with_spanning(
    project: Project, graph: Graph, max_lines: int, kind: Optional[str] = None
) -> List[Block]:
    """Module blocks, led by one flow drawn across them where there is one.

    A map of per-module blocks answers what each file contains and never
    answers how they fit together, because every call leaving a module is cut
    to an unexpanded reference leaf — 17% of rendered rows on requests and 40%
    on yt-dlp are those stubs. One block drawn with no `members` restriction
    follows a flow wherever it goes, which is the view the rest of the document
    cannot give.

    It has to earn the slot it takes, so it is built first and kept only if it
    spans real ground. Where a codebase has no flow crossing a module boundary
    the block is not built and the map is exactly as it was.
    """
    modules = _module_blocks(project, graph, max_lines, _MAX_BLOCKS, kind)
    if kind in ('lib', 'test'):
        # `lib` says there is no one flow to lead with, `test` that the subject
        # is a suite whose modules seldom share one. Either way the answer is
        # the per-file blocks, and asking is cheaper than recce guessing.
        return modules
    if kind != 'app' and _stub_rows(modules) < _SPANNING_MIN_STUBS:
        # Nothing is being cut apart, so there is nothing to draw together. A
        # small package whose blocks already show the whole flow gets the map it
        # got before, rather than a fourth heading restating the other three.
        #
        # Skipped under `--type app`, because this is a guess about whether the
        # block is worth having and the reader has just answered that question.
        # The floors in `_spanning_block` still apply: they are about whether
        # there is a flow at all, which no flag can assert into existence.
        return modules
    spanning = _spanning_block(project, graph, max_lines, kind)
    if spanning is None:
        return modules
    slots = -(-spanning.line_count() // max_lines)
    budget = _MAX_BLOCKS - slots
    modules = _module_blocks(project, graph, max_lines, budget, kind)
    kept = [b for b in modules if not _restates(b, spanning)]
    if len(kept) < len(modules):
        # A dropped block leaves its slot free, and a module with no block at
        # all is worth more than a second drawing of one already on the page.
        # Rebuilt once rather than looped: the refill can only pull in modules
        # the spanning block was not rooted in, so a second round has nothing
        # left to drop.
        modules = _module_blocks(
            project, graph, max_lines, budget + (len(modules) - len(kept)), kind
        )
        kept = [b for b in modules if not _restates(b, spanning)]
    return [spanning] + kept


# `… 3 more` announces rows that were cut, and names none of them, so it is not
# content either block can be said to carry. Counting the text would make two
# blocks differ over how many rows each had to drop, which is a fact about their
# budgets rather than about what they say.
_MORE_ROW = re.compile(r'^…\s*\d+\s+more$')


def _row_tokens(node: Node, into: set) -> None:
    """Everything one row claims, named the way another block would name it.

    A function reached inside its own module is a `func` row; the same function
    reached from outside is a reference leaf with no `func` and a dotted label;
    and either can be folded into a `…` row that still names it. The three
    spellings have to collapse to one token or a block that says strictly less
    will not look like it.
    """
    if node.func is not None:
        into.add(node.func.qualname.split('.')[-1])
    elif node.label.startswith('…'):
        # Before the bracket test, not after it: a collapsed row carries one
        # whenever the helpers it folded call externals, so testing `bracket`
        # first threw away the names and kept the literal row text. That is
        # what stopped this firing on the map it was written for -- four of
        # `main_uip.py`'s stubs are named only inside block one's collapsed
        # row, and the two rows fold different numbers of helpers, so their
        # labels never matched.
        if not _MORE_ROW.match(node.label):
            for name in node.label.lstrip('… ').split(','):
                name = name.strip()
                if name:
                    into.add(name.split('.')[-1])
    elif node.bracket is not None:
        into.add(node.label)
    else:
        into.add(node.label.rstrip('()').split('.')[-1])
    for child in node.children:
        _row_tokens(child, into)


def _restates(block: Block, spanning: Block) -> bool:
    """Whether a module block says nothing the spanning block has not said.

    Blocks are deliberately self-contained and overlap between them is normal,
    so this is not a rule against repetition. It is about the one block that
    repeats everything: the module the spanning block is rooted in, whose block
    starts from the same function and follows it with every crossing collapsed
    back to a stub.

    Both conditions are required, and the second is why. On the map that raised
    this, `main_uip.py` held 58 rows, 15.5% of the document, and not one name
    the spanning block lacked. But yt-dlp's `__init__.py` block leads with the
    same `main` and is not a restatement: the spanning block spent its budget
    crossing modules and gave up `re.compile` and `getpass.getpass` inside
    `validate_options` to afford it, so the module block is where those appear.
    Dropping on the shared root alone would have taken that away.

    Comparing names rather than node ids is what lets a stub match the function
    it stands for, and the shared root is what makes the looseness safe: two
    unrelated functions of the same name cannot collide into a false drop
    unless this block is already rooted where the spanning block is.
    """
    if not block.roots or not spanning.roots:
        return False
    lead, span_lead = block.roots[0].func, spanning.roots[0].func
    if lead is None or span_lead is None or lead.node_id != span_lead.node_id:
        return False
    mine: set = set()
    for root in block.roots:
        _row_tokens(root, mine)
    theirs: set = set()
    for root in spanning.roots:
        _row_tokens(root, theirs)
    return mine <= theirs


def _stub_rows(blocks: Sequence[Block]) -> int:
    """Rows that name a call leaving the block and cannot follow it.

    This is the cost the spanning block exists to offset, so it is also what
    decides whether that block is worth a slot. A reference leaf has no `func`
    to expand and no bracket, and carries a dotted name written the way the
    calling file writes it.
    """
    return sum(
        1
        for block in blocks
        for root in block.roots
        for node in _iter_nodes(root)
        if node.func is None and not node.bracket and '.' in node.label
    )


def _spanning_block(
    project: Project, graph: Graph, max_lines: int, kind: Optional[str] = None
) -> Optional[Block]:
    """The one flow worth drawing across module boundaries, or nothing.

    Built only on a way in the code declares — a console script, a `__main__`
    guard, a framework decorator. Any inferred entry point will do to order a
    list, and will not do to lead a document. Allowing them produced a block on
    every large library, and on a library there is no dominant flow to find, so
    what led the map was whichever deep function happened to touch the most
    files: `Provider.ascii_company_email` for faker,
    `_SubqueryLoader.create_row_processor` for sqlalchemy, one downloader of
    hundreds for yt-dlp. Each was presented, by position, as how the package
    fits together. That is the guessed purpose line again in another costume.

    So where a project does not say how it is run, there is no spanning block
    and the map is the module blocks alone. `--type app` is the way to say it
    anyway when the entry point is one recce cannot see.

    Only `_DECLARED`, and the two weaker tiers are excluded for reasons worth
    keeping. A `__main__` guard marks a demo as readily as a program — see
    `_GUARD`. A framework decorator is a name match and nothing more: rich's
    `Traceback._render_stack` is decorated `@group`, which is in
    `_ENTRY_DECORATORS` and means "combine these renderables" rather than
    "something calls this", and it led rich's map until this was narrowed. Ways
    in that live in a test module are excluded too, which `_select_modules`
    already does for the module blocks and which this door bypassed.

    Among what remains, the one reaching the most modules wins. That is also
    what stops a wrapper leading: black declares both `main` and
    `patched_main`, and the wrapper reaches six modules where the function it
    wraps reaches ten.
    """
    tests = {m.name for m in project.modules.values() if _is_test_module(m)}
    ways_in = declared_ways_in(project, graph)
    by_id = project.by_id()
    if kind == 'app':
        # The reader has asserted what the manifest does not say, which is the
        # case this flag exists for: httpie, flake8 and pre-commit all declare
        # no console script, so the strict gate leaves them with no way to ask.
        # Every entry point is a candidate, and the reach ordering below picks
        # among them — the risk of an arbitrary pick is the reader's to take,
        # having said this is an application.
        pool = {f.node_id for f in _entry_points(project, graph)}
    else:
        pool = {n for n, tier in ways_in.items() if tier == _DECLARED}
    candidates = [by_id[n] for n in pool if n in by_id and by_id[n].module not in tests]
    if not candidates:
        return None
    reach = _reach_sets(candidates, graph)

    def spans(func: Func) -> int:
        return len({by_id[n].module for n in reach[func.node_id] if n in by_id})

    root = max(candidates, key=lambda f: (spans(f), len(reach[f.node_id]), f.node_id))
    if spans(root) < _SPANNING_MIN_MODULES:
        return None
    # Buy as many block slots as the flow needs, cheapest first. One slot is
    # one module's worth of budget, so a three-slot block displaces three module
    # blocks and the document stays bounded by `_MAX_BLOCKS * max_lines` exactly
    # as before — the flow is paid for in modules, not in extra length.
    #
    # It has to be bought rather than pruned because the pruning ladder cannot
    # reach these trees. Every concession on it trades away depth, and a
    # whole-program flow is wide: recce's is 106 lines at the tightest rung
    # available and cookiecutter's 62, unchanged by anything `_fit` can do. A
    # flat one-slot budget kept only the two that happened to fit and dropped
    # both of the ones worth having.
    nodes = None
    for slots in range(1, _SPANNING_MAX_SLOTS + 1):
        attempt = _fit([root], project, graph, slots * max_lines, truncate=False)
        if attempt[0].line_count() <= slots * max_lines:
            nodes = attempt
            break
    if nodes is None:
        return None
    drawn = {n.func.module for n in _iter_nodes(nodes[0]) if n.func is not None}
    if len(drawn) < _SPANNING_MIN_MODULES or nodes[0].line_count() < _SPANNING_MIN_ROWS:
        return None
    return Block(
        title='{}() across {} modules'.format(root.qualname, len(drawn)),
        purpose=root.doc,
        roots=_mark_lead(nodes),
        spanning=True,
    )


def _omissions(
    project: Project, result: Plan, kind: Optional[str] = None
) -> Tuple[int, int]:
    """What is missing from the map, split by why it is missing.

    Two reasons a module has no block, and the reader needs them apart. One is
    that it did not fit, and the answer is a tighter target or a bigger budget.
    The other is that it is a test module and this is a map of the source, where
    the answer is to point recce at the tests instead. Reporting them as one
    number sends a reader looking for source that was never missing.
    """
    with_funcs = [m for m in project.modules.values() if m.funcs]
    tests = sum(1 for m in with_funcs if _is_test_module(m))
    source = len(with_funcs) - tests
    # The spanning block is a flow, not a module, so it does not reduce the
    # count of modules still unshown.
    shown = sum(1 for b in result.blocks if not b.spanning)
    # With one exception. Where the module the spanning block is rooted in had
    # its own block dropped as a restatement, that module is on the page and
    # counting it as missing sends a reader looking for something they have
    # already read.
    span_root = next(
        (
            b.roots[0].func.module
            for b in result.blocks
            if b.spanning and b.roots and b.roots[0].func
        ),
        None,
    )
    if span_root is not None and not any(
        b.roots and b.roots[0].func and b.roots[0].func.module == span_root
        for b in result.blocks
        if not b.spanning
    ):
        shown += 1
    if kind == 'test':
        # The suite is the subject and the source was set aside on purpose, so
        # what is missing is the test modules that did not fit. Source is not
        # counted as absent: the reader asked for it to be.
        return max(tests - shown, 0), 0
    if not source:
        # Nothing but tests in the tree, so they are the subject by default and
        # are not an omission either.
        return max(len(with_funcs) - shown, 0), 0
    return max(source - shown, 0), tests


# Below this, a package reads better as one tree: the flow across two files is
# the thing worth seeing, and splitting it hides the only edge that matters.
# At three or more, per-module blocks win — each carries its own docstring as a
# purpose line, and the flat tree would have thrown all of them away to show a
# single spine the reader cannot navigate by.
_SPLIT_MODULE_COUNT = 3


def _wants_module_split(project: Project) -> bool:
    """Whether to split by module before even trying to fit one tree."""
    return sum(1 for m in project.modules.values() if m.funcs) >= _SPLIT_MODULE_COUNT


def _roots_for(entries: Sequence[Func], project: Project, graph: Graph) -> List[Func]:
    """Tree roots for a single-block map: complementary flows, best first.

    Taking the top four entries showed the same flow four times. requests ranks
    `Session.delete`, `Session.get`, `Session.head` and `Session.options`
    together — four public methods that each reach the same 37 functions,
    because each one delegates to `Session.request` and the work is downstream
    of that. Four spellings of one flow is not four flows.

    So after the first, each root is the entry that reaches the most functions
    *not already shown*. An entry adding nothing new is a different name for a
    map the reader already has, and is skipped rather than spent on a root.

    The first root is taken in rank order rather than by coverage, because a
    declared script or a `__main__` guard is where the program actually starts
    and that outranks how much of the project it happens to touch.
    """
    if not entries:
        funcs = project.funcs()
        return [max(funcs, key=lambda f: f.score)] if funcs else []

    reach = _reach_sets(entries, graph)
    chosen = [entries[0]]
    covered = set(reach[entries[0].node_id])
    while len(chosen) < 4:
        remaining = [f for f in entries if f not in chosen]
        if not remaining:
            break
        best = max(remaining, key=lambda f: len(reach[f.node_id] - covered))
        if not reach[best.node_id] - covered:
            break
        chosen.append(best)
        covered |= reach[best.node_id]
    return chosen


def _module_blocks(
    project: Project,
    graph: Graph,
    max_lines: int,
    max_blocks: int = _MAX_BLOCKS,
    kind: Optional[str] = None,
) -> List[Block]:
    """One block per module, leaves first, capped at what a reader will read.

    Past eight blocks the document stops being a map and becomes a listing, so
    a large package is cut to the modules carrying the highest-scoring code.
    The survivors are then put back into dependency order, because the cut is
    about which modules matter and the order is about how to read them.

    The count of what was dropped is reported rather than hidden — a reader who
    knows they are seeing eight modules of thirty can go and ask for the rest.
    """
    blocks: List[Block] = []
    chosen = _select_modules(project, max_blocks, kind)
    titles = _block_titles([project.modules[n].path for n in chosen])
    for name in chosen:
        module = project.modules[name]
        if not module.funcs:
            continue
        members = {f.node_id for f in module.funcs}
        _ensure_block_spine(module.funcs, _block_coverage(module, graph))
        roots = _module_roots(module, graph)
        # Each block is deliberately self-contained: a function shown in an
        # earlier block is still shown here, because a reader who jumps to one
        # module's block should not find its tree missing rows that happen to
        # have been spent elsewhere.
        nodes = _fit(roots, project, graph, max_lines, members=members)
        blocks.append(
            Block(
                title=titles[module.path],
                purpose=module.doc or module.header_comment,
                roots=_mark_lead(nodes),
            )
        )
    return blocks


def _ensure_block_spine(funcs: Sequence[Func], covers) -> None:
    """Star the way into a block that has no star yet.

    A block is a map in its own right, and one with no star tells the reader to
    start anywhere. The marker says "read first", so it goes to the function the
    rest of the block hangs off rather than the one holding the most branches —
    the same measure `_module_roots` picks its root by, and for the same reason.
    The overall spine list stays capped separately, so this only affects the
    markers inside the fence.
    """
    candidates = [f for f in funcs if f.role != TRIVIAL]
    if not candidates or any(f.role == SPINE for f in candidates):
        return
    max(candidates, key=lambda f: (covers(f), -f.lineno)).role = SPINE


def _block_titles(paths: Sequence[str]) -> Dict[str, str]:
    """Name each block by the shortest path suffix that is unique among them.

    A basename alone is usually right and occasionally ambiguous. Flask has
    `flask/app.py` and `flask/sansio/app.py`, and a map with two blocks both
    headed `app.py` is asking the reader to guess which is which — the one
    question a heading exists to answer.

    Only the blocks in this map are considered, so a name stays short unless
    something it is actually shown beside forces it longer.
    """
    titles: Dict[str, str] = {}
    for path in paths:
        parts = path.split('/')
        for depth in range(1, len(parts) + 1):
            candidate = '/'.join(parts[-depth:])
            clash = any(
                other != path and other.endswith('/' + candidate) for other in paths
            )
            if not clash:
                titles[path] = candidate
                break
        else:
            titles[path] = path
    return titles


def _is_test_module(module) -> bool:
    """Whether a module is a test suite rather than the thing under test."""
    name = module.path.rsplit('/', 1)[-1]
    return (
        name.startswith('test_')
        or name.endswith('_test.py')
        or '/tests/' in module.path
        or '/test/' in module.path
    )


def _select_modules(
    project: Project, max_blocks: int, kind: Optional[str] = None
) -> List[str]:
    """Which modules get a block, in the order they should be read.

    Mapping a package to understand the package and mapping a test suite to
    understand the suite are two jobs, and one document cannot do both — a map
    that is mostly source with two test blocks bolted on serves neither reader.
    So the tree decides which job this is: where there is source, the map is of
    the source and test modules are not eligible for a block at all; where there
    is nothing but tests, the tests are the subject and fill the map as they
    should. The selector is the path recce was pointed at, which is why
    `pkg/` and `pkg/tests/` give two different and equally correct maps.

    This used to be a ranking rather than an exclusion, and ranking cannot
    express it. Tests sorted last still take any slot the source does not fill,
    so a repository with seven source modules and eight slots spent its eighth
    on a test file; and the sort only ran when the modules outnumbered the
    slots, so a project with three of each — the case this docstring has always
    used as the thing that must not happen — returned all six untouched.
    """
    order = [n for n in _module_order(project) if project.modules[n].funcs]
    source = [n for n in order if not _is_test_module(project.modules[n])]
    # `--type test` says the suite is the subject even where source sits beside
    # it, which is the one case the tree cannot show: a repository root holds
    # both, and only the reader knows which they came to read. It mirrors the
    # default exactly — the default keeps source and drops tests, this keeps
    # tests and drops source — because a map that mixed them would be the
    # half-of-each document neither reader wants.
    if kind == 'test':
        eligible = [n for n in order if _is_test_module(project.modules[n])] or order
    else:
        eligible = source or order
    if len(eligible) <= max_blocks:
        return eligible
    ranked = sorted(
        eligible, key=lambda n: -max(f.score for f in project.modules[n].funcs)
    )
    keep = set(ranked[:max_blocks])
    return [n for n in eligible if n in keep]


def _module_roots(module, graph: Graph) -> List[Func]:
    """Within one module, the functions nothing else in that module calls.

    Trivial functions are filtered out even though they qualify. A module full
    of `@property` accessors has a dozen uncalled one-liners, and every one of
    them is a graph root; listing them buries whatever the module is actually
    for under its own boilerplate. They come back only if the filter would
    leave the block empty, which happens for pure record modules where the
    accessors genuinely are the content.
    """
    internal = set()
    for func in module.funcs:
        for callee in graph.callees(func.node_id):
            internal.add(callee)
    roots = [f for f in module.funcs if f.node_id not in internal]
    substantial = [f for f in roots if f.role != TRIVIAL]
    chosen = substantial or roots
    covers = _block_coverage(module, graph)
    chosen.sort(key=lambda f: (-covers(f), f.lineno))

    # Being uncalled is what makes something a way in, but it is not what
    # makes it worth reading, and in a class-heavy module the two come apart
    # badly. Having callers disqualifies you, which leaves the constructors
    # and the thin public wrappers as roots while the code doing the work sits
    # one level down and can never be one.
    #
    # It is the third way into the `_constructor_factor` case: `Client.send`
    # has two internal callers, so being called at all kept the best function
    # in httpx's `_client.py` out of the roots while the constructors led.
    # The module's best-scoring function is promoted when nothing else would
    # have put it near the top.
    best = _best_in(module.funcs, covers)
    if best is not None and best not in chosen[:2]:
        chosen.insert(0, best)

    return chosen or sorted(module.funcs, key=lambda f: f.lineno)[:1]


def _block_coverage(module, graph: Graph):
    """How much of this module's own code each of its functions leads to.

    The measure a block wants is not the same one a star wants. Score asks
    which function holds the most decisions; a block root has to ask which one
    the rest of the block hangs off, and those come apart exactly where it
    matters. `rank.py` scored `_build_tree` above `plan`, so the block for the
    module this tool is built around led with a private helper and `plan`
    appeared below it or, once it grew, not at all. `render.py` led with
    `_data_section` rather than `render`.

    Restricted to the module's own functions because a block is drawn with
    `members` set: a callee in another module renders as an unexpanded
    reference leaf, so reach that leaves the module buys no rows here and
    should not decide which root leads.
    """
    members = {f.node_id for f in module.funcs}
    reach = _reach_sets(module.funcs, graph)

    def covers(func: Func) -> int:
        return len(reach[func.node_id] & members)

    return covers


def _best_in(funcs: Sequence[Func], covers) -> Optional[Func]:
    """The function a block is most usefully read from, trivia excluded."""
    candidates = [f for f in funcs if f.role != TRIVIAL]
    return max(candidates, key=lambda f: (covers(f), -f.lineno)) if candidates else None


def _entry_blocks(
    project: Project, graph: Graph, roots: Sequence[Func], max_lines: int
) -> List[Block]:
    """One block per top-level flow, for a single file too big for one tree."""
    blocks: List[Block] = []
    for root in roots:
        emitted: Set[str] = set()
        node = _build_tree(root, project, graph, 4, emitted)
        blocks.append(
            Block(
                title='{}() flow'.format(root.qualname),
                purpose=root.doc,
                roots=_mark_lead([node]),
            )
        )
    return blocks


def rendered_funcs(mapping: Plan) -> List[Func]:
    """Every project function the plan actually put on the page.

    The map is not the project. `rich` is 100 modules and renders 8, so
    ranking the whole project to decide what is worth a note spends half the
    asks on rows that were never going to exist. Planning once to find out
    which functions survive, and only then asking about them, is what this is
    for -- see the call in `cli`.

    External calls have no `func` and contribute nothing, which is right: a
    bracket at the right margin is not somewhere a note could hang.
    """
    found: List[Func] = []

    def walk(node: Node) -> None:
        if node.func is not None:
            found.append(node.func)
        for child in node.children:
            walk(child)

    for block in mapping.blocks:
        for root in block.roots:
            walk(root)
    return found
