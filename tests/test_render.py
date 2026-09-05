"""Output conventions: the shape a code map is required to have."""

from __future__ import annotations

import re

from recce.render import LEGEND_HEADING

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
    # strict: the assertion above already guarantees the pairing, and a fence
    # count that goes odd should fail loudly rather than silently drop the last.
    for start, end in zip(fences[::2], fences[1::2], strict=True):
        inside.update(range(start, end + 1))
    for index, line in enumerate(lines):
        if line.startswith(('#', '- ')):
            assert index not in inside, line


def test_external_calls_are_bracketed(build):
    _, _, _, text = build(SAMPLE, 'a.py')
    assert re.search(r'\[argparse\]|\[pathlib\]', text)


def test_the_legend_closes_the_document(build):
    _, _, _, text = build(SAMPLE, 'a.py')
    body, _, legend = text.rstrip().partition(LEGEND_HEADING)
    assert legend, text
    assert LEGEND_HEADING not in body
    assert all(
        line.startswith('- ') for line in legend.strip().splitlines() if line.strip()
    )


def test_the_legend_names_only_the_marks_on_the_page(build):
    """A legend listing marks the map does not use is reference, not a key.

    The one-line legend it replaced had the opposite fault: it named the three
    rarest marks and left out the return arrow, which is the commonest thing in
    any recce map.
    """
    annotated = dict(SAMPLE)
    annotated['b.py'] = (
        '"""Q."""\n\n\ndef sized(xs) -> int:\n    total = 0\n'
        '    for x in xs:\n        if x:\n            total += 1\n    return total\n'
    )
    for files in (SAMPLE, annotated):
        _, _, _, text = build(files, '.')
        trees = '\n'.join(re.findall(r'```\n(.*?)```', text, re.S))
        legend = text.partition(LEGEND_HEADING)[2]
        for mark, entry in (
            ('─→', '`─→` returns'),
            ('◆', 'densest logic'),
            ('↑', 'shown above'),
            ('…', '`…` more'),
        ):
            assert (entry in legend) == (mark in trees), (mark, entry)


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


def test_a_map_says_so_when_files_would_not_parse(build):
    """The warning went to stderr, where a saved map cannot carry it.

    A map of a package where a third of the files failed still looks like a
    map, and nothing in the document admits what is missing.
    """
    _, _, _, text = build(
        {
            'good.py': '"""P."""\n\n\ndef go(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
            'bad.py': 'def (:\n',
        },
        '.',
    )
    assert 'Incomplete' in text
    assert 'bad.py' in text
    assert '1 of 2 files' in text


def test_a_clean_map_carries_no_warning(build):
    _, _, _, text = build({'a.py': '"""P."""\n\n\ndef go():\n    pass\n'}, 'a.py')
    assert 'Incomplete' not in text
