"""Output conventions: the shape a code map is required to have."""

from __future__ import annotations

import re

from recce.render import LEGEND

SAMPLE = {
    'a.py': '''
    """Render a report."""

    import argparse
    from pathlib import Path


    def main(argv=None):
        args = _args(argv)
        rows = load(Path(args.src))
        if not rows:
            return 1
        for row in rows:
            emit(row)
        return 0


    def _args(argv):
        return argparse.ArgumentParser().parse_args(argv)


    def load(path):
        text = path.read_text()
        out = []
        for line in text.splitlines():
            if line:
                out.append(line)
        return out


    def emit(row):
        print(row)
    ''',
}


def test_prose_is_outside_the_fence_and_only_trees_are_inside(build):
    """What makes a saved map navigable by heading jumps rather than opaque."""
    _, _, _, text = build(SAMPLE, 'a.py')
    lines = text.splitlines()
    assert lines[0].startswith('# ')
    fences = [i for i, line in enumerate(lines) if line == '```']
    assert len(fences) % 2 == 0 and fences
    inside = set()
    for start, end in zip(fences[::2], fences[1::2]):
        inside.update(range(start, end + 1))
    for index, line in enumerate(lines):
        if line.startswith(('#', '- ')):
            assert index not in inside, line


def test_external_calls_are_bracketed(build):
    _, _, _, text = build(SAMPLE, 'a.py')
    assert re.search(r'\[argparse\]|\[pathlib\]', text)


def test_the_legend_is_the_last_line(build):
    _, _, _, text = build(SAMPLE, 'a.py')
    assert text.rstrip().endswith(LEGEND)


def test_data_shapes_are_a_bullet_list_under_a_heading(build):
    _, _, _, text = build(
        {'a.py': '"""P."""\n\n\ndef record(m):\n    return {"a": 1, "b": 2}\n'}, 'a.py'
    )
    assert '## Data' in text
    assert '- `record()` — returns `{a, b}`' in text


def test_signatures_collapse_past_two_parameters(build):
    _, _, _, text = build(
        {
            'a.py': '"""P."""\n\n\ndef go(alpha, beta, gamma):\n    for x in alpha:\n        print(x)\n    return beta\n'
        },
        'a.py',
    )
    assert 'go(...)' in text
    assert 'go(alpha, beta, gamma)' not in text


def test_return_annotations_ride_on_the_edge(build):
    _, _, _, text = build(
        {
            'a.py': '"""P."""\n\nfrom typing import Optional\n\n\ndef go(x) -> Optional[int]:\n    if x:\n        return 1\n    return None\n'
        },
        'a.py',
    )
    assert '─→ int?' in text


def test_scalar_constants_do_not_crowd_out_real_shapes(build):
    """`NOT_APPLICABLE — str` costs a line to say nothing.

    Found on `xray-analysis`, where five of ten data bullets were bare scalars
    and the record types they displaced were the ones worth naming.
    """
    _, _, _, text = build(
        {
            'a.py': '"""P."""\n\nimport re\n\nNOT_APPLICABLE = "n/a"\nROUNDS = 3\n'
            'PATTERN = re.compile("x")\nALIASES = {"a": "b"}\n\n\n'
            'def go(rows):\n    for r in rows:\n        if r:\n            print(r)\n',
        },
        'a.py',
    )
    data = [line for line in text.splitlines() if line.startswith('- `')]
    assert any('PATTERN' in line for line in data)
    assert any('ALIASES' in line for line in data)
    assert not any('NOT_APPLICABLE' in line for line in data)
    assert not any('ROUNDS' in line for line in data)


def test_scalars_return_when_there_is_nothing_better(build):
    """A module whose only names are strings still gets them listed."""
    _, _, _, text = build(
        {
            'a.py': '"""P."""\n\nHOST = "example"\n\n\n'
            'def go(rows):\n    for r in rows:\n        if r:\n            print(r)\n',
        },
        'a.py',
    )
    assert '- `HOST` — str' in text
