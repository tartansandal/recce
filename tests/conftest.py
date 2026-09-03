"""Shared helpers for building throwaway source trees.

Fixtures are written as string literals and laid down under `tmp_path` rather
than committed as files. A test that shows the code it is about reads in one
pass, and the alternative — a `fixtures/` directory — puts the input three
files away from the assertion about it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from recce.extract import discover
from recce.graph import resolve
from recce.rank import plan
from recce.render import render


def write_tree(root: Path, files: dict) -> Path:
    """Write `{relative path: source}` under `root`, creating directories."""
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip('\n'))
    return root


@pytest.fixture
def build(tmp_path):
    """Run the whole pipeline over a written tree and hand back every stage."""

    def _build(files: dict, target: str = '.', max_lines: int = 40):
        write_tree(tmp_path, files)
        project = discover(tmp_path / target)
        graph = resolve(project)
        mapping = plan(project, graph, max_lines=max_lines)
        text = render(project, mapping, base=str(tmp_path))
        return project, graph, mapping, text

    return _build
