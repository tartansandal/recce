"""Filtering: the judgements that make a map a map rather than a listing."""

from __future__ import annotations

from recce.model import SPINE, TRIVIAL


def role_of(project, module, name):
    return next(f for f in project.modules[module].funcs if f.name == name).role


LOG_PARSER = {
    'a.py': '''
    """Parse and summarize webserver access logs."""

    import argparse
    from collections import Counter


    def main(argv=None):
        args = _parse_args(argv)
        records = [parse_line(l) for l in args.lines]
        return format_summary(summarize(records))


    def _parse_args(argv):
        p = argparse.ArgumentParser()
        p.add_argument("lines")
        return p.parse_args(argv)


    def parse_line(line):
        parts = line.split()
        if not parts:
            return None
        return {"ip": parts[0], "status": int(parts[1]), "bytes": _parse_bytes(parts[2])}


    def _parse_bytes(value):
        return 0 if value == "-" else int(value)


    def summarize(records):
        counts = Counter()
        total = 0
        for r in records:
            counts[r["status"] // 100] += 1
            total += r["bytes"]
        return {"counts": counts, "total": total, "n": len(records)}


    def format_summary(s):
        lines = ["Total requests: %d" % s["n"]]
        lines.append("Total bytes: %s" % _human_bytes(s["total"]))
        for k, v in sorted(s["counts"].items()):
            lines.append("  %dxx: %d" % (k, v))
        return "\\n".join(lines)


    def _human_bytes(n):
        for unit in ("B", "KB", "MB"):
            if n < 1024:
                return "%.1f%s" % (n, unit)
            n /= 1024
        return "%.1fGB" % n
    ''',
}


def test_small_private_leaf_helpers_are_collapsed(build):
    """Even the ones with a loop in them.

    An earlier rule required zero branches and let `_human_bytes` through — a
    six-line unit formatter whose `for` over four suffixes counted the same as
    a `for` over a million records.
    """
    project, _, _, text = build(LOG_PARSER, 'a.py')
    for name in ('_parse_args', '_parse_bytes', '_human_bytes'):
        assert role_of(project, 'a', name) == TRIVIAL, name
        assert '\n ├─ {}('.format(name) not in text
        assert '\n └─ {}('.format(name) not in text
    assert '…' in text


def test_visibility_only_decides_the_borderline_sizes(build):
    """Public is not a shield, but it does raise the bar.

    The skill collapses one-line helpers without asking who can call them, so
    a public one-liner goes too. What public buys is the middle ground: at
    five lines with a branch, a private helper is still noise and a public
    function is somebody's interface.
    """
    project, _, _, _ = build(
        {
            'a.py': (
                'def slug(s):\n    return s.lower()\n\n\n'
                'def widen(s):\n    out = s\n    if not out:\n        out = "?"\n    return out.upper()\n\n\n'
                'def _widen(s):\n    out = s\n    if not out:\n        out = "?"\n    return out.upper()\n\n\n'
                'def go():\n    return slug("A") + widen("b") + _widen("c")\n'
            )
        },
        'a.py',
    )
    assert role_of(project, 'a', 'slug') == TRIVIAL
    assert role_of(project, 'a', 'widen') != TRIVIAL
    assert role_of(project, 'a', '_widen') == TRIVIAL


def test_the_spine_is_the_logic_not_the_formatter(build):
    """Output builders branch as much as real logic; the map must tell them apart."""
    project, _, mapping, _ = build(LOG_PARSER, 'a.py')
    starred = {f.name for f in project.funcs() if f.role == SPINE}
    assert 'main' in starred
    assert 'summarize' in starred
    assert 'format_summary' not in starred


def test_every_map_stars_something(build):
    project, _, _, text = build({'a.py': 'def go():\n    return 1\n'}, 'a.py')
    assert '★' in text


def test_a_three_module_package_splits_by_module_leaves_first(build):
    _, _, mapping, text = build(
        {
            'pkg/__init__.py': '',
            'pkg/parser.py': '"""Parse a line."""\n\n\ndef parse(line):\n    return line.split()\n',
            'pkg/summary.py': '"""Aggregate records."""\n\n\ndef summarize(rs):\n    return len(rs)\n',
            'pkg/cli.py': '"""Entry point."""\n\nfrom .parser import parse\nfrom .summary import summarize\n\n\ndef main(argv):\n    return summarize([parse(l) for l in argv])\n',
        },
        'pkg',
    )
    assert mapping.strategy == 'module'
    assert 'Splitting by module for fit' in text
    titles = [b.title for b in mapping.blocks]
    assert titles.index('cli.py') > titles.index('parser.py')
    assert '## Spine to read first' in text


def test_a_cross_module_call_is_referenced_not_redrawn(build):
    _, _, _, text = build(
        {
            'pkg/__init__.py': '',
            'pkg/parser.py': '"""Parse."""\n\n\ndef parse(line):\n    return line.split()\n',
            'pkg/summary.py': '"""Sum."""\n\n\ndef summarize(rs):\n    return len(rs)\n',
            'pkg/cli.py': '"""Entry."""\n\nfrom .parser import parse\nfrom .summary import summarize\n\n\ndef main(argv):\n    return summarize([parse(l) for l in argv])\n',
        },
        'pkg',
    )
    assert 'parser.parse()' in text


def test_no_block_exceeds_the_line_budget(build):
    """The budget is the product, so it is asserted rather than aimed at."""
    files = {'pkg/__init__.py': ''}
    for index in range(6):
        body = '\n'.join(
            '    if x == {}:\n        helper_{}(x)'.format(n, n) for n in range(12)
        )
        helpers = '\n\n'.join(
            'def helper_{}(x):\n    for i in range(x):\n        x += i\n    return x'.format(
                n
            )
            for n in range(12)
        )
        files['pkg/m{}.py'.format(index)] = (
            '"""Module {}."""\n\n\ndef entry_{}(x):\n{}\n    return x\n\n\n{}\n'.format(
                index, index, body, helpers
            )
        )
    _, _, mapping, text = build(files, 'pkg', max_lines=40)
    for block in mapping.blocks:
        assert block.line_count() <= 40, block.title
