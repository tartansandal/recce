# CLAUDE.md

Guidance for Claude Code working in this repository. The README is for people
using recce; this is for whoever changes it.

## Commands

```sh
./check                      # ruff, then pytest on 3.11, then pytest on 3.14
uv run pytest -q             # the floor only, when iterating
uv run pytest -q -k <name>   # one test
uv sync                      # build .venv from the lock
```

The recipe the flags were added for, when you want a map to work from rather
than a screen to read:

```sh
python3 -m recce --draft --model qwen3.8:27b-mlx pkg/ -o code-map.md
```

Roughly a quarter of an hour for forty notes, cached after. `--draft` is only
`--max-lines 120 --notes-limit 40`, and either given explicitly still wins.

**Run `./check`, not `uv run pytest`, before calling anything done.** Two
interpreters are needed and neither run is redundant. The floor catches syntax
too new to compile. The ceiling catches `ast` API that has since been removed —
`ast.Ellipsis` went in 3.12, passed the entire suite on the floor, and crashed
on every codebase using `Callable[..., X]`.

**Pass `--isolated` to any other `uv run --python`.** Without it, `uv run
--python 3.14` rebuilds `.venv` on 3.14 and leaves it there, so every later
floor run silently tests the wrong thing.

## What must not regress

Four rules carry the design. Each exists because the alternative was tried or
because breaking it makes the map actively misleading rather than merely worse.

- **A call recce cannot explain is dropped, never guessed.** Resolution goes
  through the import table, the module's own names, and the class MRO; anything
  else — `getattr` dispatch, handler dicts, callbacks — produces no edge. A
  missing edge costs the reader a `grep`. An invented one sends them somewhere
  the code never goes, and the map gives them no way to tell the two apart.
- **The purpose line comes from three sources and nowhere else**: module
  docstring, a README in that exact directory, a top-of-file comment. Where all
  three are silent there is no purpose line. It is the first thing read and the
  reader cannot tell a guessed one from a real one. Note the asymmetry with
  `pyproject.toml`, which *is* searched upward — a README above a package
  describes the project the package sits in, but a manifest above a package
  describes that package.
- **The line budget is real.** A map that quietly runs to three screens has
  failed at its only job. `_CONCESSION_ORDER` in `rank.py` spells out the
  order of concessions as a list rather than burying it in the nesting of
  loops, so the priority is something you can read and argue with — reorder
  those lines and you have changed what recce gives up first. Notes go before
  rows, because a hidden row is a call the reader never learns about while a
  dropped note only costs a sentence they can get by opening the file.

  `--draft` does not break this rule, it steps outside the case the rule is
  about: a map read once on screen and a map saved and annotated for days are
  not the same artifact. Worth knowing before you touch `_CONCESSION_ORDER`,
  though — under a loose budget it never fires at all. On `requests` the map at
  `--max-lines 200` is byte-identical to the one at 120, because by 120 there
  is nothing left to concede. Any change to the order is therefore invisible in
  draft mode and only observable at the default, which is the opposite of where
  you would think to look for it.
- **Model output is checked against the syntax tree.** `notes.py` knows from
  `n_loops` whether a function actually loops, so a note claiming one where
  none exists is not a judgement call but a falsified statement, and it is
  dropped. This is not hypothetical: `qwen2.5-coder:7b`, shown a body that is a
  regex match and an early return, answered *"loops over characters in line"*.

## Where the judgement lives

`rank.py` is the product. Producing a call graph is mechanical and a tool that
dumps all of it is no better than `pyan`; the filtering is what makes a map.
Argue with these rather than around them, and read the docstring before
changing a constant — each one is a defence against a specific observed
failure, named in the docstring.

| Knob | Defends against |
|---|---|
| `_is_trivial` | one-line helpers taking a row from the spine |
| `_presentation_factor` | report writers scoring like aggregators |
| `_constructor_factor` | `__init__` argument-wrangling scoring like logic |
| `_effective_branches` | ternary defaulting counting as real decisions |
| `_module_roots` promotion | class-heavy modules showing only constructors |
| `_select_modules` | tests taking half the blocks |

## Testing

Three layers, and the third is the one that finds things.

1. **Unit tests** over `extract`, `graph`, `rank`, `render`, `notes`.
2. **`tests/test_skill_checklist.py`** is the acceptance suite. It runs the
   three fixtures shipped with the `code-map` skill at
   `~/.claude/skills/code-map/testing/fixtures` and asserts that skill's own
   verification checklist item by item. Those are the closest thing to a ground
   truth recce has. The tests skip when the skill is not checked out.
3. **Diffing real repositories.** Keep a map of a few public codebases before a
   change and diff after. This is not optional politeness — it is what actually
   catches things, and a green suite has repeatedly proved nothing.

```sh
git clone --depth 1 https://github.com/psf/requests
python3 -m recce requests/src/requests > /tmp/base.md
# ...make the change...
python3 -m recce requests/src/requests > /tmp/after.md && diff /tmp/base.md /tmp/after.md
```

Judging a model needs its own arrangement, because the rendered map is the
wrong instrument: notes are the first concession, so at the default budget most
of what a model wrote never reaches the page — 12 asked and 1 rendered on
`requests`. Compare models by calling `notes.candidates` and `notes._ask`
directly over the same candidate set and reading the sentences, and keep the
keep rate in perspective. It has stopped discriminating: both a 7B and a 27B
keep twelve of twelve on recce and on `requests`, and the whole difference
between them is in whether the sentence was worth its line.

Every bug that mattered needed a codebase big enough to run out of room. On a
three-function fixture there is always space for everything, so nothing has to
be chosen between — and choosing is the whole product. Fixtures cannot catch a
scale bug by construction.

The sharpest example: a pure caching change made four maps differ and started
rendering `HTTPBasicAuth.__init__` twice. The cache was not the cause. requests
declares that constructor three times — two `@overload` stubs and the body —
and all three were being emitted with the same `node_id`, so the index and the
renderer had disagreed about what existed for as long as the code had run. It
only surfaced because the change shifted the score population enough that both
copies fit the budget. Ninety-nine passing tests never saw it.

## Gotchas

- **`python3` on this machine is Homebrew's 3.14**, not `/usr/bin/python3`
  (3.9.6). `brew install ollama` pulls `python@3.14` as a dependency and it
  shadows the system one. Which interpreter runs recce decides what syntax it
  can read, so this matters more here than in most projects.
- **`RECCE_MODEL=auto` turns notes on for every run in that shell**, which is
  worth knowing when a map you did not expect to be annotated is, or when a
  run takes twenty seconds instead of one. `--model` with no value does the
  same for a single run; both resolve to a concrete model name before anything
  is asked, because the cache keys on it.
- **Notes are cached** under `$XDG_CACHE_HOME/recce`, so a rerun will not
  re-ask the model. The key covers the model name, `MAX_NOTE_CHARS`, the
  `PROMPT` text and the function source, so changing any of them invalidates
  itself — you do not need `--no-cache` for that. What `--no-cache` does is
  skip the cache in *both* directions: it never writes, so a run with it leaves
  the previous entries on disk for the next run without it to serve back.
- **A thinking model returns nothing unless told not to think.** Every Qwen
  from 3.6 on reasons by default and puts the reasoning first, so with
  `num_predict` at 60 the whole budget goes to the thinking and `response`
  comes back empty — `done_reason` is `length`, `_reduce` takes the first line
  of nothing, and every note is rejected as `empty`. The report then reads as a
  verdict on the model. `_ask` sends `think: false` unconditionally; Ollama
  ignores the field on models that cannot think, verified against
  `qwen2.5-coder:7b`. Do not gate it on a list of which models reason, and note
  that 3.6 dropped the `/no_think` prompt switch, so the field is the only way
  left to say it.
- **Timeouts are per note, connection errors are per run.** `fill` used to
  treat them alike and stop on the first failure, which was right when a note
  cost a second. It is not: `qwen3.8:27b` answers a 93-line function in 59.8
  seconds, so under the old 60-second default one slow function abandoned every
  note after it, and `--draft` produced nothing at all against the model it
  exists for. A timeout now costs its own note; `_MAX_CONSECUTIVE_TIMEOUTS` in
  a row is read as a wedged server. Count consecutively, never cumulatively —
  two slow functions in a forty-function run are not a dead Ollama.
- **`--out` refuses to overwrite.** The check sits beside the target check
  rather than at the write, and it must stay there: a `--draft` run against a
  27B spends a quarter of an hour in the model before there is a byte to write,
  and refusing after that wait is worse than not refusing. There is a test that
  fails if it drifts down to the write.
- **`ruff format` runs before you patch by string match.** It rewrites slices
  like `chosen[: 1]` to `chosen[:1]`, which silently breaks a scripted
  `str.replace` against text you read earlier.
- **`uv run` uses `.venv` at the floor**, but a bare `python3 -m recce` uses
  3.14. Output can differ between them, and that is the tool working as
  designed rather than a bug.
