"""The model stage: what it is allowed to say, and what happens when it lies.

Every test here runs offline. `clean` and `candidates` are pure functions, and
the one test that touches the network points at a closed port on purpose —
degradation is a feature with a test, not an assumption.
"""

from __future__ import annotations

import hashlib
import json

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


class _FakeTags:
    """Stand-in for the /api/tags response body."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


def _tags(monkeypatch, models):
    payload = {'models': [{'name': n, 'size': s} for n, s in models]}
    monkeypatch.setattr(
        notes.urllib.request, 'urlopen', lambda *a, **k: _FakeTags(payload)
    )


def test_a_code_model_wins_over_a_general_one(monkeypatch):
    """A note about loops and branches is what these families are trained on."""
    _tags(
        monkeypatch, [('llama3:8b', 4_000_000_000), ('qwen2.5-coder:7b', 4_700_000_000)]
    )
    assert notes.resolve_model() == 'qwen2.5-coder:7b'


def test_the_smallest_build_of_the_preferred_family_wins(monkeypatch):
    """A note is one line: the 7B answers as well as the 32B and answers sooner."""
    _tags(
        monkeypatch,
        [('qwen2.5-coder:32b', 20_000_000_000), ('qwen2.5-coder:7b', 4_700_000_000)],
    )
    assert notes.resolve_model() == 'qwen2.5-coder:7b'


def test_preference_order_beats_size(monkeypatch):
    """`_PREFERRED_MODELS` is a ranking, not a tie-break on bytes."""
    _tags(
        monkeypatch,
        [('codellama:7b', 3_800_000_000), ('qwen2.5-coder:14b', 9_000_000_000)],
    )
    assert notes.resolve_model() == 'qwen2.5-coder:14b'


def test_an_embedding_model_is_never_picked(monkeypatch):
    """It does not answer /api/generate, so picking it fails every note."""
    _tags(monkeypatch, [('nomic-embed-text:latest', 274_000_000)])
    assert notes.installed_models() == []
    assert notes.resolve_model() is None


def test_a_general_model_is_the_fallback(monkeypatch):
    """Weaker notes, not more dangerous ones — the tree check is unchanged."""
    _tags(monkeypatch, [('mistral:7b', 4_100_000_000), ('phi3:mini', 2_200_000_000)])
    assert notes.resolve_model() == 'phi3:mini'


def test_no_ollama_resolves_to_no_model(monkeypatch):
    def boom(*a, **k):
        raise notes.urllib.error.URLError('refused')

    monkeypatch.setattr(notes.urllib.request, 'urlopen', boom)
    assert notes.installed_models() == []
    assert notes.resolve_model() is None


def test_bare_model_flag_asks_for_a_pick():
    """`--model` with no value is the whole point of the auto path."""
    from recce.cli import build_parser

    assert build_parser().parse_args(['x', '--model']).model == notes.AUTO
    assert build_parser().parse_args(['x', '--model', 'm:1b']).model == 'm:1b'


def test_a_branch_claimed_where_none_exists_is_dropped():
    """Grounding the prompt moved the lie from loops to branches."""
    claim = 'dispatches on the node type it was handed'
    assert notes.why_rejected(claim, n_loops=0, n_branches=0) == 'invented a branch'
    assert notes.clean(claim, n_loops=0, n_branches=0) is None


def test_a_branch_claim_survives_when_the_function_branches():
    note = notes.clean(
        'dispatches on `bound == owner`, then on the attr', n_loops=0, n_branches=4
    )
    assert note == 'dispatches on `bound == owner`, then on the attr'


def test_the_shape_line_forbids_loops_only_when_there_are_none():
    straight = make(loops=0, branches=6)
    looping = make(loops=2, branches=4)
    assert 'NO loops' in notes.shape_of(straight)
    assert '6 branch' in notes.shape_of(straight)
    assert 'NO loops' not in notes.shape_of(looping)
    assert '2 loop' in notes.shape_of(looping)
