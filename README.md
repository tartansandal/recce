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

recce is stdlib-only and imports nothing you have to install, so the zero-install
path is the real one:

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
recce pkg/ -o /tmp/map.md                 # save it
recce pkg/ --stats                        # what it parsed, to stderr
recce pkg/ --json                         # the intermediate state, not the map
```

## What it is for

Orienting before a deep read. The output is a map, not a reference — the value
is in what it leaves out, and it will not tell you what the code *means*.

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
- non-Python code, and Python that a 3.9 `ast` cannot parse — mapping a
  codebase that uses `match` needs a newer interpreter to run recce, even
  though recce itself stays 3.9-compatible

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
| render | `render.py` | format as markdown |

`rank.py` is where the arguing is worth doing. It decides what counts as a
trivial helper, what earns a star, and when a map splits, and those three
choices are most of what separates this from a graph dump.

## Notes, from a local model

Pass `--model` and recce will ask a local Ollama for the one thing `ast` cannot
tell it: what a function's loops and branches actually do.

```sh
ollama pull qwen2.5-coder:7b
recce pkg/ --model qwen2.5-coder:7b
```

```
main(argv)  ★
 loops over lines, filters valid parsed lines, summarizes, prints
 ├─ summarize(records)  ★
 │   loops over records, accumulates counts and bytes by status and route
```

It is off by default and it fails soft. No Ollama, a timeout, a model that is
not pulled — all of them leave the notes empty and the map renders exactly as
it does without one.

The model is shown one function body and asked for one line. It never sees the
tree, the markers, the budget, or the markdown, so there is no convention for
it to break and no global reasoning for it to get wrong. That narrowing is why
a 7B is a plausible tool here and would not be if it were writing the document.

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

## Tests

```sh
uv sync
uv run pytest -q
```

`tests/test_skill_checklist.py` is the acceptance suite: it runs recce against
the three fixtures shipped with the `code-map` skill and asserts that skill's
own verification checklist, item by item. Those tests skip when the skill is
not checked out on this machine.
