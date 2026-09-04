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

from . import __version__, notes
from .extract import discover
from .graph import resolve
from .rank import annotate, plan, rendered_funcs
from .render import render

# The budget a map is read on screen with, and the one it is written to a file
# with, are not the same number. The default is the orientation case: one
# screen, read once, where a map running to three screens has failed.
DEFAULT_MAX_LINES = 40

# `--draft` is the other case. The map is generated once for an investigation
# lasting hours or days, saved, and then edited by hand as the reader learns
# the code; it is a starting document, not a screenful.
#
# 200 is measured, not chosen, and it took two codebases to find. `notes` is
# the first entry in `_CONCESSION_ORDER`, so a tight budget starves it: on
# requests, 40 asked notes rendered 5 at the default, 21 at 80, and all 40 at
# 120, with 200 giving a byte-identical map.
#
# 120 would be the answer if requests were the whole world. It is not: rich is
# 100 modules to requests' 19, and at 120 it still renders only 23 of 40 while
# 200 renders all 40. Where the budget stops binding is a property of the tree,
# so the number has to clear the larger case, and clearing it costs the smaller
# case nothing at all — requests is the same map either way. The concession
# order is not wrong here; the pressure is.
DRAFT_MAX_LINES = 200
DRAFT_NOTES_LIMIT = 40


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
        '-f',
        '--force',
        action='store_true',
        help='overwrite the --out file if it already exists',
    )
    parser.add_argument(
        '--max-lines',
        type=int,
        default=None,
        help='line budget per fenced tree before the map splits (default: {})'.format(
            DEFAULT_MAX_LINES
        ),
    )
    parser.add_argument(
        '--draft',
        action='store_true',
        help=(
            'settings for a map you save and annotate rather than read once: '
            '--max-lines {} and --notes-limit {}. Either flag given '
            'explicitly still wins'
        ).format(DRAFT_MAX_LINES, DRAFT_NOTES_LIMIT),
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
        '--model',
        nargs='?',
        const=notes.AUTO,
        default=notes.DEFAULT_MODEL,
        metavar='NAME',
        help=(
            'Ollama model to write branch-shape notes with '
            '(e.g. qwen2.5-coder:7b). Give it no value to pick an installed '
            'one. Set RECCE_MODEL=auto to have notes without the flag'
        ),
    )
    parser.add_argument(
        '--notes-limit',
        type=int,
        default=None,
        help='how many functions to ask about (default: {})'.format(
            notes.DEFAULT_LIMIT
        ),
    )
    parser.add_argument(
        '--note-chars',
        type=int,
        default=notes.MAX_NOTE_CHARS,
        metavar='N',
        help=(
            'longest a note may be before it is trimmed to whole clauses '
            '(default: {}). Raise it for a model that writes denser than the '
            'default was tuned for; it is part of the cache key, so a change '
            're-asks rather than serving answers written to another limit'
        ).format(notes.MAX_NOTE_CHARS),
    )
    parser.add_argument(
        '--max-source-chars',
        type=int,
        default=notes.MAX_SOURCE_CHARS,
        metavar='N',
        help=(
            'longest function body to ask about at all (default: {}). Raise it '
            'when the machine serving the model can take it'
        ).format(notes.MAX_SOURCE_CHARS),
    )
    parser.add_argument(
        '--notes-timeout',
        type=float,
        default=notes.DEFAULT_TIMEOUT,
        metavar='SECONDS',
        help=(
            'how long one note may take before it is given up on '
            '(default: {:g}). A timeout costs that note, not the rest of the '
            'run'
        ).format(notes.DEFAULT_TIMEOUT),
    )
    parser.add_argument(
        '--ollama-host',
        default=notes.DEFAULT_HOST,
        help='where Ollama is listening, else $OLLAMA_HOST (default: {})'.format(
            notes.DEFAULT_HOST
        ),
    )
    parser.add_argument(
        '--no-cache',
        action='store_true',
        help='ask the model again rather than reusing remembered notes',
    )
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='never call a model, whatever --model says; the default already does not',
    )
    parser.add_argument('--version', action='version', version=__version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # `--draft` fills in only what was not asked for, so `--draft --max-lines
    # 60` means 60 rather than silently meaning 120. Both flags default to None
    # for exactly this: argparse cannot otherwise tell "left alone" from "given
    # the same value the default happens to be".
    fallback_lines = DRAFT_MAX_LINES if args.draft else DEFAULT_MAX_LINES
    fallback_notes = DRAFT_NOTES_LIMIT if args.draft else notes.DEFAULT_LIMIT
    max_lines = args.max_lines if args.max_lines is not None else fallback_lines
    notes_limit = args.notes_limit if args.notes_limit is not None else fallback_notes

    if not args.target.exists():
        print(
            'recce: no such file or directory: {}'.format(args.target), file=sys.stderr
        )
        return 2

    # Checked here rather than at the write, and the distance is the point: a
    # `--draft` run against a 27B spends a quarter of an hour in the model
    # before there is anything to write, and refusing after that wait is a
    # worse answer than refusing before it.
    #
    # What `--out` names stopped being disposable when `--draft` arrived. The
    # map used to be a cheap artifact where an overwrite cost a rerun; a draft
    # is annotated by hand for days, the notes cache means recce's own
    # sentences come back, and the reader's marginal ones do not. The refresh
    # command and the destroy command were the same keystrokes.
    if args.out and args.out.exists() and not args.force:
        print(
            'recce: {} exists; --force to overwrite'.format(args.out), file=sys.stderr
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

    # Notes are written before planning, not after, because a note is a line
    # and the budget counts lines. Annotating twice is cheap and idempotent:
    # `plan` re-runs it to get roles and scores, and leaves `note` alone.
    report = None
    model = args.model
    if model == notes.AUTO and not args.no_llm:
        # Resolved to a name before anything asks for a note: the note cache
        # keys on the model, so leaving it as 'auto' would file every answer
        # under a name no future run looks up.
        model = notes.resolve_model(args.ollama_host)
        if model is None:
            print(
                'recce: no usable model at {}; mapping without notes'.format(
                    args.ollama_host
                ),
                file=sys.stderr,
            )
        else:
            print('recce: notes from {}'.format(model), file=sys.stderr)
    if model and not args.no_llm:
        annotate(project, graph)
        # Plan once before asking anything, purely to find out which functions
        # reach the page, then ask only about those. Scoring is global and the
        # map is not: on `rich`, 100 modules ranked and 8 rendered, 19 of 40
        # asks went to modules the reader never sees.
        #
        # The preview is deliberately optimistic. Without notes the blocks fit
        # more rows, so this set is a superset of what survives once the notes
        # are added and start costing lines -- which is the concession
        # mechanism doing its job, not an error. A superset is the right side
        # to err on: asking about a row that later drops costs one note, while
        # excluding one that would have stayed loses it for good.
        rendered = {
            notes.key_of(f) for f in rendered_funcs(plan(project, graph, max_lines))
        }
        report = notes.fill(
            project.funcs(),
            model=model,
            host=args.ollama_host,
            limit=notes_limit,
            use_cache=not args.no_cache,
            max_chars=args.note_chars,
            timeout=args.notes_timeout,
            rendered=rendered,
            max_source_chars=args.max_source_chars,
        )
        # `--stats` prints this same line further down, and printing it twice
        # made a failed run look like two failed runs.
        if report.error and not args.stats:
            print('recce: {}'.format(report.summary()), file=sys.stderr)

    mapping = plan(project, graph, max_lines=max_lines)

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
        if report is not None:
            print('recce: {}'.format(report.summary()), file=sys.stderr)
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
                        'n_loops': f.n_loops,
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
