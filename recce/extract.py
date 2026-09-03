"""Parse Python sources into the records in `model.py`, using `ast` only.

Everything here is textual and static. Nothing is imported, nothing is
executed, and no type inference is attempted beyond reading annotations that
are already written down. That is the deal recce makes: it will miss the edges
that only exist at runtime, and in exchange it can be pointed at code nobody is
allowed to run.

Two extraction choices are worth knowing before you change anything:

- **Nested functions are folded into their parent.** A closure defined and used
  inside one function is part of that function's shape, not a place a reader
  navigates to, so its calls and its branches count toward the enclosing
  function and it gets no row of its own. The visitor therefore never descends
  into a function body looking for more definitions.
- **A call is recorded by the root of its dotted chain**, not by its final
  attribute. `pydub.AudioSegment.export()` is recorded with root `pydub`,
  because the import table is keyed on that root and it is the only thing that
  can tell us whether the call leaves the project. Chains that do not bottom
  out in a plain name — `results[0].save()`, `factory().run()` — get a null
  root and will be dropped by the resolver rather than guessed at.
"""

from __future__ import annotations

import ast
import builtins
import os
import re
import tokenize
import tomllib
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from .model import Call, Class, Constant, Func, Module, Project

# Directories that are never someone's source tree, skipped on the walk. Test
# directories are deliberately absent: a test suite is often the best available
# description of what the code is for, and the caller can exclude it if not.
# How far up to look for a `pyproject.toml` before giving up. Four covers the
# src layout (`repo/src/pkg/sub` -> `repo`) with a level to spare.
_PYPROJECT_SEARCH_LEVELS = 4

SKIP_DIRS = frozenset(
    {
        '.git',
        '.hg',
        '.svn',
        '.venv',
        'venv',
        'env',
        '__pycache__',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        '.tox',
        'node_modules',
        'build',
        'dist',
        'site-packages',
    }
)

# Nodes that mean the reader has to hold a second possibility in their head.
# The count is a complexity proxy, and it is the strongest single signal we
# have for which function holds the interesting logic.
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.ExceptHandler,
    ast.IfExp,
    ast.comprehension,
)

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Loops are counted apart from branches because a note that claims one can be
# checked against this. A comprehension counts: it is a loop the reader sees.
_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.comprehension)

# Typing containers whose bracket form says the same thing in fewer characters.
_CONTAINER_SHORTHAND = {
    'List': '[{}]',
    'list': '[{}]',
    'Set': '{{{}}}',
    'set': '{{{}}}',
}


# reStructuredText underlines the title of a section with a run of punctuation.
# Any of these, three or more, on the line under a heading.
_REST_UNDERLINE = frozenset('=-`:\'"~^_*+#<>')


def _strip_rest_title(text: str) -> str:
    """Drop leading reST section titles so the summary is the prose below them.

    The `name\n~~~~` convention opens the module docstring of a great deal of
    older Python. `requests` uses it in every module, and taking the first
    paragraph gave seven of its eight blocks the purpose line
    `requests.cookies ~~~~~~~~~~~~~~~~` — punctuation presented to the reader
    as a statement of what the module is for.

    A title is a line followed by a line of one repeated punctuation mark, at
    least three long. Both lines go, and repeatedly, since an overline puts one
    above the title as well.
    """
    lines = text.strip().splitlines()
    changed = True
    while changed and lines:
        changed = False
        while lines and not lines[0].strip():
            lines.pop(0)
        # An overline sits above the title, so a rule in first position is not
        # a summary either — drop it and let the title+underline case follow.
        if lines and _is_rule(lines[0]):
            lines.pop(0)
            changed = True
            continue
        if len(lines) >= 2 and _is_rule(lines[1]):
            del lines[:2]
            changed = True
    return '\n'.join(lines).strip()


def _is_rule(line: str) -> bool:
    """Whether a line is a run of one punctuation mark, reST's section rule."""
    stripped = line.strip()
    return (
        len(stripped) >= 3
        and len(set(stripped)) == 1
        and stripped[0] in _REST_UNDERLINE
    )


def first_sentence(text: Optional[str]) -> Optional[str]:
    """Return the first sentence of a docstring, collapsed onto one line.

    Docstring summaries are usually already one sentence, but a one-paragraph
    summary is common enough that taking the whole first paragraph would blow
    the purpose line's budget.
    """
    if not text:
        return None
    para = _strip_rest_title(text).split('\n\n')[0]
    para = ' '.join(para.split())
    if not para:
        return None
    match = re.search(r'(?<![A-Z])[.!?](?:\s|$)', para)
    if match:
        para = para[: match.start() + 1]
    return para.rstrip('.') or None


def compress_type(node: Optional[ast.AST]) -> Optional[str]:
    """Render an annotation in the shortest form that keeps its meaning.

    The map's job is to show what flows along an edge, and the written-out
    generic forms defeat that — `Optional[List[Tuple[str, float]]]` is nine
    tokens of type-system grammar wrapped around the two that matter. The
    rewrites are purely notational:

    - `Optional[X]` becomes `X?`
    - `List[X]` becomes `[X]`, `Dict[K, V]` becomes `{K: V}`
    - `Tuple[A, B]` becomes `(A, B)`
    - `Union[A, B]` and `A | B` both become `A|B`
    - a `typing.` or other module prefix is dropped

    Anything not recognised is unparsed as written, so an unusual annotation
    degrades to being verbose rather than to being wrong.
    """
    match node:
        case None:
            return None
        case ast.Constant(value=None):
            return 'None'
        case ast.Constant(value=builtins.Ellipsis):
            return '...'
        case ast.Constant(value=str() as text):
            # A string annotation is a forward reference; the text is the type.
            return text
        case ast.Constant():
            return _unparse(node)
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attr):
            # `typing.Optional` and `t.Optional` both mean Optional to a reader.
            return attr
        case ast.BinOp(op=ast.BitOr(), left=left, right=right):
            return '{}|{}'.format(compress_type(left), compress_type(right))
        case ast.Subscript():
            return _compress_subscript(node)
        case ast.Tuple(elts=elts):
            return ', '.join(compress_type(e) or '?' for e in elts)
        case _:
            return _unparse(node)


def _compress_subscript(node: ast.Subscript) -> str:
    base = compress_type(node.value) or '?'
    inner_node = node.slice
    if inner_node.__class__.__name__ == 'Index':  # 3.8 shape, harmless on 3.9+
        inner_node = inner_node.value  # type: ignore[attr-defined]
    parts = (
        [compress_type(e) or '?' for e in inner_node.elts]
        if isinstance(inner_node, ast.Tuple)
        else [compress_type(inner_node) or '?']
    )
    inner = ', '.join(parts)
    if base == 'Optional':
        return '{}?'.format(inner)
    if base == 'Union':
        return '|'.join(parts)
    if base in _CONTAINER_SHORTHAND:
        return _CONTAINER_SHORTHAND[base].format(inner)
    if base in ('Dict', 'dict') and len(parts) == 2:
        return '{{{}: {}}}'.format(parts[0], parts[1])
    if base in ('Tuple', 'tuple'):
        return '({})'.format(inner)
    return '{}[{}]'.format(base, inner)


def _unparse(node: ast.AST) -> str:
    """Best-effort source text for a node, collapsed onto one line."""
    try:
        text = ast.unparse(node)  # 3.9+
    except Exception:
        return '?'
    return ' '.join(text.split())


def _dotted_of(node: ast.AST) -> Tuple[Optional[str], str, str]:
    """Split a callee expression into (root, final attribute, dotted text).

    Returns a null root when the chain does not bottom out in a plain name,
    which is the signal the resolver uses to drop a call rather than guess.
    """
    parts: List[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts[0], parts[-1], '.'.join(parts)
    parts.reverse()
    dotted = '.'.join(parts) if parts else _unparse(node)
    return None, (parts[-1] if parts else dotted), dotted


class _BodyMetrics(NamedTuple):
    """What one walk of a function body measures."""

    n_stmts: int
    n_branches: int
    n_loops: int
    n_ternaries: int
    calls: List[Call]
    returns_keys: List[str]
    n_strings: int


def _body_metrics(body: Iterable[ast.stmt]) -> _BodyMetrics:
    """Measure a function body in one walk.

    The fields are named in `_BodyMetrics`. The enumeration that used to stand
    here fell two behind what the function returns, which is what a positional
    tuple lets happen.

    The string count is the least obvious and the most useful. A function
    thick with string literals is almost always building output, and output
    builders branch as much as real logic does — a report writer looping over
    its sections looks exactly like an aggregator looping over records if all
    you count is `For` nodes. The count is what lets the scorer tell them
    apart.

    The walk deliberately covers nested definitions too, so a function that
    hides its work in a closure is not scored as though it were empty.
    """
    n_stmts = 0
    n_branches = 0
    n_loops = 0
    n_ternaries = 0
    n_strings = 0
    returns_keys: List[str] = []
    calls: List[Call] = []
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.stmt):
                n_stmts += 1
            if isinstance(node, _BRANCH_NODES):
                n_branches += 1
            if isinstance(node, _LOOP_NODES):
                n_loops += 1
            if isinstance(node, ast.IfExp):
                n_ternaries += 1
            if _is_prose_string(node):
                n_strings += 1
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        if key.value not in returns_keys:
                            returns_keys.append(key.value)
            if isinstance(node, ast.Call):
                root, attr, dotted = _dotted_of(node.func)
                calls.append(
                    Call(
                        dotted=dotted,
                        root=root,
                        attr=attr,
                        lineno=getattr(node, 'lineno', 0),
                    )
                )
    # `ast.walk` is breadth-first, so the calls come out in tree order rather
    # than source order. The map reads top to bottom the way the function does,
    # which means the line number is what orders the children.
    calls.sort(key=lambda c: c.lineno)
    return _BodyMetrics(
        n_stmts, n_branches, n_loops, n_ternaries, calls, returns_keys, n_strings
    )


def _is_prose_string(node: ast.AST) -> bool:
    """Whether a string literal is text for a human rather than a key.

    This distinction is what makes the string count usable as a signal. A
    function full of dict keys and regex group names — `'status'`, `'method'` —
    is not building output, but counting every `str` constant made it look
    identical to one that is, and the two ended up scoring the same.

    An f-string is always prose: nothing interpolates into a key. Otherwise the
    tell is a space, because identifiers do not have them and sentences do.
    """
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ' ' in node.value.strip()
    return False


def _decorator_names(node: ast.AST) -> List[str]:
    names = []
    for dec in getattr(node, 'decorator_list', []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        _, attr, dotted = _dotted_of(target)
        names.append(dotted or attr)
    return names


def _arg_names(node: ast.AST) -> List[str]:
    """Positional and keyword-only parameter names, minus `self` and `cls`."""
    spec = node.args  # type: ignore[attr-defined]
    names = [a.arg for a in list(getattr(spec, 'posonlyargs', [])) + list(spec.args)]
    names += [a.arg for a in spec.kwonlyargs]
    return [n for n in names if n not in ('self', 'cls')]


# `@overload` declares a signature; it does not define a function. requests
# writes three `HTTPBasicAuth.__init__`s — two overloads and the real one —
# and emitting a row for each says the class has three constructors. Stubs are
# extracted and then lose to the implementation below, rather than being
# skipped outright: a name that has nothing but stubs is still a name, and
# skipping it emptied whole modules out of the map.
_OVERLOAD_DECORATORS = frozenset({'overload'})

# The other half of a `@property` pair. A setter and a deleter share the
# getter's name, and the getter is the definition that says what the attribute
# is, so it wins however short its body.
_MUTATOR_DECORATORS = frozenset({'setter', 'deleter'})


class _DefinitionRank(NamedTuple):
    """How much a definition deserves the single row its name gets.

    Compared as a tuple, so the fields are in priority order and higher wins.
    """

    implementation: int  # 0 for an `@overload` stub, 1 for a real body
    getter: int  # 0 for a `@property` setter or deleter, 1 for anything else
    n_stmts: int


def _definition_rank(func: Func) -> _DefinitionRank:
    """Rank one definition of a name against another of the same name.

    An implementation beats its `@overload` stubs, and a `@property` getter
    beats its setter at any size. Only when neither applies does body size
    decide.
    """
    tails = func.decorator_tails
    stub = any(t in _OVERLOAD_DECORATORS for t in tails)
    mutator = any(t in _MUTATOR_DECORATORS for t in tails)
    return _DefinitionRank(0 if stub else 1, 0 if mutator else 1, func.n_stmts)


def _merge_calls(winner: Func, loser: Func) -> None:
    """Keep the discarded definition's call sites on the one that survives.

    A `@property` setter is dropped in favour of its getter, but the calls in
    its body are the attribute's calls and they are the only record that those
    edges exist. Losing them drops an edge the reader needed and, worse, can
    leave the callee with no callers at all — which `rank` reads as a way into
    the codebase. Calls are still unresolved here, so the key is the source
    shape; `lineno` is left out so a call both bodies make appears once.
    """
    seen = {(c.dotted, c.root, c.attr) for c in winner.calls}
    for call in loser.calls:
        key = (call.dotted, call.root, call.attr)
        if key not in seen:
            seen.add(key)
            winner.calls.append(call)


def _dedupe_definitions(funcs: List[Func]) -> List[Func]:
    """Keep one entry per qualified name, the one the reader should see.

    A name defined twice in a module is one function at runtime, and Python
    binds the last definition. recce ranks instead: an implementation beats its
    `@overload` stubs and a getter beats its setter, because those are the pairs
    where the last definition is not the informative one. Ties go to the later
    definition, which is what Python would leave bound.

    Emitting both put two identical rows in the map and, worse, gave two `Func`
    objects the same `node_id`, so the index silently kept whichever it saw last
    while the renderer walked a list that still had both.

    Only definitions recce extracts reach here. A def inside an `if` or a `try`
    body is not seen at all, so a platform branch and a `try`/`except
    ImportError` pair are a gap rather than a case this handles.
    """
    ordered: Dict[str, Func] = {}
    for func in funcs:
        previous = ordered.get(func.qualname)
        if previous is None:
            ordered[func.qualname] = func
            continue
        # The getter wins a `@property` pair outright — both share a name, and
        # the getter is the one that says what the attribute is, however short
        # its body. Otherwise the definition with the most in it wins. Ties go
        # to the later definition, matching what Python itself would leave
        # bound. The loser's calls move across rather than dying with it.
        if _definition_rank(func) >= _definition_rank(previous):
            winner, loser = func, previous
        else:
            winner, loser = previous, func
        _merge_calls(winner, loser)
        ordered[func.qualname] = winner
    return list(ordered.values())


def _make_func(node: ast.AST, module: str, path: str, cls: Optional[str]) -> Func:
    (
        n_stmts,
        n_branches,
        n_loops,
        n_ternaries,
        calls,
        returns_keys,
        n_strings,
    ) = _body_metrics(node.body)  # type: ignore[attr-defined]
    doc = ast.get_docstring(node)  # type: ignore[arg-type]
    if doc:
        n_stmts -= 1  # the docstring is an Expr statement, not work
        n_strings -= 1
    name = node.name  # type: ignore[attr-defined]
    end = getattr(node, 'end_lineno', node.lineno) or node.lineno  # type: ignore[attr-defined]
    return Func(
        name=name,
        qualname='{}.{}'.format(cls, name) if cls else name,
        module=module,
        path=path,
        lineno=node.lineno,  # type: ignore[attr-defined]
        end_lineno=end,
        args=_arg_names(node),
        returns=compress_type(node.returns),  # type: ignore[attr-defined]
        doc=first_sentence(doc),
        decorators=_decorator_names(node),
        calls=calls,
        n_stmts=max(n_stmts, 0),
        n_branches=n_branches,
        n_loops=n_loops,
        n_ternaries=n_ternaries,
        n_strings=max(n_strings, 0),
        returns_keys=returns_keys,
        loc=max(end - node.lineno + 1, 1),  # type: ignore[attr-defined]
        cls=cls,
        is_async=isinstance(node, ast.AsyncFunctionDef),
    )


def _literal_shape(node: Optional[ast.AST]) -> Optional[str]:
    """Name the shape of a module-level value, for the data-shapes section.

    Only the outline is wanted. A list of tuples is worth saying; which tuples
    is not, and the reader can open the file for that.
    """
    match node:
        case None:
            return None
        case ast.Call(func=func):
            _, attr, dotted = _dotted_of(func)
            if dotted.startswith('re.compile') or attr == 'compile':
                return 'regex'
            return '{}(...)'.format(attr)
        case ast.List(elts=elts):
            return _sequence_shape(elts, '[', ']')
        case ast.Tuple(elts=elts):
            return _sequence_shape(elts, '(', ')')
        case ast.Set(elts=elts):
            return _sequence_shape(elts, '{', '}')
        case ast.Dict(keys=[], values=[]):
            return '{}'
        case ast.Dict(keys=[key, *_], values=[value, *_]):
            return '{{{}: {}}}'.format(
                _literal_shape(key) or '?', _literal_shape(value) or '?'
            )
        case ast.Constant(value=value):
            return type(value).__name__
        case ast.JoinedStr():
            return 'str'
        case _:
            return None


def _sequence_shape(elts: List[ast.expr], opener: str, closer: str) -> str:
    """A container's outline: its brackets, and the shape of its first element."""
    if not elts:
        return opener + closer
    return '{}{}{}'.format(opener, _literal_shape(elts[0]) or '?', closer)


def _header_comment(source: str) -> Optional[str]:
    """The top-of-file comment block, if the file leads with one.

    This is the third of the three purpose sources the map is allowed to use,
    and it only counts when it is genuinely a header: a shebang, a coding
    line, or a PEP 723 metadata block is machinery rather than description, so
    all three are skipped and the block after them is what gets read.
    """
    lines: List[str] = []
    in_pep723 = False
    for raw in source.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        if not line.startswith('#'):
            break
        body = line.lstrip('#').strip()
        if line.startswith('#!') or body.startswith('-*-') or 'coding:' in body:
            continue
        if body.startswith('///'):
            in_pep723 = not in_pep723
            continue
        if in_pep723:
            continue
        lines.append(body)
    text = ' '.join(line for line in lines if line)
    return first_sentence(text) if text else None


def _resolve_relative(
    module_name: str, level: int, target: Optional[str], is_package: bool
) -> str:
    """Turn a `from . import x` target into an absolute dotted module name.

    One dot means "the package this module lives in", and which name that is
    depends on what the importing module is. For `pkg.cli` the package is
    everything but the last component. For `pkg/__init__.py`, which we address
    as plain `pkg`, the package is the whole name — there is no component to
    strip, because stripping already happened when the file was named.

    Getting this backwards is quiet rather than loud: `from .parser import
    parse_line` inside `pkg.cli` resolves to `pkg.cli.parser`, which matches no
    module, so the call is classified external and the cross-file edge simply
    never appears in the map.
    """
    parts = module_name.split('.')
    keep = len(parts) - level + (1 if is_package else 0)
    base = parts[: max(keep, 0)]
    if target:
        base = base + target.split('.')
    return '.'.join(p for p in base if p)


def _import_statements(body: Iterable[ast.stmt]) -> Iterable[ast.stmt]:
    """Yield import statements, descending into conditional blocks.

    Two idioms put imports somewhere other than the top level, and both are
    common enough that skipping them loses real edges:

    - `if TYPE_CHECKING:` — the annotation-only imports, which are exactly the
      ones naming where a re-exported symbol actually lives. httpx binds its
      console-script entry point this way, and nothing else in the file says
      that `main` comes from `._main`.
    - `try: import fast except ImportError: import slow` — the optional
      dependency pattern.

    Both branches of either are walked. A name bound in only one of them is
    still a name this file can call, and recording both is what keeps the
    resolver from silently classifying it as external.
    """
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, ast.If):
            yield from _import_statements(node.body)
            yield from _import_statements(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _import_statements(node.body)
            yield from _import_statements(node.orelse)
            yield from _import_statements(node.finalbody)
            for handler in node.handlers:
                yield from _import_statements(handler.body)


def _imports(tree: ast.Module, module_name: str, is_package: bool) -> Dict[str, str]:
    """Map each name a file binds by import to the dotted module it came from."""
    table: Dict[str, str] = {}
    for node in _import_statements(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    # `import a.b.c` binds only `a`.
                    table[alias.name.split('.')[0]] = alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(module_name, node.level, node.module, is_package)
                if node.level
                else (node.module or '')
            )
            for alias in node.names:
                if alias.name == '*':
                    continue  # a star import binds nothing we can name
                bound = alias.asname or alias.name
                table[bound] = '{}.{}'.format(base, alias.name) if base else alias.name
    return table


def _class_fields(node: ast.ClassDef) -> List[Tuple[str, Optional[str]]]:
    """Annotated class attributes, plus whatever `__init__` assigns to self."""
    fields: List[Tuple[str, Optional[str]]] = []
    seen = set()
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append((stmt.target.id, compress_type(stmt.annotation)))
            seen.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id not in seen:
                    fields.append((target.id, _literal_shape(stmt.value)))
                    seen.add(target.id)
    for stmt in node.body:
        if isinstance(stmt, _FUNC_NODES) and stmt.name == '__init__':
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        is_self_attr = (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == 'self'
                        )
                        if is_self_attr and target.attr not in seen:
                            fields.append((target.attr, _literal_shape(sub.value)))
                            seen.add(target.attr)
    return fields


def _class_kind(node: ast.ClassDef, bases: List[str], decorators: List[str]) -> str:
    """Classify a class by whether its fields or its methods are the story."""
    if any(d.endswith('dataclass') for d in decorators):
        return 'dataclass'
    for base in bases:
        tail = base.split('.')[-1]
        if tail == 'NamedTuple':
            return 'namedtuple'
        if tail == 'TypedDict':
            return 'typeddict'
        if tail in ('Enum', 'IntEnum', 'StrEnum', 'Flag', 'IntFlag'):
            return 'enum'
        if tail in ('Protocol', 'ABC'):
            return 'protocol'
    return 'class'


def extract_module(
    path: Path, module_name: str, source: Optional[str] = None
) -> Module:
    """Parse one file into a `Module`.

    A file that will not parse comes back as a `Module` carrying `parse_error`
    rather than raising. One unreadable file in a package should cost you that
    file's rows, not the whole map.
    """
    if source is None:
        source = _read_source(path)
    module = Module(
        name=module_name,
        path=str(path),
        doc=None,
        header_comment=None,
        is_package=path.name == '__init__.py',
    )
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        module.parse_error = '{}: line {}'.format(exc.msg, exc.lineno)
        return module

    module.doc = first_sentence(ast.get_docstring(tree))
    module.header_comment = None if module.doc else _header_comment(source)
    module.imports = _imports(tree, module_name, module.is_package)

    for node in tree.body:
        if isinstance(node, _FUNC_NODES):
            module.funcs.append(_make_func(node, module_name, str(path), None))
        elif isinstance(node, ast.ClassDef):
            bases = [_dotted_of(b)[2] for b in node.bases]
            decorators = _decorator_names(node)
            methods = []
            for stmt in node.body:
                if isinstance(stmt, _FUNC_NODES):
                    func = _make_func(stmt, module_name, str(path), node.name)
                    module.funcs.append(func)
                    methods.append(func.qualname)
            module.classes.append(
                Class(
                    name=node.name,
                    module=module_name,
                    path=str(path),
                    lineno=node.lineno,
                    bases=bases,
                    doc=first_sentence(ast.get_docstring(node)),
                    fields=_class_fields(node),
                    methods=methods,
                    kind=_class_kind(node, bases, decorators),
                )
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module.constants.append(
                Constant(
                    name=node.target.id,
                    module=module_name,
                    lineno=node.lineno,
                    shape=compress_type(node.annotation),
                )
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                # Only shouted names. A lowercase module-level assignment is
                # usually a singleton or a bit of setup, not a data shape the
                # reader needs named up front.
                if isinstance(target, ast.Name) and target.id.isupper():
                    module.constants.append(
                        Constant(
                            name=target.id,
                            module=module_name,
                            lineno=node.lineno,
                            shape=_literal_shape(node.value),
                        )
                    )
        elif isinstance(node, ast.If) and _is_main_guard(node):
            module.main_calls = _body_metrics(node.body).calls

    module.funcs = _dedupe_definitions(module.funcs)
    # `methods` is collected while walking the class body, before the dedupe
    # above has run, so a `@property` triple leaves three `C.enc` entries
    # against the one `Func` that survived. Rebuild it from what actually
    # exists rather than deduping the names on their own — one source of truth,
    # whatever the dedupe rule becomes next.
    by_class: Dict[str, List[str]] = {}
    for func in module.funcs:
        if func.cls is not None:
            by_class.setdefault(func.cls, []).append(func.qualname)
    for cls in module.classes:
        cls.methods = by_class.get(cls.name, [])
    return module


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name):
        return False
    if test.left.id != '__name__':
        return False
    return any(
        isinstance(c, ast.Constant) and c.value == '__main__' for c in test.comparators
    )


def _read_source(path: Path) -> str:
    """Read a source file, honouring any PEP 263 coding declaration."""
    try:
        with tokenize.open(str(path)) as handle:
            return handle.read()
    except (SyntaxError, UnicodeDecodeError, LookupError):
        return path.read_text(encoding='utf-8', errors='replace')


def _module_name_for(path: Path, root: Path) -> str:
    """Dotted name for a file, relative to the tree root recce was given."""
    rel = path.relative_to(root)
    parts = list(rel.parts)
    parts[-1] = parts[-1][: -len('.py')]
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts) if parts else root.name


def _package_root(directory: Path) -> Path:
    """Walk up out of a package so module names keep their package prefix.

    Pointed at `pkg/`, we want `pkg.cli` rather than `cli`, because the prefix
    is what makes a cross-module reference in the map unambiguous. Pointed at a
    plain directory of scripts there is no prefix to keep, so the directory
    itself is the root.
    """
    root = directory
    while (root / '__init__.py').exists() and root.parent != root:
        root = root.parent
    return root


def _find_readme(directory: Path) -> Optional[str]:
    """First prose paragraph of a README sitting in this exact directory.

    The search deliberately does not walk up, and a single-file target does not
    get one at all. A README one level up describes the project a file happens
    to sit in, which is not the same claim as describing that file — pointed at
    a fixture inside a test suite, walking up produced a purpose line about the
    test harness and attached it to the code under test.

    A wrong purpose line is worse than no purpose line. It is the first thing
    read and the reader has no way to tell it was guessed, so when the three
    permitted sources are silent the right output is silence.
    """
    for name in ('README.md', 'README.rst', 'README.txt', 'README'):
        candidate = directory / name
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding='utf-8', errors='replace')
        for para in text.split('\n\n'):
            stripped = para.strip()
            # Skip the title line and any badge soup above the prose.
            if stripped and not stripped.startswith(('#', '=', '[!', '<')):
                return first_sentence(stripped)
    return None


def declared_entry_points(root: Path) -> List[str]:
    """Console scripts a `pyproject.toml` above this tree declares.

    `[project.scripts]` is the only place a package states, rather than
    implies, where it is meant to be entered. Everything else recce has is
    inference — a `__main__` guard, a decorator it recognises, a function
    called `main`, or the shape of the call graph — and inference is what
    produces a map that opens on a helper because nothing happened to call it.

    Returns dotted `module:function` targets as written. Reading this needed
    `tomllib`, which is the concrete thing the 3.11 floor bought: a TOML parser
    that is not a dependency, on a tool whose whole premise is having none.

    A malformed or unreadable file yields nothing. This is a bonus signal, and
    failing a map over a `pyproject.toml` recce was not asked about would be a
    poor trade.
    """
    for directory in _project_dirs(root):
        candidate = directory / 'pyproject.toml'
        if not candidate.is_file():
            continue
        try:
            with candidate.open('rb') as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return []
        scripts = data.get('project', {}).get('scripts', {})
        if isinstance(scripts, dict):
            return [str(v) for v in scripts.values()]
    return []


def _project_dirs(root: Path) -> Iterable[Path]:
    """Directories to look in for a `pyproject.toml`, nearest first.

    Walking up is right here where it was wrong for READMEs, and the
    difference is what the file claims. A README one level up describes the
    project a file happens to sit in; a `pyproject.toml` above a package
    describes *that package*, which is what every build tool already assumes.
    The src layout makes the walk necessary rather than optional — flask's
    manifest is two levels above `src/flask`.

    The walk stops at the repository root, since a `pyproject.toml` outside it
    belongs to something else entirely, and gives up after a few levels when
    there is no `.git` to find.
    """
    directory = root if root.is_dir() else root.parent
    for _ in range(_PYPROJECT_SEARCH_LEVELS):
        yield directory
        if (directory / '.git').exists() or directory.parent == directory:
            return
        directory = directory.parent


def discover(target: Path) -> Project:
    """Parse a file, a package, or a directory of scripts into a `Project`."""
    target = target.resolve()
    project = Project()
    if target.is_file():
        root = _package_root(target.parent)
        project.root = str(target.parent)
        module = extract_module(target, _module_name_for(target, root))
        project.modules[module.name] = module
        project.declared_entries = declared_entry_points(target.parent)
        return project

    root = _package_root(target)
    project.root = str(target)
    project.readme = _find_readme(target)
    project.declared_entries = declared_entry_points(target)
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')
        )
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            path = Path(dirpath) / filename
            module = extract_module(path, _module_name_for(path, root))
            project.modules[module.name] = module
    return project
