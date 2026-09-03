"""Argument resolution, where a preset and an explicit flag can disagree.

`--draft` is not a different code path. It is two numbers, and the only thing
worth testing is that giving one of them by hand still means what it says.
"""

from __future__ import annotations

from recce import notes
from recce.cli import (
    DEFAULT_MAX_LINES,
    DRAFT_MAX_LINES,
    DRAFT_NOTES_LIMIT,
    build_parser,
)


def resolve(argv):
    """What `main` computes before it maps anything."""
    args = build_parser().parse_args(argv)
    lines = (
        args.max_lines
        if args.max_lines is not None
        else (DRAFT_MAX_LINES if args.draft else DEFAULT_MAX_LINES)
    )
    limit = (
        args.notes_limit
        if args.notes_limit is not None
        else (DRAFT_NOTES_LIMIT if args.draft else notes.DEFAULT_LIMIT)
    )
    return lines, limit


def test_the_default_is_the_one_screen_budget():
    assert resolve(['pkg']) == (DEFAULT_MAX_LINES, notes.DEFAULT_LIMIT)


def test_draft_loosens_both_knobs_together():
    assert resolve(['pkg', '--draft']) == (DRAFT_MAX_LINES, DRAFT_NOTES_LIMIT)


def test_an_explicit_flag_beats_the_preset():
    """Otherwise `--draft --max-lines 60` silently means 120.

    This is why both flags default to None rather than to their numbers:
    argparse cannot otherwise tell an untouched flag from one given the value
    the default happened to hold.
    """
    assert resolve(['pkg', '--draft', '--max-lines', '60']) == (60, DRAFT_NOTES_LIMIT)
    assert resolve(['pkg', '--draft', '--notes-limit', '5']) == (DRAFT_MAX_LINES, 5)


def test_draft_is_off_unless_asked_for():
    assert build_parser().parse_args(['pkg']).draft is False


def test_note_chars_defaults_to_the_constant_it_overrides():
    assert build_parser().parse_args(['pkg']).note_chars == notes.MAX_NOTE_CHARS
    assert build_parser().parse_args(['pkg', '--note-chars', '140']).note_chars == 140
