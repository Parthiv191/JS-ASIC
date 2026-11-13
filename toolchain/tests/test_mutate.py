"""The mutation runner (emu/mutate.py): which mutants it makes, and that it tells killed from
survived and timed out."""
from __future__ import annotations

import ast
import textwrap

from emu.mutate import apply, order_tests, run, sites

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
    outs = [apply(SNIPPET, m.index) for m in ms]
    assert all(o != clean for o in outs)
    assert len(set(outs)) == len(outs)
    assert "a - b" in apply(SNIPPET, next(m.index for m in ms if m.desc == "+ -> -"))
    assert "x >>= 1" in apply(SNIPPET, next(m.index for m in ms if m.op == "aug"))


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
from pkg.m import double, spin

def test_double():
    assert double(2) == 4

def test_spin():
    assert spin(3) == 3
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
