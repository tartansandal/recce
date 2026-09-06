"""Filtering: the judgements that make a map a map rather than a listing."""

from __future__ import annotations

from recce import rank
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


def test_source_modules_take_the_block_slots(build):
    """A map of the source is a map of the source, tests excluded."""
    files = {}
    files.update(_many_modules(10, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(10, 'test_t').items()})
    _, _, mapping, _ = build(files, '.')
    titles = [b.title for b in mapping.blocks]
    assert all(not t.startswith('test_') for t in titles), titles


def test_a_small_project_excludes_its_tests_too(build):
    """weep's actual shape: three scripts and three test files.

    This is the failure `_select_modules` was written for, and for a long time
    it was not covered at the size it happens at. The case above uses twenty
    modules, which is over the block cap and so reaches the ranking; weep's six
    are under it, and the early return handed back all six with half the map
    spent on tests — the exact thing the fix was for, still happening.
    """
    files = {}
    files.update(_many_modules(3, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(3, 'test_t').items()})
    _, _, mapping, _ = build(files, '.')
    titles = [b.title for b in mapping.blocks]
    assert [t for t in titles if t.startswith('src')], titles
    assert not [t for t in titles if t.startswith('test_')], titles


def test_tests_do_not_take_a_slot_the_source_leaves_spare(build):
    """Two source modules and ten test files map to two blocks, not eight.

    An empty slot is the right outcome rather than a wasted one. Understanding
    a package and understanding its suite are separate jobs, and a map that
    answers neither is worse than a short map that answers one.
    """
    files = {}
    files.update(_many_modules(2, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(10, 'test_t').items()})
    _, _, mapping, _ = build(files, '.')
    titles = [b.title for b in mapping.blocks]
    assert all(t.startswith('src') for t in titles), titles


def test_tests_left_out_are_counted_apart_from_what_did_not_fit(build):
    """Two absences with two different answers, so two different numbers."""
    files = {}
    files.update(_many_modules(3, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(4, 'test_t').items()})
    _, _, mapping, text = build(files, '.')
    assert mapping.omitted_modules == 0
    assert mapping.omitted_tests == 4
    assert 'further modules not shown' not in text
    assert '4 test modules are not mapped here' in text


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

    def walk(node):
        yield node
        for child in node.children:
            yield from child.walk() if hasattr(child, 'walk') else walk(child)

    shown = {n.func.qualname for r in block.roots for n in walk(r) if n.func}
    # The root is whatever leads furthest into the block, which here is the
    # public method rather than the worker it delegates to — and that is a
    # better answer than the worker, because the reader gets the way in and the
    # work below it. What must not happen is the block leading with a
    # constructor while the work never appears at all.
    assert not block.roots[0].func.name.startswith('__'), [
        r.func.qualname for r in block.roots
    ]
    assert 'Client.send' in shown, shown


def test_a_declared_entry_point_outranks_every_inference(build):
    from recce.rank import _resolve_declared

    files = {
        'pyproject.toml': "[project]\nname='x'\n\n[project.scripts]\nx='pkg.cli:run'\n",
        'pkg/__init__.py': '',
        'pkg/cli.py': '"""Entry."""\n\n\ndef run(xs):\n    total = 0\n    for x in xs:\n        if x:\n            total += x\n    return total\n',
        'pkg/other.py': '"""Other."""\n\n\ndef helper(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
        'pkg/third.py': '"""Third."""\n\n\ndef more(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
    }
    project, _, mapping, _ = build(files, 'pkg')
    assert project.declared_entries == ['pkg.cli:run']
    assert _resolve_declared('pkg.cli:run', project).node_id == 'pkg.cli::run'
    assert mapping.entries[0].node_id == 'pkg.cli::run'


def test_a_shim_entry_point_does_not_lead_the_spine(build):
    """flask declares `flask.cli:main`, whose whole body is `cli.main()`.

    "Spine to read first" pointing at a forwarding address sends the reader to
    the wrong file. The shim can still be starred inside its own block, where
    it is the only thing there and the star means "this is all of it".
    """
    files = {
        'pyproject.toml': "[project]\nname='x'\n\n[project.scripts]\nx='pkg.cli:main'\n",
        'pkg/__init__.py': '',
        'pkg/cli.py': '"""Entry."""\n\nimport click\n\n\ndef main():\n    click.main()\n',
        'pkg/work.py': '"""Work."""\n\n\ndef process(xs):\n    total = 0\n    for x in xs:\n        if x > 1:\n            total += x\n        elif x:\n            total -= x\n    return total\n',
        'pkg/third.py': '"""Third."""\n\n\ndef more(xs):\n    for x in xs:\n        if x:\n            return x\n    return None\n',
    }
    project, _, mapping, _ = build(files, 'pkg')
    assert mapping.entries[0].node_id == 'pkg.cli::main'
    spine = [f.node_id for f in mapping.spine]
    assert 'pkg.cli::main' not in spine, spine
    assert spine[0] == 'pkg.work::process'


_APP_FILES = {
    'pkg/__init__.py': '',
    'pkg/one.py': '"""One."""\n\n\ndef step_one(xs):\n    total = 0\n    for x in xs:\n        if x:\n            total += x\n    return total\n',
    'pkg/two.py': '"""Two."""\n\nfrom .one import step_one\n\n\ndef step_two(xs):\n    return step_one(xs) + 1\n',
    'pkg/three.py': '"""Three."""\n\nfrom .two import step_two\n\n\ndef step_three(xs):\n    return step_two(xs) * 2\n',
    'pkg/four.py': '"""Four."""\n\nfrom .three import step_three\n\n\ndef step_four(xs):\n    return step_three(xs) - 1\n',
    'pkg/five.py': '"""Five."""\n\nfrom .four import step_four\n\n\ndef step_five(xs):\n    return step_four(xs) + 3\n',
    'pkg/cli.py': '"""Entry."""\n\nfrom .five import step_five\n\n\ndef main(argv):\n    """Run it."""\n    return step_five(argv)\n',
}


def test_type_app_draws_a_flow_without_a_declared_script(build):
    """The case the flag exists for.

    httpie, flake8 and pre-commit declare no console script, so the evidence
    gate leaves them with no way to ask for the flow that crosses their
    modules. `--type app` is the reader supplying what the manifest does not.
    """
    _, _, plain, _ = build(_APP_FILES, 'pkg')
    _, _, asked, _ = build(_APP_FILES, 'pkg', kind='app')
    assert not any(b.spanning for b in plain.blocks)
    assert [b.spanning for b in asked.blocks][0]
    assert 'across' in asked.blocks[0].title


def test_type_lib_suppresses_the_flow_block(build):
    """Said of the same tree that `--type app` draws a flow across."""
    _, _, as_app, _ = build(_APP_FILES, 'pkg', kind='app')
    _, _, as_lib, _ = build(_APP_FILES, 'pkg', kind='lib')
    assert any(b.spanning for b in as_app.blocks)
    assert not any(b.spanning for b in as_lib.blocks)


def test_type_test_makes_the_suite_the_subject(build):
    """The mirror of the default, for a tree holding both.

    A repository root has source and tests in it and the tree cannot say which
    the reader came for. Left alone recce maps the source; asked, it maps the
    suite, and in neither case does it mix them.
    """
    files = {}
    files.update(_many_modules(4, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(4, 'test_t').items()})
    _, _, default, _ = build(files, '.')
    _, _, asked, _ = build(files, '.', kind='test')
    assert all(b.title.startswith('src') for b in default.blocks), default.blocks
    assert all(b.title.startswith('test_t') for b in asked.blocks), asked.blocks


def test_type_test_does_not_report_the_source_it_was_told_to_drop(build):
    """Source is absent because it was asked to be, so it is not an omission."""
    files = {}
    files.update(_many_modules(3, 'src'))
    files.update({'tests/' + k: v for k, v in _many_modules(3, 'test_t').items()})
    _, _, mapping, text = build(files, '.', kind='test')
    assert mapping.omitted_tests == 0
    assert 'not mapped here' not in text


def test_a_tree_too_wide_to_prune_is_cut_rather_than_shipped_over(build):
    """The ladder trades depth, and some trees are wide.

    Every concession in `_CONCESSION_ORDER` shortens a tree: fewer notes,
    shallower externals, a lower depth cap, no skims. None of them narrows one.
    A function calling fifty others is fifty rows at any depth, so the ladder
    runs out with the tree still over budget, and it used to ship anyway —
    yt-dlp rendered 114 rows against a budget of 40.
    """
    calls = '\n'.join('    helper_{}(x)'.format(n) for n in range(50))
    helpers = '\n\n'.join(
        'def helper_{}(x):\n    total = 0\n    for i in range(x):\n'
        '        if i:\n            total += i\n    return total'.format(n)
        for n in range(50)
    )
    _, _, mapping, text = build(
        {
            'a.py': '"""Wide."""\n\n\ndef go(x):\n{}\n    return x\n\n\n{}\n'.format(
                calls, helpers
            )
        },
        'a.py',
        max_lines=40,
    )
    for block in mapping.blocks:
        assert block.line_count() <= 40, block.line_count()
    assert 'more' in text


def test_what_was_cut_is_counted_not_silently_dropped(build):
    """A reader who cannot see that rows went has been misled, not spared."""
    calls = '\n'.join('    helper_{}(x)'.format(n) for n in range(30))
    helpers = '\n\n'.join(
        'def helper_{}(x):\n    total = 0\n    for i in range(x):\n'
        '        if i:\n            total += i\n    return total'.format(n)
        for n in range(30)
    )
    _, _, _, text = build(
        {
            'a.py': '"""Wide."""\n\n\ndef go(x):\n{}\n    return x\n\n\n{}\n'.format(
                calls, helpers
            )
        },
        'a.py',
        max_lines=20,
    )
    marker = [line for line in text.splitlines() if '…' in line and 'more' in line]
    assert marker, text
    # The count names rows that exist rather than rows that were rendered.
    assert int(marker[0].split()[-2]) > 0


def test_a_repeat_of_a_cross_module_call_goes_first_under_pressure(build):
    """The cheapest concession, and the only row that carries nothing.

    A second appearance of a reference leaf is an edge the reader has already
    met. Dropping it hides no call, which is why it is spent before notes.
    """
    files = {
        'pkg/__init__.py': '',
        'pkg/shared.py': (
            '"""Shared."""\n\n\ndef helper(xs):\n    total = 0\n'
            '    for x in xs:\n        if x:\n            total += x\n    return total\n'
        ),
    }
    # Several callers into one shared module, so the reference repeats.
    for index in range(8):
        files['pkg/m{}.py'.format(index)] = (
            '"""M{}."""\n\nfrom .shared import helper\n\n\n'
            'def entry_{}(xs):\n    for x in xs:\n        if x:\n'
            '            helper(x)\n    return helper(xs)\n'.format(index, index)
        )
    _, _, roomy, _ = build(files, 'pkg', max_lines=40)
    _, _, tight, text = build(files, 'pkg', max_lines=6)

    def refs(mapping):
        return [
            node
            for block in mapping.blocks
            for root in block.roots
            for node in _iter(root)
            if node.func is None and not node.bracket and '.' in node.label
        ]

    def _iter(node):
        yield node
        for child in node.children:
            yield from _iter(child)

    assert not [n for n in refs(tight) if n.repeat], 'repeats survived the squeeze'
    assert refs(roomy), 'the roomy map should still show the references'


def _uip_shaped_tree():
    """An app whose entry module holds nothing but the entry point.

    The shape that raised this: `main` calls across three other modules and its
    own file has no second function, so the module block is the spanning block
    redrawn with every crossing collapsed back to a stub.
    """
    return {
        'pkg/__init__.py': '',
        'pkg/cli.py': '"""CLI."""\n\n'
        'from .io import read\n'
        'from .work import run\n'
        'from .out import write\n\n\n'
        'def main(argv):\n'
        '    data = read(argv)\n'
        '    if not data:\n'
        '        return 1\n'
        '    for item in data:\n'
        '        run(item)\n'
        '    write(data)\n'
        '    return 0\n',
        'pkg/io.py': '"""IO."""\n\nimport json\n\n\n'
        'def read(path):\n'
        '    if path:\n'
        '        for p in path:\n'
        '            json.loads(p)\n'
        '    return path\n',
        'pkg/work.py': '"""Work."""\n\nimport re\n\n\n'
        'def run(item):\n'
        '    if item:\n'
        '        for c in item:\n'
        '            re.match(c, item)\n'
        '    return item\n',
        'pkg/out.py': '"""Out."""\n\nimport csv\n\n\n'
        'def write(rows):\n'
        '    if rows:\n'
        '        for r in rows:\n'
        '            csv.writer(r)\n'
        '    return rows\n',
    }


def test_the_spine_list_names_a_function_once(build):
    """A spanning block and its own module's block lead with the same function.

    Listing it twice spends one of five slots telling the reader to start where
    they were already told to start. Seen on yt-dlp and cookiecutter.
    """
    _, _, _, text = build(_uip_shaped_tree(), 'pkg', max_lines=12, kind='app')
    spine = text.split('## Spine to read first')[1].split('## Legend')[0]
    entries = [
        line
        for line in spine.splitlines()
        if line.strip().startswith(('1.', '2.', '3.', '4.', '5.'))
    ]
    assert len(entries) == len({e.split('. ', 1)[1] for e in entries}), entries


def test_a_module_block_that_only_restates_the_spanning_block_is_dropped(build):
    """The 58 rows, 15.5% of a real map, that named nothing block one lacked."""
    _, _, plan_, _ = build(_uip_shaped_tree(), 'pkg', max_lines=12, kind='app')
    spanning = [b for b in plan_.blocks if b.spanning]
    assert spanning, 'expected a spanning block under --type app'
    leads = [
        b.roots[0].func.node_id
        for b in plan_.blocks
        if not b.spanning and b.roots and b.roots[0].func
    ]
    assert spanning[0].roots[0].func.node_id not in leads


def test_a_restating_block_is_caught_when_the_collapsed_row_has_a_bracket(build):
    """The case the fixture above is too simple to reach.

    A collapsed `… a, b, c` row carries a bracket whenever the helpers it
    folded call externals, and `_row_tokens` tested `bracket` before the `…`
    prefix — so those rows contributed their literal text instead of the names
    inside them. The block that should have been dropped was kept, because the
    two collapsed rows fold different numbers of helpers and their labels never
    matched.

    Here `main` calls four trivial helpers that the spanning block folds into
    one bracketed row while the module block shows them as four stubs. That
    asymmetry is the whole bug, and the simpler fixture has no trivial helpers
    to fold.
    """
    _, _, plan_, _ = build(
        {
            'pkg/__init__.py': '',
            'pkg/helpers.py': '"""Helpers."""\n\nimport time\nimport uuid\n\n\n'
            'def stamp():\n    return time.time()\n\n\n'
            'def new_id():\n    return uuid.uuid4()\n\n\n'
            'def tag():\n    return uuid.uuid1()\n\n\n'
            'def mark():\n    return time.monotonic()\n',
            'pkg/io.py': '"""IO."""\n\nimport json\n\n\n'
            'def read(path):\n'
            '    if path:\n        for p in path:\n            json.loads(p)\n'
            '    return path\n',
            'pkg/work.py': '"""Work."""\n\nimport re\n\n\n'
            'def run(item):\n'
            '    if item:\n        for c in item:\n            re.match(c, item)\n'
            '    return item\n',
            'pkg/cli.py': '"""CLI."""\n\n'
            'from .helpers import mark, new_id, stamp, tag\n'
            'from .io import read\n'
            'from .work import run\n\n\n'
            'def main(argv):\n'
            '    data = read(argv)\n'
            '    if not data:\n        return 1\n'
            '    for item in data:\n        run(item)\n'
            '    stamp()\n    new_id()\n    tag()\n    mark()\n'
            '    return 0\n',
        },
        'pkg',
        max_lines=14,
        kind='app',
    )
    spanning = [b for b in plan_.blocks if b.spanning]
    assert spanning, 'expected a spanning block under --type app'
    # The collapsed row is what makes this fixture worth having.
    tokens: set = set()
    for root in spanning[0].roots:
        rank._row_tokens(root, tokens)
    assert {'stamp', 'new_id', 'tag', 'mark'} <= tokens, sorted(tokens)
    leads = [
        b.roots[0].func.node_id
        for b in plan_.blocks
        if not b.spanning and b.roots and b.roots[0].func
    ]
    assert spanning[0].roots[0].func.node_id not in leads
