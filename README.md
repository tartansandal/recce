# recce

A reconnaissance pass over unfamiliar Python. Point it at a file, a package, or
a directory of scripts and it prints a one-screen map: the entry points, who
calls whom, which calls leave the project, and the one to three functions worth
reading first.

It is a scripted stand-in for the `code-map` skill, for code that cannot be
sent to a hosted model.

```
# with_docstring.py — Parse and summarize webserver access logs

main(argv)  ★
 ├─ parse_line(line)  ★
 │   └─ … _parse_bytes
 ├─ summarize(records)  ★
 │   └─ Counter()                           [collections]
 ├─ format_summary(s)
 │   └─ … _human_bytes
 └─ … _parse_args, _read_input              [argparse, pathlib]

★ read first · ~ skim · [brackets] = external
```

## Running it

recce needs Python 3.11 or newer and imports nothing you have to install, so
the zero-install path is the real one:

```sh
python3 -m recce path/to/package          # copy the recce/ directory anywhere
```

Where installing is allowed:

```sh
uv tool install .
recce path/to/package
```

Useful flags:

```sh
recce pkg/ --max-lines 30                 # tighter budget; splits sooner
recce pkg/ -o /tmp/map.md                 # save it; refuses to clobber
recce pkg/ -o /tmp/map.md --force         # overwrite one that is already there
recce pkg/ --stats                        # what it parsed, to stderr
recce pkg/ --json                         # the intermediate state, not the map
recce pkg/ --draft                        # a map to save and annotate, not read once
recce pkg/ --max-source-chars 20000       # ask about long functions too, given the memory
```

`--out` will not write over a file that already exists. That is a small
rudeness when the map is disposable and the only thing standing between you
and a lost afternoon when it is not — see the draft workflow below.

## What it is for

Orienting before a deep read. The output is a map, not a reference — the value
is in what it leaves out, and it will not tell you what the code *means*.

There are two ways that goes, and they want different numbers. Read once on
screen, the budget is the product: a map running to three screens has failed at
its only job, and the default 40 lines a block is tuned for that. Saved to a
file as the starting point for an investigation lasting days — annotated by
hand as you learn the code — it is a document, and a document is not a
screenful. `--draft` is the second case:

```sh
recce pkg/ --draft -o code-map.md         # --max-lines 200, --notes-limit 40
```

Both are still ordinary flags and either one given explicitly still wins, so
`--draft --max-lines 60` means 60.

The number is measured rather than chosen, and it took two codebases to settle.
Notes are the first thing the budget gives up, so a tight one starves them: on
`requests`, 40 requested notes rendered 5 at the default, 21 at 80, and all 40
at 120, with 200 giving a byte-identical map. On `rich` — 100 modules to
`requests`' 19 — 120 renders 23 of 40 and 200 renders all 40. Where the budget
stops binding is a property of the tree being mapped, so the default has to
clear the larger case, and clearing it costs the smaller one nothing.

Since that file is one you will be writing in, `--out` refuses to overwrite an
existing one without `--force`. The command that refreshes a draft and the
command that destroys it are otherwise the same keystrokes, and while the notes
cache brings recce's own sentences back from disk, nothing brings yours back.

A package of more than two modules is drawn as one block per file, which
answers what each file holds and not how they fit together — every call leaving
a module becomes a reference leaf the block cannot follow. So where a project
declares how it is run, in `[project.scripts]`, the map opens with that flow
drawn across the modules it touches:

```
## [1] main() across 6 modules
```

It is bought rather than added: a flow needing three blocks' worth of lines
displaces three per-file blocks, and the modules that lose their place are
counted in the note at the top. A project that does not declare an entry point
gets no such block, because the alternative is picking whichever deep function
happens to touch the most files and calling it the way in.

It is not a call-graph dump. `pyan` and `code2flow` already draw every edge, and
a complete graph of unfamiliar code is as hard to read as the code. recce
filters: trivial helpers collapse into a `…` row, external calls are pushed to
a `[bracket]` at the right margin so the eye can skip them, and the tree is
pruned to a line budget rather than allowed to run to three screens.

## What it will miss

Resolution is static and name-based, and a call it cannot explain is dropped
rather than guessed at — a missing edge costs you a `grep`, an invented one
sends you somewhere the code never goes. So it does not see:

- dynamic dispatch: `getattr`, handler dicts, callbacks passed as arguments
- methods called on objects whose type is not written down
- anything registered by a decorator it does not recognise as an entry point
- methods inherited from a base class outside the project
- functions defined inside an `if` or `try` body: a platform branch or an
  `ImportError` fallback is invisible, so calls to them resolve to nothing
- non-Python code, and any syntax newer than the interpreter running recce

That last one is worth being precise about, because it is not what people
expect. What recce can *read* is decided by the interpreter it runs on, not by
`requires-python` — run it on 3.11 and `match`, `except*` and PEP 695 generics
are all unreadable; run the same code on 3.14 and they are fine. **Run recce on
the newest Python you have**, whatever it was built against. When files do fail
to parse the map says so at the top rather than quietly leaving them out.

A short list of external calls is left out on purpose rather than missed:
`itertools`, `functools`, `operator`, `gettext`, `logging`, `typing`, and the
path arithmetic in `os.path` — `join`, `basename`, `dirname`, `splitext` and
their neighbours. Everything else keeps its bracket, including the rest of
`os`: `os.stat`, `os.remove`, `environ.get`, and `pathlib` in full.

The list is short because it was measured rather than reasoned out. Each entry
is there because removing it made room for something worth more — dropping these
freed 96 rows across a corpus of fifteen codebases, and what moved in included
the whole of `rich`'s traceback rendering. `collections` looks like it belongs
and does not: dropping it costs 44 `Counter` and `defaultdict` rows and buys
back little. Where a call is borderline it keeps its bracket, on the same
grounds as everything else here — a row you never see is a cost you cannot
notice, and a row you did not need is one line.

The purpose line has a rule of its own. It comes from the module docstring, a
README in that exact directory, or a top-of-file comment block — and from
nothing else. Where all three are silent there is no purpose line, because a
guessed one is read first, believed, and unfalsifiable from the map alone.

## How it works

Four passes, each reading only what the one before it wrote:

| Pass | Module | Does |
|---|---|---|
| extract | `extract.py` | parse sources with `ast` into plain records |
| graph | `graph.py` | resolve call sites to targets, or drop them |
| rank | `rank.py` | score, classify, prune to budget, split if needed |
| notes | `notes.py` | optional; ask a local model for branch-shape lines |
| render | `render.py` | format as markdown |

`rank.py` is where the arguing is worth doing. It decides what counts as a
trivial helper, what earns a star, and when a map splits, and those three
choices are most of what separates this from a graph dump.

Speed is not the point but it is worth knowing the shape, since it decides
whether you can point this at something on a whim:

| Target | Size | Time |
|---|---|---|
| a single module | 20-odd functions | instant |
| `requests`, `httpx`, `flask` | 250-450 functions | under 0.1s |
| `rich` | 100 modules, 844 functions | 0.2s |
| the whole standard library | 1868 modules, 55,710 functions | 10s |

Cost is nearly all parsing, so it scales with source read rather than with
anything recce decides. Pointing it at a tree that large is a misuse — you get
eight blocks and a note accounting for the many hundreds of modules that are
not shown — but it will not fall over, and it says what it left out. That note
separates the two reasons a module is missing: source that did not fit, and
test modules, which are not mapped alongside source at all.

## Notes, from a local model

Pass `--model` and recce will ask a local Ollama for the one thing `ast` cannot
tell it: what a function's loops and branches actually do.

```sh
ollama pull qwen2.5-coder:7b
recce pkg/ --model qwen2.5-coder:7b   # a model you name
recce pkg/ --model                    # whichever installed one it finds
```

Given no value, `--model` picks from what Ollama has pulled: a code-trained
family first — `qwen2.5-coder`, `deepseek-coder`, `codellama` and the like, in
that order — and the smallest build of it, because a note is one line and the
7B answers as well as the 32B and answers sooner. Then `qwen3.6`, which is a
general family with coding builds rather than a code-trained one, so it ranks
below all of them and wins only where nothing better is pulled — on a server
holding one 35B coding build and a 1B general model, the alternative was the
1B. Failing every one of those, the smallest model installed that is not an
embedding model.

recce prints which one it chose, since the note cache is keyed on the model
name and a map is only reproducible if you know what wrote it. A model you
pinned yourself is not announced that way — you named it — but `--stats` and
any failure line carry it either way, so a name that is not pulled reports as
`notes: qwen3.6:typo - unavailable (...)` rather than a bare `unavailable`.

Set `RECCE_MODEL=auto` to get that without the flag, or `RECCE_MODEL=<name>` to
pin one:

```sh
export RECCE_MODEL=auto
recce pkg/
```

```
main(argv)  ★
 loops over lines, filters valid parsed lines, summarizes, prints
 ├─ summarize(records)  ★
 │   loops over records, accumulates counts and bytes by status and route
```

It is off unless you ask — by flag or by `RECCE_MODEL` — and it fails soft. No
Ollama, a refused connection, a model that is not pulled: all of them leave the
notes empty and the map renders exactly as it does without one. A timeout is
narrower, because it usually means one function was long rather than that
anything is wrong, so it costs that note alone and the run carries on; only
three in a row are read as a server that has stopped answering.
`--notes-timeout` sets the limit, at 300 seconds by default, which is generous
for a 7B and about right for a 27B.

The model is shown one function body and asked for one line. It never sees the
tree, the markers, the budget, or the markdown, so there is no convention for
it to break and no global reasoning for it to get wrong. That narrowing is why
a 7B is a plausible tool here and would not be if it were writing the document.

### Bigger models, and thinking ones

A 7B is the default because it is enough for one line and answers in about a
second. A larger model is worth it when the map is a draft you will live with
for days rather than a screen you read once — the difference is not in how many
notes survive the checks, which is near all of them either way, but in how many
say something. `qwen2.5-coder:7b` described one function as *"loops over
project modules, functions, and classes, collecting data section bullets"*,
which restates the name; `qwen3.8:27b` described the same one as *"walks
modules for dict returns, dataclasses, enums, constants, scalars if empty,
dedupes"*. Both pass every check. Only one is worth its line.

```sh
recce pkg/ --draft --model qwen3.8:27b-mlx -o code-map.md
```

Expect around 20 seconds a note instead of one, so a 40-note draft is a
quarter of an hour. Against an investigation measured in days that is a good
trade, and it is cached afterwards.

Two things to know before reaching for one:

- **Anything from Qwen 3.6 on thinks by default**, and reasoning is emitted
  before the answer. recce sends `think: false` on every request, so this is
  handled — but it is why it has to. Left on, the token budget for the note is
  spent entirely on reasoning, the response comes back empty, and every note is
  rejected for being blank. It reads exactly like a model that cannot write a
  sentence. Qwen 3.6 also dropped the `/no_think` switch its predecessors took
  in the prompt, so the API field is the only way left to say it.
- **A very long function is skipped rather than asked about**, because a model
  small enough to run locally can die on the prompt: an 18GB model on a 24GB
  machine returned `[METAL] Insufficient Memory` on a 296-line function, and
  since that function scored highest and was asked first, the whole run
  produced nothing. `--max-source-chars` raises the limit, which is worth doing
  when the model is served by something larger — the same function answers in
  under a second on a 96GB card.
- **The length cap is tuned for a terse model.** A denser one writes past it and
  gets trimmed at the last clause that fits: `qwen3.8:27b` answered one function
  in 114 characters, of which 90 survived and *"returns early or builds blocks
  by strategy"* did not. `--note-chars` raises the cap, and since it is part of
  the cache key, changing it re-asks rather than serving back answers written to
  the old one.

### The static pass fact-checks the model

This is the part worth stealing if you build something similar. recce already
knows, from the syntax tree, whether a function contains a loop. So a note
claiming one where none exists is not a judgement call — it is a statement the
parser has already falsified, and it is dropped.

That check is not hypothetical. Asked about a function whose entire body is a
regex match and an early return, `qwen2.5-coder:7b` answered *"loops over
characters in line, branches on match"*: fluent, specific, and describing code
that is not there. A reader cannot catch that from the map, so the map catches
it for them.

Notes are also capped in length rather than asked to be short, cached by
content hash so reruns are free and maps are stable, and counted against the
line budget like any other row.

### How good is it, actually

Measured on recce's own source, 14 functions asked: 13 notes kept, 1 refused
for inventing a loop. Reading those 13 against the code, nine were accurate and
useful, two described recursion or dispatch as looping — weak, not false — and
one was true but said nothing.

The failure mode to watch is not wrongness, it is sameness: the model latches
onto whatever shape the prompt's examples have and writes every note to that
template. Vary the examples if the notes start reading alike.

Two things worth knowing before trusting the numbers. Trimming is what made
them: ten of eleven early rejections were length, not quality, so notes are now
cut at clause boundaries — dropping whole clauses keeps every survivor exactly
as true as it was, which cutting mid-sentence would not. And a bland note is
worse than no note, since it costs a line to say nothing, which is what the
minimum length is defending against.

With trimming in place the keep rate stopped being the interesting number: over
twelve candidates each on recce's own source and on `requests`, both
`qwen2.5-coder:7b` and `qwen3.8:27b` kept all twelve. What separates them is
whether the sentence earns its line, which no counter measures.

One caveat if you care about reproducibility. Temperature is pinned at zero and
`qwen2.5-coder:7b` is byte-identical across runs because of it, but that is a
property of the model rather than of the setting: two identical runs of
`qwen3.8:27b` disagreed on three notes of twelve. The cache hides this in
normal use, since the first answer is the one kept, but a `--no-cache` run or a
changed cache key will reshuffle the wording. It matters if you diff maps; it
does not if you generate one and annotate it.

## Environment

Three variables, all optional:

| Variable | Default | What it does |
|---|---|---|
| `RECCE_MODEL` | unset | `auto` picks an installed model for every run; a model name pins that one. Same as `--model`, without the flag |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | where to look for Ollama. `--ollama-host` overrides it |
| `XDG_CACHE_HOME` | `~/.cache` | notes are remembered in `$XDG_CACHE_HOME/recce/notes.json`. Delete that file to forget them |

`RECCE_MODEL` is the only one that turns anything on. The first two are read
when `recce.notes` is imported, so exporting them mid-session affects the next
run and not a running one; `XDG_CACHE_HOME` is read when a note is cached.
With none of them set, and no `--model`, recce opens no socket and writes no
files.

## Entry points

recce looks for the way in, best evidence first: `[project.scripts]` in a
`pyproject.toml` above the tree, then a `__main__` guard, then a framework
decorator it recognises, then a function called `main`, and only then the shape
of the call graph. The first of those is a declaration and the rest are
inference, which is why it wins — though a console script that is a two-line
shim is followed rather than starred.

## Tests

```sh
uv sync
./check          # ruff, then pytest on the 3.11 floor and again on 3.14
uv run pytest -q # just the floor
```

`./check` runs two interpreters on purpose. The floor catches syntax too new to
compile; the ceiling catches `ast` API that has since been removed — `ast.Ellipsis`
went in 3.12 and crashed on every codebase using `Callable[..., X]`, having
passed the whole suite on the floor.

`tests/test_skill_checklist.py` is the acceptance suite: it runs recce against
the three fixtures shipped with the `code-map` skill and asserts that skill's
own verification checklist, item by item. Those tests skip when the skill is
not checked out on this machine.
