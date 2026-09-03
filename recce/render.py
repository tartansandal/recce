"""Turn a `Plan` into the markdown a code map is supposed to look like.

Nothing here decides anything. Every judgement — what to show, what to star,
where to split — was made in `rank.py`, and this module's only job is to put
the result on the page in the agreed shape.

The shape matters more than it looks. Prose lives outside the fences and only
the trees go inside them, which is what makes a saved map navigable by outline
view and heading jumps in an editor rather than being one opaque code block.
The markers are a fixed vocabulary explained by a legend at the foot of the
document, so a reader meeting one of these for the first time is never guessing.
"""

from __future__ import annotations

import os
from typing import List, Optional

from .model import Func, Project
from .rank import Block, Node, Plan

LEGEND = '★ read first · ~ skim · `[brackets]` = external'

# Where the bracket annotations line up, and the hard stop past which a row is
# left ragged rather than pushed off the side of a terminal.
_BRACKET_COLUMN = 44
_MAX_WIDTH = 78

# How many data bullets a reader will take in before the section stops being a
# summary of the shapes and becomes a listing of the names.
_DATA_BULLETS = 10

# Shapes that name a value's type rather than its structure. `NOT_APPLICABLE —
# str` costs a line to say the constant is a string, which its use site would
# have told the reader anyway. Seen on real code: two of `weep`'s ten data
# bullets and five of `xray-analysis`'s were these, crowding out record types
# that actually needed describing.
_SCALAR_SHAPES = frozenset({'str', 'int', 'float', 'bool', 'bytes', 'complex'})


def _is_scalar(shape: str) -> bool:
    return shape in _SCALAR_SHAPES


def render(project: Project, plan: Plan, base: Optional[str] = None) -> str:
    """Render the whole document."""
    lines: List[str] = []
    title, purpose = _heading(project, plan)
    lines.append('# {}{}'.format(title, ' — {}'.format(purpose) if purpose else ''))
    lines.append('')

    show_base = base is not None and len(project.modules) > 1
    if show_base:
        lines.append('**Base:** `{}`'.format(_as_dir(base)))
        lines.append('')

    if plan.strategy != 'single':
        noun = 'module' if plan.strategy == 'module' else 'entry-point flow'
        note = 'Splitting by {} for fit. {} blocks'.format(noun, len(plan.blocks))
        if plan.omitted_modules:
            note += ', {} further modules not shown'.format(plan.omitted_modules)
        lines.append(note + '.')
        lines.append('')

    for index, block in enumerate(plan.blocks, start=1):
        if plan.strategy != 'single':
            heading = '## [{}] {}'.format(index, block.title)
            if block.purpose:
                heading += ' — {}'.format(block.purpose)
            lines.append(heading)
            lines.append('')
        lines.append('```')
        lines.extend(_render_block(block))
        lines.append('```')
        lines.append('')

    data = _data_section(project)
    if data:
        lines.append('## Data')
        lines.append('')
        lines.extend(data)
        lines.append('')

    if plan.strategy != 'single' and plan.spine:
        lines.append('## Spine to read first')
        lines.append('')
        for position, func in enumerate(plan.spine[:5], start=1):
            lines.append(
                '{}. `{}:{} :: {}`'.format(
                    position, _relative(func.path, base), func.lineno, func.qualname
                )
            )
        lines.append('')

    lines.append(LEGEND)
    return '\n'.join(lines).rstrip() + '\n'


def _heading(project: Project, plan: Plan) -> tuple:
    """The document title and its purpose line, if one is available."""
    if len(project.modules) == 1:
        module = next(iter(project.modules.values()))
        title = os.path.basename(module.path)
        purpose = module.doc or project.readme or module.header_comment
        return title, purpose
    root = project.root or ''
    return _as_dir(os.path.basename(root.rstrip('/')) or root), project.readme


def _as_dir(path: str) -> str:
    return path if path.endswith('/') else path + '/'


def _relative(path: str, base: Optional[str]) -> str:
    if not base:
        return path
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _render_block(block: Block) -> List[str]:
    """One fenced tree: build every row, then align the bracket column."""
    rows: List[tuple] = []
    for root in block.roots:
        _walk(root, '', True, rows, is_root=True)
    if not rows:
        return ['(nothing to show)']

    widest = max((len(text) for text, bracket in rows if bracket), default=0)
    column = min(max(widest + 2, _BRACKET_COLUMN), _MAX_WIDTH - 12)

    lines: List[str] = []
    for text, bracket in rows:
        if not bracket:
            lines.append(text.rstrip())
            continue
        pad = max(column - len(text), 2)
        lines.append('{}{}[{}]'.format(text, ' ' * pad, bracket))
    return lines


def _walk(
    node: Node, prefix: str, is_last: bool, rows: List[tuple], is_root: bool = False
) -> None:
    """Depth-first row emission, carrying the box-drawing prefixes down."""
    if is_root:
        rows.append((_row_text(node), node.bracket))
        child_prefix = ' '
    else:
        connector = '└─ ' if is_last else '├─ '
        rows.append((prefix + connector + _row_text(node), node.bracket))
        child_prefix = prefix + ('    ' if is_last else '│   ')

    # The note hangs at the children's indent with no connector of its own.
    # Rows are `name(args)` and notes are lowercase prose, so the two do not
    # read as the same kind of thing even sitting at the same depth.
    if node.note:
        rows.append((child_prefix + node.note, None))

    for index, child in enumerate(node.children):
        _walk(child, child_prefix, index == len(node.children) - 1, rows)


def _row_text(node: Node) -> str:
    """The text of one row, before any bracket alignment."""
    if node.func is None:
        return node.label

    text = '{}({})'.format(node.func.qualname, _args_of(node.func))
    if node.ret and not node.repeat:
        text += '  ─→ {}'.format(node.ret)
    if node.marker:
        text += '  {}'.format(node.marker)
    if node.repeat:
        text += '  ↑'
    return text


def _args_of(func: Func) -> str:
    """The parameters worth showing: the map is about flow, not signatures.

    Two names fit and say what goes in. Three or more is a signature, which
    belongs in the source, so anything longer collapses to an ellipsis.
    """
    if not func.args:
        return ''
    if len(func.args) <= 2:
        return ', '.join(func.args)
    return '...'


def _data_section(project: Project) -> List[str]:
    """The key in-memory shapes, as a bullet list outside the fence.

    Three sources, in the order a reader needs them: record types, whose
    fields are declared; dict literals returned by functions, whose fields are
    not declared anywhere else and are the shape most Python actually passes
    around; then module constants.
    """
    bullets: List[str] = []

    for module in project.modules.values():
        for func in module.funcs:
            if len(func.returns_keys) >= 2:
                keys = ', '.join(func.returns_keys[:6])
                if len(func.returns_keys) > 6:
                    keys += ', …'
                bullets.append(
                    '- `{}()` — returns `{{{}}}`'.format(func.qualname, keys)
                )

    for module in project.modules.values():
        for cls in module.classes:
            if cls.kind in ('dataclass', 'namedtuple', 'typeddict') and cls.fields:
                fields = ', '.join(
                    '{}: {}'.format(name, shape) if shape else name
                    for name, shape in cls.fields[:4]
                )
                if len(cls.fields) > 4:
                    fields += ', …'
                bullets.append('- `{}` — {}'.format(cls.name, fields))
            elif cls.kind == 'enum':
                members = ', '.join(name for name, _ in cls.fields[:6])
                bullets.append('- `{}` — enum: {}'.format(cls.name, members))

    for module in project.modules.values():
        for constant in module.constants:
            if constant.shape and not _is_scalar(constant.shape):
                bullets.append('- `{}` — {}'.format(constant.name, constant.shape))

    # Scalars come back only if nothing better exists. A module whose only
    # module-level names are strings still deserves them listed; a module with
    # eight record types does not need two of its ten slots spent on `str`.
    if not bullets:
        for module in project.modules.values():
            for constant in module.constants:
                if constant.shape and _is_scalar(constant.shape):
                    bullets.append('- `{}` — {}'.format(constant.name, constant.shape))

    # Deduplicate names that several modules re-export, keeping first mention.
    seen = set()
    unique = []
    for bullet in bullets:
        key = bullet.split('`')[1] if '`' in bullet else bullet
        if key not in seen:
            seen.add(key)
            unique.append(bullet)
    return unique[:_DATA_BULLETS]
