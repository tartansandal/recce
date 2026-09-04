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
    """The counts are there to forbid a claim, not to be counted back at us."""
    straight = notes.shape_of(make(loops=0, branches=6))
    looping = notes.shape_of(make(loops=2, branches=4))
    assert 'NO loops' in straight
    assert 'NO loops' not in looping
    assert '2 loop' in looping
    # Both halves ask for the deciding condition and ban the vague stand-ins
    # the model reaches for when it has not found one.
    for line in (straight, looping):
        assert 'condition' in line
        assert 'various' in line


def test_the_cache_key_covers_the_shape_line():
    """The shape line is part of the prompt but not part of PROMPT.

    `_PROMPT_DIGEST` covers the template; the shape is injected into it per
    function, so editing `shape_of` changes the question asked while leaving
    the template alone. Without this the next run answers from the old one.
    """
    source = 'def go():\n    pass\n'
    looped = notes._key('m', source, notes.shape_of(make(loops=2)))
    straight = notes._key('m', source, notes.shape_of(make(loops=0)))
    assert looped != straight
    assert notes._key('m', source, 'a') != notes._key('m', source, 'b')


class TestNoteCharsIsAnArgumentNotAConstant:
    """`--note-chars` has to reach every rule that reads the cap.

    The cap is enforced in four places — the prompt asks for it, `_trim` cuts
    to it, `why_rejected` measures against it, and `_key` files the answer
    under it. A flag that moved only some of them would produce notes written
    to one limit and judged by another.
    """

    def test_a_raised_cap_keeps_a_clause_the_default_would_drop(self):
        long = (
            'loops over funcs and modules, branches on module split then fit '
            'budget, returns early or builds blocks by strategy'
        )
        assert len(notes.clean(long, 1)) <= notes.MAX_NOTE_CHARS
        raised = notes.clean(long, 1, max_chars=140)
        assert raised == long
        assert 'builds blocks by strategy' in raised

    def test_a_lowered_cap_rejects_what_the_default_accepts(self):
        answer = 'loops over rows, buckets by status, totals the bytes'
        assert notes.clean(answer, 1) == answer
        assert notes.clean(answer, 1, max_chars=20) is None

    def test_the_cap_is_part_of_what_a_cached_note_is_valid_for(self):
        source = 'def go():\n    pass\n'
        assert notes._key('m', source, '', 90) != notes._key('m', source, '', 140)

    def test_the_prompt_asks_for_the_cap_it_will_be_judged_against(self):
        sent = {}

        class _Response:
            def read(self):
                return json.dumps({'response': 'loops over rows'}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            sent['body'] = json.loads(request.data.decode())
            return _Response()

        import urllib.request

        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            notes._ask('http://h', 'm', 'def go(): pass', 5.0, '', max_chars=140)
        finally:
            urllib.request.urlopen = original
        # `PROMPT` wraps between "Under" and the number, so match the number.
        assert '140 characters' in sent['body']['prompt']
        assert '90 characters' not in sent['body']['prompt']
        # The reason the whole comparison was runnable at all: a thinking
        # model spends num_predict on reasoning and returns an empty response.
        assert sent['body']['think'] is False


class TestATimeoutIsNotADeadServer:
    """The distinction `--draft` forced.

    A note used to cost a second or two, so waiting a full minute for one
    really did mean the server had gone and abandoning the rest was right.
    Against a 27B a timeout means the opposite — this function was long — and
    the old behaviour threw away thirty-nine good notes to save one wait.
    """

    def _funcs(self, n):
        return [make(name='f{}'.format(i), loops=2, loc=10 + i) for i in range(n)]

    def test_one_slow_function_costs_one_note(self, monkeypatch):
        calls = []

        def flaky(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            if len(calls) == 2:
                raise TimeoutError('read timed out')
            return 'loops over rows, buckets by status, totals the bytes'

        monkeypatch.setattr(notes, '_ask', flaky)
        monkeypatch.setattr(
            notes, '_source_of', lambda f: 'def {}(): pass'.format(f.name)
        )
        report = notes.fill(self._funcs(4), model='m', use_cache=False)
        assert len(calls) == 4, 'the run stopped instead of skipping the slow one'
        assert (report.filled, report.timed_out, report.error) == (3, 1, None)

    def test_a_timeout_wrapped_in_a_urlerror_counts_the_same(self, monkeypatch):
        """urllib wraps a connect timeout but raises a read timeout bare."""
        import urllib.error

        def wrapped(host, model, source, timeout, shape='', max_chars=90):
            raise urllib.error.URLError(TimeoutError('timed out'))

        monkeypatch.setattr(notes, '_ask', wrapped)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(3), model='m', use_cache=False)
        assert report.timed_out == 3
        assert 'in a row' in (report.error or '')

    def test_a_refused_connection_still_stops_the_whole_run(self, monkeypatch):
        """The original reasoning, which is still right for this case."""
        import urllib.error

        calls = []

        def refused(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            raise urllib.error.URLError(ConnectionRefusedError('refused'))

        monkeypatch.setattr(notes, '_ask', refused)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(5), model='m', use_cache=False)
        assert len(calls) == 1, 'kept asking a server that is not there'
        assert report.timed_out == 0
        assert 'unavailable' in report.summary()

    def test_timeouts_in_a_row_are_read_as_a_wedged_server(self, monkeypatch):
        calls = []

        def always_slow(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            raise TimeoutError('read timed out')

        monkeypatch.setattr(notes, '_ask', always_slow)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(40), model='m', use_cache=False)
        assert len(calls) == notes._MAX_CONSECUTIVE_FAILURES
        assert report.error and 'in a row' in report.error

    def test_the_streak_is_consecutive_not_cumulative(self, monkeypatch):
        """Two slow functions in a long run are slowness, not a dead server."""
        calls = []

        def every_other(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            if len(calls) % 2 == 0:
                raise TimeoutError('read timed out')
            return 'loops over rows, buckets by status, totals the bytes'

        monkeypatch.setattr(notes, '_ask', every_other)
        monkeypatch.setattr(
            notes, '_source_of', lambda f: 'def {}(): pass'.format(f.name)
        )
        report = notes.fill(self._funcs(8), model='m', use_cache=False)
        assert len(calls) == 8
        assert (report.filled, report.timed_out, report.error) == (4, 4, None)

    def test_a_part_finished_run_reports_its_notes_not_just_its_error(self):
        """'unavailable' would discard a true count of what was written."""
        partial = notes.Report(asked=9, filled=6, timed_out=3, error='timed out')
        assert 'unavailable' not in partial.summary()
        assert '6 filled' in partial.summary()
        assert '3 timed out' in partial.summary()
        nothing = notes.Report(asked=1, filled=0, error='connection refused')
        assert 'unavailable' in nothing.summary()

    def test_timeouts_do_not_make_the_model_look_worse_than_it_is(self):
        report = notes.Report(filled=3, rejected=1, timed_out=6)
        assert report.kept_rate == 0.75


class TestFailuresThatAreAboutOneFunction:
    """A 5xx joins the timeout, for the same reason and by the same route.

    The claim that a non-timeout failure means the server is gone was written
    with a refused socket in mind, where it holds. It does not hold for the
    500 that actually turned up: an out-of-memory panic in the model runner on
    one very long function, with the next function a tenth the size and fine.
    """

    def _funcs(self, n):
        return [make(name='f{}'.format(i), loops=2, loc=10 + i) for i in range(n)]

    def _http(self, code):
        import urllib.error

        return urllib.error.HTTPError('u', code, 'boom', {}, None)

    def test_one_server_error_costs_one_note(self, monkeypatch):
        calls = []

        def flaky(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            if len(calls) == 2:
                raise self._http(500)
            return 'loops over rows, buckets by status, totals the bytes'

        monkeypatch.setattr(notes, '_ask', flaky)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(4), model='m', limit=4, use_cache=False)
        assert len(calls) == 4, 'the run stopped instead of skipping the bad one'
        assert (report.filled, report.server_errors, report.error) == (3, 1, None)

    def test_a_4xx_is_not_survivable(self, monkeypatch):
        """A 404 is a model that is not pulled: every later call is identical."""
        calls = []

        def missing(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            raise self._http(404)

        monkeypatch.setattr(notes, '_ask', missing)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(5), model='m', limit=5, use_cache=False)
        assert len(calls) == 1
        assert report.server_errors == 0

    def test_mixed_failures_share_one_streak(self, monkeypatch):
        """Alternating kinds still mean the server has stopped answering."""
        calls = []

        def alternating(host, model, source, timeout, shape='', max_chars=90):
            calls.append(source)
            raise TimeoutError('slow') if len(calls) % 2 else self._http(503)

        monkeypatch.setattr(notes, '_ask', alternating)
        monkeypatch.setattr(notes, '_source_of', lambda f: 'def go(): pass')
        report = notes.fill(self._funcs(40), model='m', limit=40, use_cache=False)
        assert len(calls) == notes._MAX_CONSECUTIVE_FAILURES
        assert report.timed_out + report.server_errors == 3
        assert 'in a row' in (report.error or '')


class TestAFunctionTooLongToDescribe:
    def test_it_is_never_asked_about(self, monkeypatch):
        asked = []

        def spy(host, model, source, timeout, shape='', max_chars=90):
            asked.append(len(source))
            return 'loops over rows, buckets by status, totals the bytes'

        monkeypatch.setattr(notes, '_ask', spy)
        monkeypatch.setattr(
            notes, '_source_of', lambda f: 'x' * (notes.MAX_SOURCE_CHARS + 1)
        )
        report = notes.fill([make(loops=2)], model='m', use_cache=False)
        assert asked == []
        assert (report.asked, report.oversized) == (0, 1)

    def test_skipping_one_costs_its_slot_not_a_note(self, monkeypatch):
        """Oversampling is the point: the next best candidate moves up."""
        funcs = [make(name='f{}'.format(i), loops=2, loc=10 + i) for i in range(9)]
        huge = {'f8', 'f7', 'f6'}

        monkeypatch.setattr(
            notes,
            '_source_of',
            lambda f: (
                'x' * (notes.MAX_SOURCE_CHARS + 1)
                if f.name in huge
                else 'def go(): pass'
            ),
        )
        monkeypatch.setattr(
            notes,
            '_ask',
            lambda *a, **k: 'loops over rows, buckets by status, totals bytes',
        )
        report = notes.fill(funcs, model='m', limit=5, use_cache=False)
        assert report.asked == 5, 'a skipped function shortened the run'
        assert report.filled == 5


class TestOnlyAskAboutFunctionsThatRender:
    def test_a_function_off_the_page_is_not_asked_about(self):
        on = make(name='shown', loops=2)
        off = make(name='hidden', loops=2)
        off.qualname = 'hidden'
        picked = notes.candidates([on, off], 10, rendered={notes.key_of(on)})
        assert [f.name for f in picked] == ['shown']

    def test_no_set_means_no_filter(self):
        funcs = [make(name='a', loops=2), make(name='b', loops=2)]
        funcs[1].qualname = 'b'
        assert len(notes.candidates(funcs, 10)) == 2

    def test_the_key_separates_overloads_sharing_a_name(self):
        """requests declares HTTPBasicAuth.__init__ three times."""
        stub = make(name='__init__')
        body = make(name='__init__')
        body.lineno = 42
        assert notes.key_of(stub) != notes.key_of(body)
