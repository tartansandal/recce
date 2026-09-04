"""Ask a local model for the one line the static passes cannot write.

Everything else recce prints is derived from the syntax tree. This is the one
thing that is not: a sentence describing what a function's loops and branches
actually do, which is what the `code-map` skill writes by hand and what no
amount of `ast` walking will produce.

The design is a deliberate narrowing. The model is shown one function body and
asked for one line about it. It never sees the tree, the markers, the budget,
or the markdown, so there is no convention for it to violate and no global
reasoning for it to get wrong — which is why a 7B is a plausible tool here
when the same model asked to write the whole document is not.

Three guardrails matter more than the prompt:

- **The length cap is enforced here, not requested in the prompt.** Small
  models drift long. A note over budget is dropped whole rather than
  truncated, because a missing note reads as a clean map and a note cut off
  mid-clause reads as a bug.
- **Notes are cached by content hash**, so a rerun costs nothing and the same
  code gives the same map. That matters if maps get committed or diffed.
- **Failure is silent, and costs only what it has to.** No Ollama, a refused
  connection, a model that is not pulled — all of them leave every `Func.note`
  empty and the map renders exactly as it does without a model. A timeout is
  narrower: it means one function was long, not that the server is gone, so it
  costs that note alone and the run carries on. Only a run of them in
  succession is read as the server having stopped answering. The notes are an
  enhancement to something that already stands up.

Nothing here is imported unless notes are asked for, so recce stays stdlib-only
in the sense that matters: the default path opens no sockets and reads no cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .model import SPINE, TRIVIAL, Func

DEFAULT_HOST = os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')

# What `--model` means when it is given no value, and what `RECCE_MODEL=auto`
# asks for: pick a model off whatever Ollama has pulled.
AUTO = 'auto'

# `RECCE_MODEL` is the way to have notes without typing a model name on every
# run. Notes stay off by default because they are the one part of a map that a
# machine wrote: the deterministic pipeline gives the same map twice, and a
# model at temperature 0.1 does not. Setting this is a decision to trade that.
DEFAULT_MODEL = os.environ.get('RECCE_MODEL')

# Preferred first. These are the families trained on code, which is what a
# note about loops and branches asks for; anything else is a fallback that
# still has to survive the same checks against the syntax tree.
_PREFERRED_MODELS = (
    'qwen2.5-coder',
    'qwen3-coder',
    'deepseek-coder',
    'codegemma',
    'codellama',
    'starcoder2',
    'granite-code',
)

# Past this the note stops being an annotation and becomes a paragraph
# competing with the row it hangs under.
#
# It is the default rather than the law, because it is tuned to a 7B's terse
# register and a stronger model writes denser. qwen3.8:27b answered `plan`
# with 114 characters, of which `_trim` kept 70 and dropped "returns early or
# builds blocks by strategy" — a clause the reader wanted. `--note-chars`
# raises it. Everything below takes it as an argument rather than reading the
# constant, so the cap travels with the question being asked; `_key` folds it
# into the cache key, so changing it re-asks rather than serving answers
# written to a different limit.
MAX_NOTE_CHARS = 90

# Trimming can leave a clause that is true and says nothing — `render` came
# back as "loops over plain blocks", which is correct and tells a reader
# nothing they could not see from the row above. A note has to carry more than
# its own existence to be worth the line it costs.
MIN_NOTE_CHARS = 25

# A body with no shape has nothing for a note to say. One `if` and an early
# return is shape the row above already implies, so the bar is a loop, or more
# than one decision. Below it the note restates the signature at best and
# invents something at worst.
MIN_LOOPS = 1
MIN_BRANCHES = 2
MIN_LOC = 5

# Verbs a model reaches for when it is guessing rather than reading. The
# static pass knows whether a function loops, so a note claiming one where
# none exists is not an opinion to weigh — it is a checkable falsehood.
_LOOP_CLAIM = re.compile(
    r'\b(?:loops?|looping|iterat\w+|for\s+each|walks?\s+(?:through|over)|'
    r'cycles?\s+through|traverses?|repeat(?:s|edly)?)\b',
    re.IGNORECASE,
)

# The same trade in the other direction. Telling the model a function has no
# loops moved it from claiming loops to claiming branches, so the claim worth
# checking moved with it: a straight-line function that "dispatches on node
# type" is the same falsehood wearing different words.
_BRANCH_CLAIM = re.compile(
    r'\b(?:branch\w*|dispatch\w*|decides?|chooses?|switch\w*|'
    r'depending\s+on|either\b)',
    re.IGNORECASE,
)

# Asking about every function is slow and pointless — most rows are plumbing.
DEFAULT_LIMIT = 12

# Long enough that a slow model on a long function is not mistaken for a dead
# server. Sixty seconds was written when a note cost a second or two and any
# wait that long really did mean something was wrong. It does not survive a
# 27B: qwen3.8 answered `main` (93 lines) in 59.8 seconds, inside the old
# limit by two tenths of a second and over it under any load at all.
DEFAULT_TIMEOUT = 300.0

# How many timeouts in a row before the server is presumed wedged rather than
# merely slow. One timeout is now survivable — see `fill` — but survivable
# cannot mean unbounded, or a hung Ollama turns a 40-function draft run into
# forty consecutive full-length waits. Three is enough to distinguish a couple
# of long functions from a server that has stopped answering.
_MAX_CONSECUTIVE_TIMEOUTS = 3

PROMPT = """\
Describe the control flow of this Python function in ONE short line.

{shape}

Say what its branches decide and in what order. Name the condition that ends a
loop or returns early, if there is one. Do not describe the signature, do not
restate the name, do not explain why, do not count anything back to me. Under
{limit} characters. No Markdown, no quotes, no trailing full stop.

Examples of the register wanted:
    loop over records, bucket by status class, accumulate byte total
    returns early when the cache is warm, otherwise rebuilds and writes it
    walk up until a directory has no __init__.py, then stop
    dispatches on node type; recurses on the nested ones
    tries imports, then module names, then the class MRO; drops what is left

Function:
```python
{source}
```

One line:"""

# The prompt decides what the model answers, so a cached note is only good for
# the prompt that produced it. Keying on this digest means editing PROMPT
# invalidates the cache by itself.
_PROMPT_DIGEST = hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()[:8]

# Openers a small model reaches for when it has been told not to.
_PREAMBLE = re.compile(
    r'^(?:this|the)\s+(?:function|method|code)\s+'
    r'(?:is\s+used\s+to\s+|does\s+the\s+following[:,]?\s*|simply\s+)?'
    r'(?:that\s+)?',
    re.IGNORECASE,
)


@dataclass
class Report:
    """What happened, so the caller can say so rather than guess."""

    asked: int = 0
    filled: int = 0
    cached: int = 0
    rejected: int = 0
    timed_out: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def kept_rate(self) -> float:
        # Timeouts are deliberately absent. A rejection is an answer that was
        # read and refused, which says something about the model; a timeout is
        # no answer at all, and folding it in here would report a model as
        # worse for running on a busy machine.
        answered = self.filled + self.rejected
        return self.filled / float(answered) if answered else 0.0

    def summary(self) -> str:
        # Only a run that produced nothing is 'unavailable'. Once a timeout is
        # survivable a run can end early having already written half its notes,
        # and reporting that as unavailable would throw away the true count.
        if self.error and not (self.filled or self.cached):
            return 'notes: unavailable ({})'.format(self.error)
        line = 'notes: {} filled, {} cached, {} rejected of {} asked'.format(
            self.filled, self.cached, self.rejected, self.asked
        )
        if self.timed_out:
            line += ', {} timed out'.format(self.timed_out)
        if self.error:
            line += '; stopped early: {}'.format(self.error)
        if self.reasons:
            detail = ', '.join(
                '{} {}'.format(count, reason)
                for reason, count in sorted(self.reasons.items())
            )
            line += ' ({})'.format(detail)
        return line


def cache_path() -> Path:
    """Where notes are remembered between runs.

    XDG rather than a macOS-native location on purpose: the same cache path has
    to make sense on the Linux build servers this tool is really for, and a
    cache is not user-facing state that anyone goes looking for in Finder.
    """
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'recce' / 'notes.json'


def candidates(funcs: Sequence[Func], limit: int = DEFAULT_LIMIT) -> List[Func]:
    """The functions whose shape is worth a sentence, best first.

    Branching is the filter. A function with no conditionals has nothing for
    the note to say that the row above it has not already said, and spending a
    line on `returns the joined lines` is how a map fills up with words that
    are individually true and collectively useless.
    """
    worth = [
        f
        for f in funcs
        if f.role != TRIVIAL
        and f.loc >= MIN_LOC
        and (f.n_loops >= MIN_LOOPS or f.n_branches >= MIN_BRANCHES)
    ]
    worth.sort(key=lambda f: (f.role != SPINE, -f.score, f.module, f.lineno))
    return worth[:limit]


def _source_of(func: Func) -> Optional[str]:
    try:
        lines = (
            Path(func.path).read_text(encoding='utf-8', errors='replace').splitlines()
        )
    except OSError:
        return None
    body = lines[func.lineno - 1 : func.end_lineno]
    return '\n'.join(body) if body else None


def _key(
    model: str, source: str, shape: str = '', max_chars: int = MAX_NOTE_CHARS
) -> str:
    """What a cached note is valid for.

    `shape` is in here because it is part of the prompt but not part of
    `PROMPT`: `_PROMPT_DIGEST` covers the template, and the shape line is
    injected into it per function. Editing `shape_of` therefore changes the
    question while leaving the template alone, and without this the next run
    answers from the old one.
    """
    asked = hashlib.sha256((shape + '\x00' + source).encode('utf-8'))
    return '{}:{}:{}:{}'.format(
        model, max_chars, _PROMPT_DIGEST, asked.hexdigest()[:24]
    )


def why_rejected(
    raw: str,
    n_loops: Optional[int] = None,
    n_branches: Optional[int] = None,
    max_chars: int = MAX_NOTE_CHARS,
) -> Optional[str]:
    """Name the rule a response broke, or None if it passed.

    Split out from `clean` so the CLI can say *which* rule is doing the
    rejecting. Whether a 29% keep rate means the model is weak or the length
    cap is mean is not a thing to have an opinion about when it can be counted.
    """
    text = _trim(_reduce(raw), max_chars)
    if not text:
        return 'empty'
    if len(text) > max_chars:
        return 'too long'
    if len(text.split()) < 3 or len(text) < MIN_NOTE_CHARS:
        return 'too short'
    if n_loops == 0 and _LOOP_CLAIM.search(text):
        return 'invented a loop'
    if n_branches == 0 and _BRANCH_CLAIM.search(text):
        return 'invented a branch'
    return None


def _reduce(raw: str) -> str:
    """Strip a response down to its one candidate line."""
    if not raw:
        return ''
    text = raw.strip()
    if text.startswith('```'):
        text = text.strip('`').strip()
        if text.lower().startswith('python'):
            text = text[len('python') :].strip()
    text = text.splitlines()[0].strip() if text else ''
    text = text.strip('`"\'').strip()
    text = _PREAMBLE.sub('', text).strip()
    text = text.rstrip('.').strip()
    return ' '.join(text.split())


def _trim(text: str, max_chars: int = MAX_NOTE_CHARS) -> str:
    """Drop trailing clauses until the note fits, or give up on it.

    This is the one place truncation is allowed, and the distinction is worth
    being precise about. Cutting a sentence at a character count leaves a
    fragment that is not a claim at all. Cutting a comma-separated list of
    clauses after any clause leaves every surviving clause exactly as true as
    it was — "loops over records, buckets by status" is a smaller statement
    than the three-clause original, not a corrupted one.

    Measured on recce's own source, ten of eleven rejections were length
    rather than any fault in the answer, so refusing to trim was throwing away
    most of what the model got right.
    """
    if len(text) <= max_chars:
        return text
    clauses = text.split(', ')
    kept: list = []
    for clause in clauses:
        candidate = ', '.join(kept + [clause])
        if len(candidate) > max_chars:
            break
        kept.append(clause)
    # A single clause over the cap is a run-on, not a list, and there is no
    # honest place to cut it.
    return ', '.join(kept)


def clean(
    raw: str,
    n_loops: Optional[int] = None,
    n_branches: Optional[int] = None,
    max_chars: int = MAX_NOTE_CHARS,
) -> Optional[str]:
    """Reduce a model response to a usable note, or reject it.

    Rejection is the common case worth designing for. Anything that arrives
    long, empty, or still wearing a code fence is dropped rather than repaired,
    because a note is a small enough win that salvaging a bad one is not worth
    the chance of publishing a wrong one.

    The last check is the one that justifies the whole hybrid arrangement. The
    model is guessing; the syntax tree is not. When `n_loops` is zero and the
    note says the function loops, that is not a difference of opinion to weigh
    but a statement the parser has already falsified, so it is thrown away.

    This is not a hypothetical. Asked about a function whose entire body is a
    regex match and an early return, qwen2.5-coder:7b answered "loops over
    characters in line" — fluent, specific, and describing code that is not
    there. A reader has no way to catch that from the map alone, which is
    exactly why the map must catch it for them.
    """
    if why_rejected(raw, n_loops, n_branches, max_chars) is not None:
        return None
    text = _trim(_reduce(raw), max_chars)
    return text[0].lower() + text[1:] if text[:1].isupper() else text


def installed_models(host: str = DEFAULT_HOST, timeout: float = 5.0) -> List[str]:
    """Model names Ollama has pulled, smallest first, or [] if it cannot say.

    Embedding models are dropped: they are installed for other tools, they do
    not answer `/api/generate`, and picking one would turn "notes are off" into
    "every note failed".
    """
    request = urllib.request.Request(host.rstrip('/') + '/api/tags')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    models = [
        (m.get('size') or 0, m.get('name') or '')
        for m in body.get('models', [])
        if m.get('name') and 'embed' not in m['name']
    ]
    return [name for _, name in sorted(models)]


def resolve_model(host: str = DEFAULT_HOST, timeout: float = 5.0) -> Optional[str]:
    """The model `--model` with no value should use, or None if there is none.

    A code-trained family wins, in the order `_PREFERRED_MODELS` lists them,
    and the smallest build of it wins within that — a note is one line, so the
    7B answers as well as the 32B and answers sooner. Failing all of those,
    the smallest usable model installed: a weaker model writes worse notes but
    not more dangerous ones, since every note is checked against the tree
    before it is kept.
    """
    installed = installed_models(host, timeout)
    for preferred in _PREFERRED_MODELS:
        for name in installed:
            if name.startswith(preferred):
                return name
    return installed[0] if installed else None


def shape_of(func: Func) -> str:
    """The line of the prompt that tells the model what the tree already says.

    The counts are here to forbid, not to be repeated: a 7B shown any function
    reaches for "loops over", and on `graph.py` that was four rejections out of
    seven, every one of them a dispatch chain with no loop in it. Saying so up
    front is cheaper than rejecting the answer afterwards, and the rejection
    stays where it is either way — this steers the model, it does not license
    it.
    """
    if func.n_loops:
        return (
            'This function contains {} loop(s). Say what it loops over, and '
            'name the condition that ends it or returns early if there is '
            'one. Never write "various", "several", "multiple", "different" '
            'or "appropriate".'.format(func.n_loops)
        )
    return (
        'This function contains NO loops. Do not write "loop", "loops", '
        '"iterate" or "for each" — there is nothing to iterate. Name the '
        'condition it decides on and what each way leads to, or what makes '
        'it return early. Name the actual test, not how many there are: '
        'never write "various", "several", "multiple", "different" or '
        '"appropriate".'
    )


def _ask(
    host: str,
    model: str,
    source: str,
    timeout: float,
    shape: str = '',
    max_chars: int = MAX_NOTE_CHARS,
) -> Optional[str]:
    """One blocking call to Ollama's generate endpoint."""
    payload = json.dumps(
        {
            'model': model,
            'prompt': PROMPT.format(source=source, limit=max_chars, shape=shape),
            'stream': False,
            # Every Qwen from 3.6 on thinks by default, and the reasoning goes
            # in front of the answer. `num_predict` below is 60 tokens, so the
            # thinking consumes the whole budget and `response` comes back
            # empty or as a truncated fragment of reasoning — `_reduce` then
            # takes its first line and every note is rejected as 'empty'. The
            # report says the model is useless when what is wrong is the ask.
            # Sent unconditionally: Ollama ignores it on models that cannot
            # think, verified against qwen2.5-coder:7b, which answers normally
            # rather than erroring. Note that Qwen 3.6 dropped the `/no_think`
            # soft switch, so this field is the only way left to say it.
            'think': False,
            'options': {
                # Zero, not merely low: this is description, not composition,
                # and a map that changes wording between runs is a map nobody
                # trusts. At 0.1 three uncached runs of `graph.py` disagreed
                # about four notes and about whether one function got one at
                # all; at 0 they are byte-identical.
                'temperature': 0.0,
                'num_predict': 60,
            },
        }
    ).encode('utf-8')
    request = urllib.request.Request(
        host.rstrip('/') + '/api/generate',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode('utf-8'))
    return body.get('response')


def _is_timeout(exc: BaseException) -> bool:
    """Whether this failure was the clock, rather than the server.

    urllib reports the two shapes of timeout differently: a read that runs out
    of time raises `TimeoutError` directly, while one that times out
    connecting arrives as a `URLError` carrying it in `reason`. Both mean the
    same thing here and only the second is wrapped, so the wrapper is unpacked
    rather than matched on type alone. `socket.timeout` needs no separate case;
    it has been an alias of `TimeoutError` since 3.10, below the floor.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, 'reason', None), TimeoutError)


def fill(
    funcs: Sequence[Func],
    model: str,
    host: str = DEFAULT_HOST,
    limit: int = DEFAULT_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    max_chars: int = MAX_NOTE_CHARS,
) -> Report:
    """Write a note onto the functions worth one. Never raises."""
    report = Report()
    chosen = candidates(funcs, limit)
    if not chosen:
        return report

    path = cache_path()
    cache: Dict[str, str] = {}
    if use_cache and path.exists():
        try:
            cache = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cache = {}

    dirty = False
    # Counted in a row, not in total: two slow functions in a forty-function
    # run are the model being slow, while three in succession are the server
    # being gone. A cache hit leaves the count alone, having asked nothing.
    consecutive_timeouts = 0
    for func in chosen:
        source = _source_of(func)
        if source is None:
            continue
        report.asked += 1
        shape = shape_of(func)
        key = _key(model, source, shape, max_chars)
        if use_cache and key in cache:
            func.note = cache[key]
            report.cached += 1
            continue
        try:
            raw = _ask(host, model, source, timeout, shape, max_chars)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            # A refused socket and a slow generation are not the same failure,
            # and treating them alike is what made `--draft` produce nothing at
            # all against a 27B. A connection error really does mean every
            # remaining call fails the same way, so stopping is right. A
            # timeout only means this function was long — the next one may be
            # short — so it costs one note, not the other thirty-nine.
            if _is_timeout(exc):
                report.timed_out += 1
                consecutive_timeouts += 1
                if consecutive_timeouts < _MAX_CONSECUTIVE_TIMEOUTS:
                    continue
                # Slow is survivable; wedged is not. Past this the wait stops
                # being evidence about one function and starts being evidence
                # about the server.
                report.error = 'timed out {} times in a row'.format(
                    consecutive_timeouts
                )
                break
            report.error = str(getattr(exc, 'reason', None) or exc)
            break
        consecutive_timeouts = 0
        reason = why_rejected(
            raw or '',
            n_loops=func.n_loops,
            n_branches=func.n_branches,
            max_chars=max_chars,
        )
        if reason is not None:
            report.rejected += 1
            report.reasons[reason] = report.reasons.get(reason, 0) + 1
            continue
        note = clean(
            raw or '',
            n_loops=func.n_loops,
            n_branches=func.n_branches,
            max_chars=max_chars,
        )
        func.note = note
        report.filled += 1
        if use_cache:
            cache[key] = note
            dirty = True

    if dirty:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(cache, indent=0, sort_keys=True), encoding='utf-8'
            )
        except OSError as exc:
            print('recce: could not write note cache: {}'.format(exc), file=sys.stderr)
    return report
