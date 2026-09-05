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
  not fit, the map splits instead of shrinking further.

Nothing here reads a function body's meaning. `Func.note` stays empty; that is
the slot a model fills in later.
"""

from __future__ import annotations

import itertools
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
# The order says what recce believes is worth least. Notes go before rows,
# because a row the reader cannot see is a call they will not know about,
# while a missing note only costs them a sentence they can get by opening the
# file. Then externals, then depth for the same reason — a reader four levels
# down has already left the flow the map is describing. Giving up a whole flow
# is dearer than any of them and is not on this list: `_fit` does that only
# once every rung here is spent.
_CONCESSION_ORDER = (
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
    (`bracket` set), never both. The collapsed-helpers row is a third case with
    neither, carrying its text in `label`.
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

    for declared in project.declared_entries:
        offer(_resolve_declared(declared, project), 0)

    for module in project.modules.values():
        for target in graph.entry_calls.get(module.name, []):
            offer(by_id.get(target), 1)

    # One pass, in rank order: `offer` keeps the first rank a function is
    # given, so checking the declared ways in before the graph-shape fallback
    # is what makes a decorated entry point outrank its own lack of callers.
    for func in by_id.values():
        tails = func.decorator_tails
        if any(d in _NON_ENTRY_DECORATORS for d in tails):
            continue
        if any(d in _ENTRY_DECORATORS for d in tails):
            offer(func, 2)
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
    members: Optional[Set[str]] = None,
    drop_skim: bool = False,
    external_depth: int = 99,
    notes: str = 'all',
) -> Node:
    """Expand one entry point into a row tree, honouring the current budget."""
    by_id = project.by_id()
    if emitted is None:
        emitted = set()

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
                    reference = '{}.{}()'.format(
                        callee.module.rsplit('.', 1)[-1], callee.qualname
                    )
                    if not any(c.label == reference for c in node.children):
                        node.children.append(Node(label=reference))
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
        return '★'
    if func.role == SKIM:
        return '~'
    return ''


def _fit(
    roots: Sequence[Func],
    project: Project,
    graph: Graph,
    max_lines: int,
    members: Optional[Set[str]] = None,
) -> List[Node]:
    """Expand roots into trees, pruning down a ladder until they fit.

    The rungs are the product of `_CONCESSION_ORDER`, cheapest concession
    first, so a map only pays for the constraint it actually hits. Each rung
    re-runs the cheaper ones beneath it, which is why four dimensions come to
    48 rungs rather than four steps.

    Only when every one is spent does the map show fewer flows, by keeping the
    prefix of root trees that fits. Dropping a whole flow is dearer than
    anything on the ladder, so it is genuinely last.

    Something always comes back. If even one root is over budget, that is what
    ships, because a map that is four lines too long is a worse failure than
    no map at all only in a spec.
    """
    attempt: List[Node] = []
    for rung in _rungs():
        emitted: Set[str] = set()
        attempt = [
            _build_tree(root, project, graph, emitted=emitted, members=members, **rung)
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
    return attempt[:1]


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


def plan(project: Project, graph: Graph, max_lines: int = 40) -> Plan:
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
        result.blocks = _module_blocks(project, graph, max_lines)
        result.omitted_modules, result.omitted_tests = _omissions(project, result)
        return result

    nodes = _fit(roots, project, graph, max_lines)
    if sum(n.line_count() for n in nodes) <= max_lines:
        # A single-block map has no `##` heading, so this block's title and
        # purpose are never rendered — `render._heading` owns the document
        # heading and is the only thing that applies the purpose-provenance
        # rule. Filling them in here meant picking an arbitrary module out of
        # a dict and calling its docstring the whole map's purpose, which was
        # invisible only because nothing read it back.
        result.blocks = [Block(title='', purpose=None, roots=nodes)]
        return result

    result.strategy = 'module' if len(project.modules) > 1 else 'entry'
    result.blocks = (
        _module_blocks(project, graph, max_lines)
        if result.strategy == 'module'
        else _entry_blocks(project, graph, roots, max_lines)
    )
    if result.strategy == 'module':
        result.omitted_modules, result.omitted_tests = _omissions(project, result)
    return result


def _omissions(project: Project, result: Plan) -> Tuple[int, int]:
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
    if not source:
        # A map of a test suite: the tests are the subject, not an omission.
        return max(len(with_funcs) - len(result.blocks), 0), 0
    return max(source - len(result.blocks), 0), tests


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
    project: Project, graph: Graph, max_lines: int, max_blocks: int = 8
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
    chosen = _select_modules(project, max_blocks)
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
                roots=nodes,
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


def _select_modules(project: Project, max_blocks: int) -> List[str]:
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
                roots=[node],
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
