"""The model stage: what it is allowed to say, and what happens when it lies.

Every test here runs offline. `clean` and `candidates` are pure functions, and
the one test that touches the network points at a closed port on purpose —
degradation is a feature with a test, not an assumption.
"""

from __future__ import annotations

import hashlib

from recce import notes
from recce.model import KEEP, SPINE, TRIVIAL, Func


def make(name='go', loops=1, branches=2, loc=10, role=KEEP, score=0.5):
    return Func(
        name=name,
        qualname=name,
        module='m',
        path='m.py',
        lineno=1,
        end_lineno=loc,
        args=[],
        returns=None,
        doc=None,
        n_branches=branches,
        n_loops=loops,
        loc=loc,
        role=role,
        score=score,
    )


class TestClean:
    def test_a_plain_answer_survives(self):
        assert notes.clean('loops over records, buckets by status', 1) == (
            'loops over records, buckets by status'
        )

    def test_a_loop_claim_is_refused_when_the_parser_saw_no_loop(self):
        """The check that justifies the hybrid arrangement.

        qwen2.5-coder:7b, shown a function whose whole body is a regex match
        and an early return, answered 'loops over characters in line'. Fluent,
        specific, and about code that is not there.
        """
        for claim in (
            'loops over characters in line, branches on match',
            'iterates through the string and returns a dict',
            'walks through each field, returning None on failure',
            'for each token, builds the record',
        ):
            assert notes.clean(claim, n_loops=0) is None, claim

    def test_the_same_claim_is_fine_when_there_is_a_loop(self):
        assert notes.clean('loops over characters in line, branches on match', 1)

    def test_an_unknown_loop_count_does_not_veto(self):
        """`clean` is usable without the fact table; it just checks less."""
        assert notes.clean('loops over the rows and totals them')

    def test_over_length_is_dropped_whole_not_truncated(self):
        long = 'x ' * 200
        assert notes.clean(long, 1) is None

    def test_a_fenced_answer_is_unwrapped(self):
        assert notes.clean('```\nloops over rows, sums them\n```', 1) == (
            'loops over rows, sums them'
        )

    def test_preamble_is_stripped(self):
        assert (
            notes.clean('This function loops over rows and sums them.', 1)
            == 'loops over rows and sums them'
        )

    def test_only_the_first_line_is_taken(self):
        answer = 'loops over rows and totals them\nand then does more'
        assert notes.clean(answer, 1) == 'loops over rows and totals them'

    def test_a_true_but_empty_fragment_is_not_worth_a_line(self):
        """`render` came back as 'loops over plan.blocks' after trimming.

        Correct, and it tells a reader nothing the row above did not. A note
        has to carry more than its own existence.
        """
        assert notes.clean('loops over blocks', 1) is None

    def test_trimming_drops_whole_clauses_never_part_of_one(self):
        long = (
            'loops over the records in the batch, buckets each one by status class, '
            'accumulates the running byte total, and finally sorts the routes'
        )
        trimmed = notes.clean(long, 1)
        assert trimmed
        assert len(trimmed) <= notes.MAX_NOTE_CHARS
        for clause in trimmed.split(', '):
            assert clause in long

    def test_a_single_run_on_clause_has_no_honest_cut_point(self):
        assert notes.clean('x' * 200, 1) is None

    def test_empty_and_near_empty_answers_are_refused(self):
        for raw in ('', '   ', '```', 'loops'):
            assert notes.clean(raw, 1) is None, repr(raw)


class TestCandidates:
    def test_a_single_branch_and_no_loop_is_not_worth_a_note(self):
        """`parse_line`'s shape: one guard clause. The row already says it."""
        assert notes.candidates([make(loops=0, branches=1)]) == []

    def test_a_loop_alone_qualifies(self):
        assert notes.candidates([make(loops=1, branches=1)])

    def test_two_branches_alone_qualify(self):
        assert notes.candidates([make(loops=0, branches=2)])

    def test_trivial_functions_are_never_asked_about(self):
        assert notes.candidates([make(role=TRIVIAL)]) == []

    def test_tiny_bodies_are_never_asked_about(self):
        assert notes.candidates([make(loc=3)]) == []

    def test_the_spine_is_asked_about_first(self):
        low_spine = make(name='spine', role=SPINE, score=0.1)
        high_plain = make(name='plain', role=KEEP, score=0.9)
        assert [f.name for f in notes.candidates([high_plain, low_spine])] == [
            'spine',
            'plain',
        ]

    def test_the_limit_is_honoured(self):
        assert len(notes.candidates([make(name='f%d' % i) for i in range(20)], 5)) == 5


def test_an_unreachable_model_leaves_a_clean_map_not_an_error(tmp_path):
    """No Ollama, no notes, no traceback, no partial output."""
    source = tmp_path / 'm.py'
    source.write_text(
        'def go(rows):\n    for r in rows:\n        if r:\n            print(r)\n'
    )
    func = make()
    func.path = str(source)
    report = notes.fill(
        [func],
        model='nonexistent',
        host='http://127.0.0.1:9',
        timeout=1.0,
        use_cache=False,
    )
    assert func.note is None
    assert report.error
    assert report.filled == 0
    assert 'unavailable' in report.summary()


def test_a_function_whose_file_has_gone_is_skipped_silently(tmp_path):
    """`asked` stays honest: nothing was asked, so nothing is reported as such."""
    func = make()
    func.path = str(tmp_path / 'vanished.py')
    report = notes.fill([func], model='m', host='http://127.0.0.1:9', use_cache=False)
    assert (report.asked, report.filled, report.error) == (0, 0, None)


def test_the_cache_key_covers_the_prompt(monkeypatch):
    """A note is only good for the prompt that produced it.

    Editing PROMPT used to leave every entry looking valid, and `--no-cache`
    did not clear them — it never writes, so the stale notes came back on the
    next run without the flag.
    """
    assert (
        notes._PROMPT_DIGEST
        == hashlib.sha256(notes.PROMPT.encode('utf-8')).hexdigest()[:8]
    )
    source = 'def go():\n    pass\n'
    before = notes._key('m', source)
    monkeypatch.setattr(notes, '_PROMPT_DIGEST', 'deadbeef')
    assert notes._key('m', source) != before
