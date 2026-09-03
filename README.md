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

## Where a local model would go

The static pass deliberately leaves one thing blank: `Func.note`, the one-line
paraphrase of a function's loop and branch shape that the `code-map` skill
writes by hand. That is the piece a model has to do, and `--json` exists to
hand it over — read the functions, fill in the notes, hand it back.

Scoping it that narrowly is the point. A model asked for one line about one
function body, with no formatting rules to obey and no graph to hold in its
head, is a job a small local model does acceptably; the same model asked to
produce this whole document from the source will reproduce every anti-pattern
the filtering exists to prevent.

## Tests

```sh
uv sync
uv run pytest -q
```

`tests/test_skill_checklist.py` is the acceptance suite: it runs recce against
the three fixtures shipped with the `code-map` skill and asserts that skill's
own verification checklist, item by item. Those tests skip when the skill is
not checked out on this machine.
