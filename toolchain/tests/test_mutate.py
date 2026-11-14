"""The mutation runner (emu/mutate.py): which mutants it makes, and that it tells killed from
survived and timed out."""
from __future__ import annotations

import ast
import textwrap

from emu.mutate import apply, order_tests, run, sites
from simutil import reads_source

SNIPPET = textwrap.dedent('''
    N = 3

    def f(a, b=2):
        x: int = a + b
        if a < b and not a:
            x <<= 1
        g(x)
        return x == 4 or True
''')


def descs(src: str) -> list[tuple[str, str]]:
    return [(m.op, m.desc) for m in sites(src)]


def test_each_operator_makes_its_mutants():
    got = descs(SNIPPET)
    for want in [("int", "3 -> 4"), ("int", "3 -> 2"), ("int", "2 -> 3"), ("int", "2 -> 1"),
                 ("bin", "+ -> -"), ("cmp", "< -> <="), ("not", "not x -> x"), ("bool", "and -> or"),
                 ("aug", "<<= -> >>="), ("if", "if c -> if not c"), ("cmp", "== -> !="),
                 ("bool", "or -> and"), ("true", "True -> False"), ("del", "statement -> pass")]:
        assert want in got, want
    # x <<= 1 and g(x) can be deleted; the annotated assignment and the module constant can't
    assert got.count(("del", "statement -> pass")) == 2


def test_every_mutant_is_valid_python_and_changes_one_thing():
    clean = ast.unparse(ast.parse(SNIPPET))
    ms = sites(SNIPPET)
    outs = [ast.unparse(ast.parse(apply(SNIPPET, m.index))) for m in ms]
    assert all(o != clean for o in outs)
    assert len(set(outs)) == len(outs)
    assert "a - b" in outs[next(m.index for m in ms if m.desc == "+ -> -")]
    assert "x >>= 1" in outs[next(m.index for m in ms if m.op == "aug")]


LAYOUT = textwrap.dedent('''
    def f(a, b, diags):  # § a comment
        s = "§" + "x"; t = (a +
                            b)
        if a < (b + 1) * 2:
            diags.add("R1-x", "msg")
        elif not (a and b):
            t -= 1
        return t, s
''')


def test_a_mutant_changes_only_its_own_text():
    """apply() splices the mutated node into the source, so the file keeps its comments, quotes
    and layout. A test may read the source: test_rule_coverage greps the compiler and the analysis
    for `diags.add("R1-...")`. Rebuilt with ast.unparse, every mutant of those files failed it,
    whatever it changed. Each mutant parses to the tree the visitor makes, and only its lines differ
    (column offsets count UTF-8 bytes, hence the § before a site)."""
    from emu.mutate import _Sites
    lines = LAYOUT.splitlines()
    for m in sites(LAYOUT):
        out = apply(LAYOUT, m.index)
        assert ast.dump(ast.parse(out)) == ast.dump(_Sites(m.index).visit(ast.parse(LAYOUT))), m
        assert "# § a comment" in out, m
        assert 'diags.add("R1-x", "msg")' in out or m.op == "del", m
        new = out.splitlines()
        assert len(new) <= len(lines)
        if len(new) == len(lines):
            assert all(x == y for i, (x, y) in enumerate(zip(lines, new)) if i + 1 != m.line), m


@reads_source
def test_a_mutant_of_the_analysis_keeps_the_rule_ids_it_raises():
    import re

    from emu.mutate import ROOT
    src = (ROOT / "emu/analysis.py").read_text(encoding="utf-8")
    rules = re.compile(r'(?:err|diags\.add)\(\s*"([RST]\d?-[a-z][a-z-]*)"')
    m = next(m for m in sites(src) if m.op == "cmp")
    assert set(rules.findall(apply(src, m.index))) == set(rules.findall(src)) != set()


def test_messages_asserts_and_annotations_are_left_alone():
    src = textwrap.dedent('''
        def f(x: "tuple[int, int]") -> int:
            assert x > 0, "x must be > 0"
            if x == 1:
                raise ValueError(f"bad {x + 1}")
            return 0
    ''')
    got = descs(src)
    assert ("cmp", "> -> >=") not in got and ("bin", "+ -> -") not in got
    assert ("cmp", "== -> !=") in got and ("int", "0 -> 1") in got


def test_statements_are_deleted_only_in_function_bodies():
    src = "X = 1\nclass C:\n    y = 2\n    def m(self):\n        self.y = 3\n"
    dels = [m for m in sites(src) if m.op == "del"]
    assert [m.func for m in dels] == ["C.m"]


PKG = '''
def double(x):
    return x + x

def untested(x):
    return x - 1

def spin(n):
    i = 0
    while i < n:
        i += 1
    return i
'''
TEST = '''
import os

from pkg.m import double, spin

def test_double():
    assert double(2) == 4

def test_spin():
    assert spin(3) == 3

def test_the_runner_flags_its_runs():  # tests that read the source skip under it
    assert os.environ.get("EMU_MUTANT") == "1"
'''


def test_the_runner_tells_killed_survived_and_timeout(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text(PKG)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text(TEST)
    assert order_tests(tmp_path, "pkg/m.py") == ["tests/test_m.py"]
    ms = sites(PKG)
    pick = {("double", "+ -> -"), ("untested", "- -> +"), ("spin", "+= -> -=")}
    ms = [m for m in ms if (m.func, m.desc) in pick]
    assert len(ms) == 3
    clean, outs = run(tmp_path, "pkg/m.py", ms, workers=3, parts=("pkg", "tests"), timeout=8)
    status = {o.mutant.func: o.status for o in outs}
    assert status == {"double": "killed", "untested": "survived", "spin": "timeout"}
    killed = next(o for o in outs if o.status == "killed")
    assert killed.first_fail == "tests/test_m.py::test_double"
    assert (tmp_path / "pkg" / "m.py").read_text() == PKG  # the source tree is never touched


@reads_source
def test_every_reviewed_equivalent_is_a_current_mutant_with_a_reason():
    """tests/mutation_equivalents.json: survivors reviewed as equivalent, keyed by function,
    change and source line (not by index, which moves with every edit). An entry that matches
    no mutant is stale: the code changed, so the review has to be done again."""
    from pathlib import Path

    from emu.mutate import EQUIVALENTS, ROOT, load_equivalents
    eq = load_equivalents(EQUIVALENTS)
    assert eq
    keys: set[tuple] = set()
    for file in {k[0] for k in eq}:
        keys |= {m.key(file) for m in sites((ROOT / file).read_text(encoding="utf-8"))}
    stale = [k for k in eq if k not in keys]
    assert not stale
    assert all(len(why) > 20 for why in eq.values())
    assert Path(EQUIVALENTS).parent.name == "tests"


def test_the_summary_sets_reviewed_equivalents_apart():
    from emu.mutate import Mutant, Outcome, summary
    ms = [Mutant(0, 1, "f", "cmp", "< -> <=", "x < y"), Mutant(1, 2, "f", "int", "1 -> 2", "z = 1")]
    outs = [Outcome(ms[0], "survived", 1.0, ""), Outcome(ms[1], "survived", 1.0, "")]
    text = summary(outs, "m.py", {ms[0].key("m.py"): "why"})
    assert "(1 reviewed as equivalent)" in text and "#1 " in text and "#0 " not in text
