"""recce — a reconnaissance pass over unfamiliar code.

Point it at a file, a package, or a directory of scripts and it produces a
one-screen map: the entry points, who calls whom, which calls leave the
project, and which one to three functions hold the interesting logic.

The pipeline is four passes, each in its own module and each reading only what
the one before it wrote:

    extract  parse sources with `ast` into plain records
    graph    resolve call sites to targets, or drop them
    rank     score, classify, prune to a line budget, split if needed
    render   format the result as markdown

Everything is stdlib-only and nothing imports or executes the code being
mapped, so recce runs against sources it is not allowed to run.
"""

from __future__ import annotations

__version__ = '0.1.0'
