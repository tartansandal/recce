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


def test_a_multi_module_map_never_borrows_one_modules_docstring(build):
    """Provenance holds even when two modules share one unsplit block.

    Below the split threshold a package renders as a single tree with no
    per-module headings, and the whole map's purpose can then only come from a
    README. Taking whichever module the walk happened to reach first would
    caption the package with one file's docstring, and the reader has no way
    to see that is what happened.
    """
    _, _, mapping, text = build(
        {
            'a.py': '"""Parse a single log line."""\n\n\ndef alpha(x):\n    for i in range(x):\n        x += i\n    return x\n',
            'b.py': 'def beta(rs):\n    return len(rs)\n',
        },
        '.',
    )
    assert mapping.strategy == 'single'
    assert 'Parse a single log line' not in text


def test_a_readme_beside_a_package_is_a_valid_purpose(build):
    """The other half of the same rule: a directory's own README does count."""
    _, _, _, text = build(
        {
            'README.md': 'Tools for chewing through webserver logs.\n',
            'a.py': 'def alpha(x):\n    for i in range(x):\n        x += i\n    return x\n',
            'b.py': 'def beta(rs):\n    return len(rs)\n',
        },
        '.',
    )
    assert 'Tools for chewing through webserver logs' in text.splitlines()[0]


def _many_modules(count, prefix):
    """`count` modules of the same shape, each with something worth showing."""
    files = {}
    for index in range(count):
        files['{}{}.py'.format(prefix, index)] = (
            '"""Module {}."""\n\n\ndef entry_{}(xs):\n'
            '    total = 0\n    for x in xs:\n        if x:\n            total += x\n'
            '    return total\n'.format(index, index)
        )
    return files


def test_source_modules_outrank_tests_for_block_slots(build):
    """weep has three scripts and three test files, and spent half its map on tests.

    Sources take the slots first. Tests fill whatever is left over rather than
    being excluded, since an empty slot helps nobody.
    """
    files = {}
    files.update(_many_modules(10, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(10, 'test_t').items()})
    _, _, mapping, _ = build(files, '.')
    titles = [b.title for b in mapping.blocks]
    assert all(not t.startswith('test_') for t in titles), titles


def test_tests_still_fill_slots_the_source_does_not_need(build):
    files = {}
    files.update(_many_modules(2, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(10, 'test_t').items()})
    _, _, mapping, _ = build(files, '.')
    titles = [b.title for b in mapping.blocks]
    assert any(t.startswith('src') for t in titles)
    assert any(t.startswith('test_t') for t in titles)


def test_a_test_directory_on_its_own_still_maps(build):
    """Nothing is deprioritised when tests are all there is."""
    files = {'tests/' + k: v for k, v in _many_modules(10, 'test_t').items()}
    _, _, mapping, _ = build(files, '.')
    assert mapping.blocks
    assert all(b.title.startswith('test_t') for b in mapping.blocks)


def test_blocks_sharing_a_basename_are_told_apart(build):
    """Flask has `flask/app.py` and `flask/sansio/app.py`.

    Two blocks both headed `app.py` ask the reader to guess which is which,
    which is the one question a heading exists to answer.
    """
    body = (
        '"""Module {}."""\n\n\ndef entry_{}(xs):\n    total = 0\n'
        '    for x in xs:\n        if x:\n            total += x\n    return total\n'
    )
    _, _, mapping, _ = build(
        {
            'pkg/__init__.py': '',
            'pkg/app.py': body.format('outer', 1),
            'pkg/sansio/__init__.py': '',
            'pkg/sansio/app.py': body.format('inner', 2),
            'pkg/other.py': body.format('other', 3),
        },
        'pkg',
    )
    titles = [b.title for b in mapping.blocks]
    assert len(titles) == len(set(titles)), titles
    assert 'sansio/app.py' in titles


def test_a_unique_basename_stays_short(build):
    """Disambiguation is paid for only where it is needed."""
    body = (
        '"""Module {}."""\n\n\ndef entry_{}(xs):\n    total = 0\n'
        '    for x in xs:\n        if x:\n            total += x\n    return total\n'
    )
    _, _, mapping, _ = build(
        {
            'pkg/__init__.py': '',
            'pkg/alpha.py': body.format('a', 1),
            'pkg/nested/__init__.py': '',
            'pkg/nested/beta.py': body.format('b', 2),
            'pkg/gamma.py': body.format('g', 3),
        },
        'pkg',
    )
    assert 'beta.py' in [b.title for b in mapping.blocks]


def test_a_constructor_does_not_outrank_the_method_that_does_the_work(build):
    """Found on `httpx`: two `__init__`s took the whole block budget.

    Argument wrangling branches once per optional parameter, which counts the
    same as branching once per real case and means something quite different.
    """
    files = {
        'a.py': (
            '"""P."""\n\n\nclass Client:\n'
            '    def __init__(self, a=None, b=None, c=None, d=None):\n'
            + ''.join(
                '        self.{0} = {0} if {0} is not None else {1}\n'.format(name, i)
                for i, name in enumerate('abcd')
            )
            + '\n    def send(self, request):\n'
            '        for attempt in range(3):\n'
            '            if not request:\n'
            '                continue\n'
            '            return request\n'
            '        return None\n'
        )
    }
    project, _, _, _ = build(files, 'a.py')
    scores = {f.qualname: f.score for f in project.funcs()}
    assert scores['Client.send'] > scores['Client.__init__'], scores


def test_the_best_function_is_shown_even_when_something_calls_it(build):
    """Uncalled is what makes a way in; it is not what makes something worth reading.

    In a class-heavy module the two come apart: everything with callers is
    disqualified as a root, leaving constructors and thin wrappers, while the
    real work sits one level down. httpx's `_client.py` led with two
    `__init__`s and never showed `Client.send`.
    """
    files = {
        'pkg/__init__.py': '',
        'pkg/a.py': (
            '"""P."""\n\n\nclass Client:\n'
            '    def __init__(self, a=None):\n        self.a = a\n        self.b = 1\n\n'
            '    def get(self, url):\n        return self.send(url)\n\n'
            '    def send(self, url):\n'
            '        for attempt in range(3):\n'
            '            if not url:\n                raise ValueError(url)\n'
            '            if attempt > 1:\n                break\n'
            '            for part in url:\n'
            '                if part:\n                    url = part\n'
            '        return url\n'
        ),
        'pkg/b.py': '"""Q."""\n\n\ndef beta(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
        'pkg/c.py': '"""R."""\n\n\ndef gamma(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
    }
    _, _, mapping, _ = build(files, 'pkg')
    block = next(b for b in mapping.blocks if b.title == 'a.py')
    assert block.roots[0].func.qualname == 'Client.send', [
        r.func.qualname for r in block.roots
    ]
