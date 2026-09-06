"""The records the extractor produces and everything downstream reads.

These are deliberately plain dataclasses with no behaviour beyond a couple of
derived properties. The pipeline is four passes over the same objects — extract
fills them in, graph resolves the call targets, rank writes the scoring fields,
render only reads — and keeping them dumb is what lets `--json` dump the whole
intermediate state for the model stage to consume later.

The mutable scoring fields (`score`, `depth`, `fan_in`, `role`, `note`) start at
their neutral values and are written by later passes. `note` is the one field
nothing in the deterministic pipeline sets: it is where a paraphrase of the
function's loop and branch shape goes when a model is in the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple

# How a call site was resolved. 'project' means we found the callee among the
# modules we parsed and can draw an edge to it; 'external' means it belongs to
# the stdlib, a framework, or a third-party package and earns a `[bracket]`;
# 'unresolved' means the receiver was a local of unknown type, which is the
# normal outcome for dynamic dispatch and is dropped rather than guessed at.
PROJECT = 'project'
EXTERNAL = 'external'
UNRESOLVED = 'unresolved'

# The roles rank.py assigns, and the marker each one renders as.
SPINE = 'spine'  # -> a star; the 1-3 functions holding the interesting logic
KEEP = 'keep'  # -> no marker; ordinary rows
SKIM = 'skim'  # -> a tilde
TRIVIAL = 'trivial'  # -> an ellipsis, or inlined into the parent row


@dataclass
class Call:
    """One call site inside a function body.

    `dotted` is the source text of the callee expression, so `Path.home` for
    `Path.home()`. `root` is the leftmost name in that chain, which is what the
    resolver keys on — for `pydub.AudioSegment.export()` the root is `pydub`,
    and the import table says whether that is ours or someone else's.
    """

    dotted: str
    root: Optional[str]
    attr: str
    lineno: int
    kind: str = UNRESOLVED
    target: Optional[str] = None  # node id, when kind is PROJECT
    label: Optional[str] = None  # bracket text, when kind is EXTERNAL


@dataclass
class Phase:
    """A run of statements a comment inside a function body names.

    Poor code leaves a phase of work inline where good code would have made it
    a function, so the map inherits the missing name: the phase arrives as a
    run of sibling call rows with nothing to call it. Usually the author named
    it anyway, in a comment, and that is what this records — read off the file,
    never inferred, so it carries the same authority as a purpose line.

    `label` is the comment text verbatim, which is what makes the row worth
    having: it is greppable, so the reader can find the lines it stands for
    without the row spending characters on a line number.
    """

    label: str
    start: int
    end: int


@dataclass
class Func:
    """A module-level function or a method.

    Nested functions are not recorded separately. Their calls are folded into
    the enclosing function instead, because a closure defined and used in one
    place is part of that function's shape rather than a destination a reader
    navigates to.
    """

    name: str
    qualname: str
    module: str
    path: str
    lineno: int
    end_lineno: int
    args: List[str]
    returns: Optional[str]
    doc: Optional[str]
    # Keys of the dict literal this function returns, when it returns one.
    # Python code that passes records around as dicts has data shapes that no
    # annotation records, and this is the only place they are written down.
    returns_keys: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    calls: List[Call] = field(default_factory=list)
    n_stmts: int = 0
    n_branches: int = 0
    n_loops: int = 0
    n_ternaries: int = 0
    n_strings: int = 0
    loc: int = 0
    cls: Optional[str] = None
    is_async: bool = False

    score: float = 0.0
    depth: Optional[int] = None  # call depth from the nearest entry point
    fan_in: int = 0
    role: str = KEEP
    note: Optional[str] = None
    phases: List[Phase] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        return '{}::{}'.format(self.module, self.qualname)

    @property
    def is_public(self) -> bool:
        return not self.name.startswith('_')

    @property
    def is_method(self) -> bool:
        return self.cls is not None

    @property
    def decorator_tails(self) -> List[str]:
        """The last segment of each decorator name.

        `@overload`, `@typing.overload` and `@t.overload` are one decorator,
        and every caller cares which decorator it is rather than how the file
        chose to import it.
        """
        return [d.split('.')[-1] for d in self.decorators]


@dataclass
class Class:
    """A class definition, kept for the data-shapes section.

    `kind` separates the record types a reader needs to know the shape of
    ('dataclass', 'namedtuple', 'typeddict', 'enum') from ordinary classes,
    where the methods matter more than the fields.
    """

    name: str
    module: str
    path: str
    lineno: int
    bases: List[str]
    doc: Optional[str]
    fields: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    methods: List[str] = field(default_factory=list)
    kind: str = 'class'


@dataclass
class Constant:
    """A module-level assignment worth naming in the data section."""

    name: str
    module: str
    lineno: int
    shape: Optional[str]  # compressed annotation, or an inferred literal shape


@dataclass
class Module:
    """One parsed source file.

    `doc` and `header_comment` are the only two places a purpose line may come
    from inside a file. The extractor records both and leaves the choice to the
    renderer, which also has the package README available as a third source.
    Nothing infers a purpose from the code body — that inference is the thing
    the map is supposed to save the reader from having to trust.
    """

    name: str
    path: str
    doc: Optional[str]
    header_comment: Optional[str]
    is_package: bool = False
    imports: Dict[str, str] = field(default_factory=dict)
    funcs: List[Func] = field(default_factory=list)
    classes: List[Class] = field(default_factory=list)
    constants: List[Constant] = field(default_factory=list)
    main_calls: List[Call] = field(default_factory=list)
    parse_error: Optional[str] = None


@dataclass
class Project:
    """Every module recce parsed, plus the indexes the resolver needs."""

    modules: Dict[str, Module] = field(default_factory=dict)
    root: Optional[str] = None
    readme: Optional[str] = None
    # `module:function` targets from `[project.scripts]`, the one place a
    # package states its entry points rather than leaving them to be inferred.
    declared_entries: List[str] = field(default_factory=list)

    # Built on first use and kept. The pipeline fills `modules` during
    # discovery and never adds to it afterwards, so the index is stable for
    # every pass that reads it — and the passes read it constantly. Mapping the
    # standard library rebuilt a 55,710-entry dict 166 times, which is about
    # nine million insertions to answer questions the first one had answered.
    #
    # `len(self.modules)` is the guard rather than a full signature: it is O(1),
    # and discovery only ever adds. That buys a narrow contract — `modules` is
    # append-only once the index has been built. Replacing a module under a
    # name it already has, or deleting one and adding another, keeps the length
    # and so hands back a stale index; nothing in recce does either, and a
    # caller that starts to needs a real signature here rather than a count.
    # Functions are attached to a module when it is extracted and not after,
    # and the scoring passes mutate `Func` objects in place, which changes what
    # the index points at but never its keys.
    _index: Optional[Mapping[str, Func]] = field(
        default=None, repr=False, compare=False
    )
    _indexed_module_count: int = field(default=-1, repr=False, compare=False)

    def funcs(self) -> List[Func]:
        return list(self.by_id().values())

    def by_id(self) -> Mapping[str, Func]:
        """Every function in the project, keyed by `node_id`.

        Read-only, and the same object every time rather than a copy. Every
        caller holds the one mapping — `_Resolver` keeps it for the length of a
        resolve — so a caller who wrote to it would be writing into the cache
        every later pass reads. The proxy costs nothing and makes that a
        `TypeError` instead of a silent corruption; a caller who wants a
        mutable one says `dict(...)`.
        """
        if self._index is None or self._indexed_module_count != len(self.modules):
            self._index = MappingProxyType(
                {f.node_id: f for m in self.modules.values() for f in m.funcs}
            )
            self._indexed_module_count = len(self.modules)
        return self._index
