# CLAUDE.md

Guidance for Claude Code working in this repository. The README is for people
using recce; this is for whoever changes it.

## Commands

```sh
./check                      # ruff, then pytest on 3.11, then pytest on 3.14
uv run pytest -q             # the floor only, when iterating
uv run pytest -q -k <name>   # one test
uv sync                      # build .venv from the lock

./corpus /tmp/before         # map 18 real codebases, ~22s
# ...make the change...
./corpus /tmp/after && diff -ru /tmp/before /tmp/after
./corpus-probe <name>=<path> ...   # characterise a codebase on recce's axes
```

**`./check` says whether the code runs. `./corpus` says whether the map got
better, and it is the one that finds things.** Anything touching `rank.py`
wants both. See Testing below for what the two output directories mean and the
one rule that keeps the second tier worth having.

The recipe the flags were added for, when you want a map to work from rather
than a screen to read:

```sh
python3 -m recce --draft --model qwen3.8:27b-mlx pkg/ -o code-map.md
```

`--draft` is only `--max-lines 200 --notes-limit 40`, and either given
explicitly still wins.

How long that takes is a fact about the machine serving the model, not about
recce, and the spread is wide enough to matter: forty notes over `rich` took
2m06s from `qwen2.5-coder:7b` on this laptop, about thirteen minutes from
`qwen3.8:27b-mlx` on the same laptop, and **eleven seconds** from
`qwen3.6:35b` on a box with 96GB of VRAM. Any reasoning that starts "notes are
expensive, so..." is reasoning about the laptop.

```sh
ssh -N -L 11435:127.0.0.1:11434 dev104 &   # its Ollama binds localhost only
python3 -m recce --draft --ollama-host http://127.0.0.1:11435 \
  --model qwen3.6:35b-a3b-coding-mtp-q4_K_M pkg/ -o code-map.md
```

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
  not the same artifact.

  Where the budget stops binding is a property of the codebase, not a number
  you can learn once. On `requests` it is 120 — the map at 200 is byte
  identical. On `rich` it is 200. `--draft` was set to 120 off the `requests`
  measurement alone and under-served a 100-module tree for it, which is why it
  is 200 now: the default has to clear the larger case, and clearing it costs
  the smaller one nothing. The general point survives the fix — a change to
  `_CONCESSION_ORDER` is invisible above the knee of whatever tree you happen
  to test on, so check it at the default, and on something big.

  Above that knee a second ceiling takes over that no budget can lift: `rich`
  renders 8 of 100 modules, so at most 21 of 40 candidates were ever on the
  page. That is what `rendered_funcs` and the `rendered` argument to
  `notes.candidates` are for.

  The budget is now enforced rather than aimed at, and it was not before. Every
  concession on the ladder trades away depth and none of them narrows a tree,
  so the wide trees reached the bottom intact and `_fit` shipped them: twelve
  blocks over budget across the corpus, the worst 114 rows against 40. It held
  everywhere except where it was binding. `_truncate` is the floor under that —
  it cuts into the last subtree that will not fit and says how many went, so a
  block is never longer than its budget and never silently shorter than its
  content. A spanning block is the one exception and buys its extra length in
  block slots, so the document total is unchanged.
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
| `_select_modules` | tests taking block slots from source |
| `_block_coverage` | a block led by its branchiest private helper |
| `_reach_sets` in `_entry_points` | uncalled utilities ranking as the way in |
| `declared_ways_in` | a guessed flow leading the whole document |
| `_CONCESSION_ORDER` | the wrong thing given up first |
| `_truncate` | a block quietly running past its budget |

Two of these answer questions that sound like one question and are not.
`_block_coverage` asks what the rest of a block hangs off; `_score` asks which
function holds the most decisions. They disagree often enough to need separate
markers — `★` for the first, `◆` for the second — and using reach for both was
tried and broke `_presentation_factor`, since `format_summary` reaches
`_human_bytes` and so outranks `summarize`.

## Testing

Three layers, and the third is the one that finds things.

1. **Unit tests** over `extract`, `graph`, `rank`, `render`, `notes`.
2. **`tests/test_skill_checklist.py`** is the acceptance suite. It runs the
   three fixtures shipped with the `code-map` skill at
   `~/.claude/skills/code-map/testing/fixtures` and asserts that skill's own
   verification checklist item by item. The tests skip when the skill is not
   checked out.

   It is a contract, not a preference: recce is a stand-in for that skill, so
   the marker vocabulary and the section headings are the skill's to set. That
   is why `## Spine to read first` kept its name when its contents changed to
   the starred rows, even though the heading now describes them less well than
   `Densest logic` would have.

   Its authority stops at scale. Three fixtures never run out of room, so by
   this file's own argument the checklist cannot answer a question about what to
   cut when the budget binds. It was allowed to settle whether `collections`
   counts as plumbing, which it cannot; the corpus was asked afterwards and
   happened to agree. Use it for vocabulary and shape, not for judgement calls
   about what fits.
3. **`./corpus`.** Eighteen real codebases mapped before a change and after, in
   about twenty seconds. This is not optional politeness — it is what actually
   catches things, and a green suite has repeatedly proved nothing.

```sh
./corpus /tmp/before
# ...make the change...
./corpus /tmp/after && diff -ru /tmp/before /tmp/after
```

It writes two directories, and they answer different questions.

`regression/` is the set the constants were tuned against — httpx produced
`_constructor_factor`, flask produced the two-`app.py` case and the shim
`_pick_spine` skips, requests and rich set the budget. Diffing it answers "did
I break a case someone already paid for", which is all a circular set can
answer.

`heldout/` was chosen by measuring the axes recce turns on, with `corpus-probe`,
to be different from the first tier, and **nothing may be tuned against it.**
Generating a baseline is fine. Reading the diff once, as a check, is fine.
Iterating on a constant until the held-out diff looks right is what destroys
it — at that point it belongs in `regression/` and a new held-out set is
needed. A change that looks right on one and wrong on the other is overfitting,
caught.

It earns this on the first run and kept earning it. From the tuned set alone the
obvious reading was that map length is independent of codebase size: every
target lands between 274 and 307 lines. The held-out tier spans 198 to 569. It
went on to catch that `_fit` shipped over budget on wide trees, and that reach
cannot tell an application from a library.

Two things the diff will show that are not your change. recce maps itself, so
editing `recce/` moves `recce.default.md` and `recce-repo.default.md` — expect
two files and check the diff is your own functions appearing. And `loc` counts
docstrings, so documenting a one-line helper can push it past `_TRIVIAL_LOC` and
promote it from a collapsed `…` row to a row of its own.

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

## Measured, and rejected

Five rules that read as obviously right, were built, and lost to the corpus.
Each is recorded with its numbers beside the constant it would have governed,
because the reasoning that produced them is sound and will occur to someone
again. Before proposing any of these, read why it failed.

| Idea | Where recorded | Why it lost |
|---|---|---|
| Rank externals by ubiquity, not a list | `graph._NOISE_LABELS` | kept and dropped sets are statistically identical; the most widespread calls are `pytest.raises` and `blib2to3.Leaf` |
| Cap a repeated external per block | `graph._NOISE_LABELS` | removes 16% and takes `console.Console` and `is_lpar_token` first |
| Fold cross-module trivia into `…` | `rank._build_tree` | saves four rows corpus-wide, and a row naming another file is the one thing a per-module block cannot otherwise say |
| Reach as an app/library classifier | `corpus` header | detects small and statically legible; ranks `requests` above `httpie`, `flake8` and `poetry` |
| Cap children per node | `rank._CONCESSION_ORDER` | 14 omission markers become 64, for fewer functions shown than dropping repeats alone |

The pattern worth internalising: each was a property of the *code* offered as a
proxy for a property of the *map*. The ones that survived are measured on the
map — what a row costs, and what appears in the space it frees.

There is a sixth, which is not a rule but a habit. A theory written after the
choosing is not the rule that did the choosing. The external noise list was
first justified as "pure operations on values the caller already has", which
does not survive its own entries: `Counter()` and `Path()` are pure
constructions and both stay. What justifies it is the trade — remove a
candidate, regenerate, read what takes the freed space — and that is also the
admission test for adding to it.

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
- **Timeouts and 5xx are per note; connection errors are per run.** `fill`
  used to treat every failure alike and stop on the first, which was right when
  a note cost a second. It made `--draft` return nothing at all against a 27B
  twice, for two different reasons. First a timeout: `qwen3.8:27b` answers a
  93-line function in 59.8 seconds, so the old 60-second default meant one slow
  function abandoned every note after it. Then, once that was fixed, an HTTP
  500 — `mlx runner failed: panic: [METAL] Insufficient Memory` on `rich`'s
  296-line `traverse`, which scores highest and so is asked first, losing all
  40. Both now cost one note; `_MAX_CONSECUTIVE_FAILURES` in a row is read as a
  wedged server. Count consecutively, never cumulatively — two awkward
  functions in a run of forty are not a dead Ollama.
- **A function too long to ask about is skipped, and the limit is the
  machine's, not recce's.** `MAX_SOURCE_CHARS` is the OOM guard above. Bound it
  in characters, not `loc` — a 135-line function measured 6084 characters while
  a 137-line one measured 5376, so lines do not predict the wall. `candidates`
  is oversampled by `_OVERSAMPLE` so a skipped function costs its own slot
  rather than costing the run a note. It was also defended here on the grounds
  that nothing 6000 characters long compresses into ninety, and that was too
  strong: asked about rich's 296-line `traverse`, qwen3.6 on a 96GB card
  answered "recurses on objects, stops at max depth or cycles" in six tenths of
  a second. Thin, but better than the silence the cap imposes — hence
  `--max-source-chars` rather than a bigger constant.
- **`--out` refuses to overwrite.** The check sits beside the target check
  rather than at the write, and it must stay there: a `--draft` run against a
  27B spends a quarter of an hour in the model before there is a byte to write,
  and refusing after that wait is worse than not refusing. There is a test that
  fails if it drifts down to the write.
- **`ruff format` runs before you patch by string match.** It rewrites slices
  like `chosen[: 1]` to `chosen[:1]`, which silently breaks a scripted
  `str.replace` against text you read earlier. It also rewraps a signature that
  has just lost an argument, so an edit written against what you last read can
  stop matching for a reason nothing in your change explains.
- **Whether a package is an application cannot be read off it, and `--type` is
  why.** Reach from the best entry point looks like a clean separator on two
  applications and inverts on nine: cookiecutter 68%, pre-commit 38% and black
  35% against flake8 13%, httpie 10%, pelican 4%, sphinx and mkdocs 3%, poetry
  2% — all below `requests` at 16%. It measures how statically legible a
  codebase is, confounded with size. Declared console scripts do no better,
  since flask, httpx and luigi ship a CLI on the side. Applications that
  dispatch through a plugin registry are precisely the ones recce refuses to
  guess about, so no graph measure can see past the registry. `--type app` is
  the reader supplying what the tree cannot say, and the spanning block builds
  on `[project.scripts]` alone without it.
- **The legend is outside the budget.** `max_lines` is enforced through
  `Node.line_count()`, which counts tree rows; the legend, the data section, the
  split note and the incomplete banner are all document prose. A longer legend
  costs document length and never map rows — measured, sixteen of eighteen
  targets' fenced trees were byte-identical across the change that tripled it.
- **`uv run` uses `.venv` at the floor**, but a bare `python3 -m recce` uses
  3.14. Output can differ between them, and that is the tool working as
  designed rather than a bug.
