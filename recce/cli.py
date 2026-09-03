"""Command line for recce.

Three outputs, all from the same passes:

- markdown, the default, which is the map itself
- `--json`, the full intermediate state, which is what a model stage consumes
  when one is added
- `--stats`, a one-line summary useful for checking a run did what you expected
  before reading the map
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .extract import discover
from .graph import resolve
from .rank import plan
from .render import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='recce',
        description='Sketch the structural skeleton of unfamiliar Python code.',
    )
    parser.add_argument('target', type=Path, help='file, package, or directory')
    parser.add_argument(
        '-o', '--out', type=Path, default=None, help='write to a file instead of stdout'
    )
    parser.add_argument(
        '--max-lines',
        type=int,
        default=40,
        help='line budget per fenced tree before the map splits (default: 40)',
    )
    parser.add_argument(
        '--base',
        type=Path,
        default=None,
        help='directory paths in the map are relative to (default: the target)',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='dump the intermediate state instead of the map',
    )
    parser.add_argument(
        '--stats', action='store_true', help='print a one-line summary to stderr'
    )
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='accepted for forward compatibility; the static pass is the only mode',
    )
    parser.add_argument('--version', action='version', version=__version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.target.exists():
        print(
            'recce: no such file or directory: {}'.format(args.target), file=sys.stderr
        )
        return 2

    project = discover(args.target)
    if not project.modules:
        print(
            'recce: no Python sources found under {}'.format(args.target),
            file=sys.stderr,
        )
        return 1

    graph = resolve(project)
    mapping = plan(project, graph, max_lines=args.max_lines)

    base = str(args.base) if args.base else project.root
    output = (
        json.dumps(_as_json(project, mapping), indent=2)
        if args.json
        else render(project, mapping, base=base)
    )

    if args.out:
        args.out.write_text(output if output.endswith('\n') else output + '\n')
    else:
        sys.stdout.write(output if output.endswith('\n') else output + '\n')

    broken = [m for m in project.modules.values() if m.parse_error]
    for module in broken:
        print(
            'recce: skipped {} ({})'.format(module.path, module.parse_error),
            file=sys.stderr,
        )
    if args.stats:
        print(
            'recce: {} modules, {} functions, {} blocks, strategy={}'.format(
                len(project.modules),
                len(project.funcs()),
                len(mapping.blocks),
                mapping.strategy,
            ),
            file=sys.stderr,
        )
    return 0


def _as_json(project, mapping) -> dict:
    """The intermediate state, flattened enough to be read by something else.

    The per-function records carry the scoring fields and an empty `note`,
    which is the slot a paraphrase goes in. That is the whole contract a model
    stage needs: read the functions, fill in the notes, hand it back.
    """
    return {
        'root': project.root,
        'readme': project.readme,
        'strategy': mapping.strategy,
        'spine': [f.node_id for f in mapping.spine],
        'entries': [f.node_id for f in mapping.entries],
        'modules': {
            name: {
                'path': module.path,
                'doc': module.doc,
                'header_comment': module.header_comment,
                'parse_error': module.parse_error,
                'constants': [dataclasses.asdict(c) for c in module.constants],
                'classes': [dataclasses.asdict(c) for c in module.classes],
                'funcs': [
                    {
                        'node_id': f.node_id,
                        'qualname': f.qualname,
                        'lineno': f.lineno,
                        'end_lineno': f.end_lineno,
                        'args': f.args,
                        'returns': f.returns,
                        'doc': f.doc,
                        'n_stmts': f.n_stmts,
                        'n_branches': f.n_branches,
                        'loc': f.loc,
                        'fan_in': f.fan_in,
                        'depth': f.depth,
                        'score': round(f.score, 4),
                        'role': f.role,
                        'note': f.note,
                    }
                    for f in module.funcs
                ],
            }
            for name, module in project.modules.items()
        },
    }
