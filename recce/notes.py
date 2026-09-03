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
- **Failure is silent and total.** No Ollama, a timeout, a refused connection,
  a model that is not pulled — all of them leave `Func.note` empty and the map
  renders exactly as it does without a model. The notes are an enhancement to
  something that already stands up.

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

# Past this the note stops being an annotation and becomes a paragraph
# competing with the row it hangs under.
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

# Asking about every function is slow and pointless — most rows are plumbing.
DEFAULT_LIMIT = 12

PROMPT = """\
Describe the control flow of this Python function in ONE short line.

Say what it loops over and what its branches decide. Name the condition that
ends a loop or returns early, if there is one. If it recurses or dispatches
rather than looping, say that instead — do not call it a loop. Do not describe
the signature, do not restate the name, do not explain why. Under {limit}
characters. No Markdown, no quotes, no trailing full stop.

Examples of the register wanted:
    loop over records, bucket by status class, accumulate byte total
    returns early when the cache is warm, otherwise rebuilds and writes it
    walk up until a directory has no __init__.py, then stop
    dispatches on node type; recurses on the nested ones

Function:
```python
{source}
```

One line:"""

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
    reasons: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def kept_rate(self) -> float:
        answered = self.filled + self.rejected
        return self.filled / float(answered) if answered else 0.0

    def summary(self) -> str:
        if self.error:
            return 'notes: unavailable ({})'.format(self.error)
        line = 'notes: {} filled, {} cached, {} rejected of {} asked'.format(
            self.filled, self.cached, self.rejected, self.asked
        )
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


# The prompt decides what the model answers, so a cached note is only good for
# the prompt that produced it. Keying on it means editing PROMPT invalidates the
# cache by itself; without this a prompt change was invisible to the cache, and
# `--no-cache` did not help — it never writes, so the stale entries survived on
# disk for the next run without the flag to serve back.
_PROMPT_DIGEST = hashlib.sha256(PROMPT.encode('utf-8')).hexdigest()[:8]


def _key(model: str, source: str) -> str:
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]
    return '{}:{}:{}:{}'.format(model, MAX_NOTE_CHARS, _PROMPT_DIGEST, digest)


def why_rejected(raw: str, n_loops: Optional[int] = None) -> Optional[str]:
    """Name the rule a response broke, or None if it passed.

    Split out from `clean` so the CLI can say *which* rule is doing the
    rejecting. Whether a 29% keep rate means the model is weak or the length
    cap is mean is not a thing to have an opinion about when it can be counted.
    """
    text = _trim(_reduce(raw))
    if not text:
        return 'empty'
    if len(text) > MAX_NOTE_CHARS:
        return 'too long'
    if len(text.split()) < 3 or len(text) < MIN_NOTE_CHARS:
        return 'too short'
    if n_loops == 0 and _LOOP_CLAIM.search(text):
        return 'invented a loop'
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


def _trim(text: str) -> str:
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
    if len(text) <= MAX_NOTE_CHARS:
        return text
    clauses = text.split(', ')
    kept: list = []
    for clause in clauses:
        candidate = ', '.join(kept + [clause])
        if len(candidate) > MAX_NOTE_CHARS:
            break
        kept.append(clause)
    # A single clause over the cap is a run-on, not a list, and there is no
    # honest place to cut it.
    return ', '.join(kept)


def clean(raw: str, n_loops: Optional[int] = None) -> Optional[str]:
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
    if why_rejected(raw, n_loops) is not None:
        return None
    text = _trim(_reduce(raw))
    return text[0].lower() + text[1:] if text[:1].isupper() else text


def _ask(host: str, model: str, source: str, timeout: float) -> Optional[str]:
    """One blocking call to Ollama's generate endpoint."""
    payload = json.dumps(
        {
            'model': model,
            'prompt': PROMPT.format(source=source, limit=MAX_NOTE_CHARS),
            'stream': False,
            'options': {
                # Low temperature: this is description, not composition, and a
                # map that changes wording between runs is a map nobody trusts.
                'temperature': 0.1,
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


def fill(
    funcs: Sequence[Func],
    model: str,
    host: str = DEFAULT_HOST,
    limit: int = DEFAULT_LIMIT,
    timeout: float = 60.0,
    use_cache: bool = True,
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
    for func in chosen:
        source = _source_of(func)
        if source is None:
            continue
        report.asked += 1
        key = _key(model, source)
        if use_cache and key in cache:
            func.note = cache[key]
            report.cached += 1
            continue
        try:
            raw = _ask(host, model, source, timeout)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            # One failure means the server is gone or the model is not pulled.
            # Both make every remaining call fail the same way, so stop rather
            # than wait out the timeout once per function.
            report.error = str(getattr(exc, 'reason', None) or exc)
            break
        reason = why_rejected(raw or '', n_loops=func.n_loops)
        if reason is not None:
            report.rejected += 1
            report.reasons[reason] = report.reasons.get(reason, 0) + 1
            continue
        note = clean(raw or '', n_loops=func.n_loops)
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
