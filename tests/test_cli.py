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
    main,
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


class TestAnExistingOutputFileIsNotClobbered:
    """`--out` names a document once `--draft` exists.

    The map used to be disposable: overwriting it cost a rerun. A draft is
    annotated by hand over days, and the command that refreshes it is the
    command that destroys it, keystroke for keystroke. The notes cache brings
    recce's own sentences back; nothing brings the reader's back.
    """

    def _project(self, tmp_path):
        (tmp_path / 'pkg').mkdir()
        (tmp_path / 'pkg' / 'm.py').write_text(
            'def go(rows):\n    for r in rows:\n        if r:\n            print(r)\n'
        )
        return tmp_path / 'pkg'

    def test_it_refuses_and_leaves_the_file_exactly_as_it_was(self, tmp_path, capsys):
        target = self._project(tmp_path)
        out = tmp_path / 'draft-code-map.md'
        precious = '# my map\n\nhours of hand annotation\n'
        out.write_text(precious)

        code = main([str(target), '--no-llm', '-o', str(out)])

        assert code == 2
        assert out.read_text() == precious, 'the file was written to anyway'
        assert 'exists' in capsys.readouterr().err

    def test_force_is_how_you_say_you_meant_it(self, tmp_path):
        target = self._project(tmp_path)
        out = tmp_path / 'draft-code-map.md'
        out.write_text('# my map\n')

        assert main([str(target), '--no-llm', '-o', str(out), '--force']) == 0
        assert out.read_text() != '# my map\n'
        assert 'go' in out.read_text()

    def test_a_new_file_needs_no_ceremony(self, tmp_path):
        target = self._project(tmp_path)
        out = tmp_path / 'fresh.md'

        assert main([str(target), '--no-llm', '-o', str(out)]) == 0
        assert 'go' in out.read_text()

    def test_stdout_is_left_to_the_shell(self, tmp_path, capsys):
        """`>` is the user's own explicit act; recce does not second-guess it."""
        target = self._project(tmp_path)
        (tmp_path / 'draft-code-map.md').write_text('# my map\n')

        assert main([str(target), '--no-llm']) == 0
        assert 'go' in capsys.readouterr().out

    def test_the_refusal_comes_before_the_model_not_after_it(
        self, tmp_path, monkeypatch
    ):
        """A `--draft` run spends a quarter of an hour before it has output.

        Refusing to write after that wait would be a worse answer than
        refusing before it, so the check cannot live at the write.
        """
        target = self._project(tmp_path)
        out = tmp_path / 'draft-code-map.md'
        out.write_text('# my map\n')

        def explode(*a, **k):
            raise AssertionError('asked the model before checking the output file')

        monkeypatch.setattr(notes, 'fill', explode)
        assert main([str(target), '--model', 'qwen3.8:27b-mlx', '-o', str(out)]) == 2
