"""Mutation testing (verification.md §1): plant one small bug at a time in a source file, run the
tests, and list the mutants no test kills. A survivor is a test gap or an equivalent mutant (one
that changes nothing a program can observe); triage says which.

    cd toolchain && python -m emu.mutate emu/model/engine.py [--workers 4] [--sample 200 --seed 1]
        [--tests a.py,b.py] [--json out.json]     then --report out.json, --show --only 12,40

Each worker is one pytest process at a time, a few hundred MB: 11 of them ran this machine out of
memory. A survivor runs every test, so the time limit is 3 x the clean run measured under load.

| Operator | Mutant |
|---|---|
| `cmp` | `<` ↔ `<=`, `>` ↔ `>=`, `==` ↔ `!=`, `is` ↔ `is not`, `in` ↔ `not in` |
| `bin`, `aug` | `+` ↔ `-`, `&` ↔ `|`, `^` → `|`, `<<` ↔ `>>`, `//` → `*`, `%` → `//` (also in `+=` and the like) |
| `bool` | `and` ↔ `or` |
| `not` | `not x` → `x` |
| `if` | `if c:` → `if not c:` |
| `int` | `n` → `n + 1`, `n` → `n - 1` (not below 0) |
| `true` | `True` ↔ `False` |
| `del` | an assignment or a call statement in a function body → `pass` |

Raise statements, asserts and f-strings are left alone: their mutants only change messages. Each
worker runs the tests in its own copy of the package and the tests, with no bytecode cache (a
rewritten file in the same second could reuse a stale one), so the source tree is never touched.
The tests run fail-fast, the files that import the target's package first."""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # toolchain/

_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
        ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd,
        ast.BitXor: ast.BitOr, ast.LShift: ast.RShift, ast.RShift: ast.LShift, ast.FloorDiv: ast.Mult,
        ast.Mod: ast.FloorDiv}
_SYM = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
        ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in", ast.Add: "+", ast.Sub: "-",
        ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^", ast.LShift: "<<", ast.RShift: ">>",
        ast.FloorDiv: "//", ast.Mult: "*", ast.Mod: "%", ast.And: "and", ast.Or: "or"}


@dataclass(frozen=True)
class Mutant:
    index: int   # the site's number in traversal order: `apply` finds it the same way
    line: int
    func: str    # the enclosing function, or "<module>"
    op: str      # the operator (module docstring)
    desc: str    # what changed, e.g. "< -> <="
    code: str = ""  # the source line, stripped
    nth: int = 0    # how many earlier mutants share (func, desc, code): with them, the review key

    def key(self, file: str) -> tuple:
        return (file, self.func, self.desc, self.code, self.nth)


class _Sites(ast.NodeTransformer):
    """Counts the mutation sites in a fixed order. With `target` set, it rewrites that one."""

    def __init__(self, target: int | None = None, lines: list[str] | None = None):
        self.target = target
        self.lines = lines or []
        self.seen: dict[tuple, int] = {}
        self.found: list[Mutant] = []
        self.func: list[str] = []
        self.in_body = 0  # inside a function body: statement deletion applies

    def _site(self, node: ast.AST, op: str, desc: str) -> bool:
        k, line = len(self.found), getattr(node, "lineno", 0)
        func = ".".join(self.func) or "<module>"
        code = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        nth = self.seen.get((func, desc, code), 0)
        self.seen[(func, desc, code)] = nth + 1
        self.found.append(Mutant(k, line, func, op, desc, code, nth))
        return k == self.target

    # contexts left alone
    def visit_Raise(self, node):
        return node

    def visit_Assert(self, node):
        return node

    def visit_JoinedStr(self, node):
        return node

    def _scope(self, node, name: str, body: bool):
        self.func.append(name)
        self.in_body += body
        for field in ("args", "body"):
            setattr(node, field, self._visit_field(getattr(node, field)))
        self.in_body -= body
        self.func.pop()
        return node

    def _visit_field(self, value):
        if isinstance(value, list):
            out = []
            for v in value:
                r = self.visit(v) if isinstance(v, ast.AST) else v
                if r is None:
                    continue
                out.extend(r if isinstance(r, list) else [r])
            return out
        return self.visit(value) if isinstance(value, ast.AST) else value

    def visit_FunctionDef(self, node):
        return self._scope(node, node.name, True)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.func.append(node.name)
        saved, self.in_body = self.in_body, 0
        node.body = self._visit_field(node.body)
        self.in_body = saved
        self.func.pop()
        return node

    def visit_arguments(self, node):
        node.defaults = self._visit_field(node.defaults)
        node.kw_defaults = self._visit_field(node.kw_defaults)
        return node  # annotations are not code

    def visit_AnnAssign(self, node):
        if node.value is not None:
            node.value = self.visit(node.value)
        return node

    # operators
    def visit_Compare(self, node):
        self.generic_visit(node)
        for i, o in enumerate(node.ops):
            new = _CMP.get(type(o))
            if new is not None and self._site(node, "cmp", f"{_SYM[type(o)]} -> {_SYM[new]}"):
                node.ops[i] = new()
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        new = _BIN.get(type(node.op))
        if new is not None and self._site(node, "bin", f"{_SYM[type(node.op)]} -> {_SYM[new]}"):
            node.op = new()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        new = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._site(node, "bool", f"{_SYM[type(node.op)]} -> {_SYM[new]}"):
            node.op = new()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._site(node, "not", "not x -> x"):
            return node.operand
        return node

    def visit_Constant(self, node):
        v = node.value
        if isinstance(v, bool):
            if self._site(node, "true", f"{v} -> {not v}"):
                return ast.copy_location(ast.Constant(not v), node)
        elif isinstance(v, int):
            for new in (v + 1, v - 1):
                if new >= 0 and self._site(node, "int", f"{v} -> {new}"):
                    return ast.copy_location(ast.Constant(new), node)
        return node

    # statements
    def visit_If(self, node):
        self.generic_visit(node)
        if self.in_body and self._site(node, "if", "if c -> if not c"):
            node.test = ast.UnaryOp(ast.Not(), node.test)
        return node

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        new = _BIN.get(type(node.op))
        if new is not None and self._site(node, "aug", f"{_SYM[type(node.op)]}= -> {_SYM[new]}="):
            node.op = new()
        return self._delete(node)

    def visit_Assign(self, node):
        self.generic_visit(node)
        return self._delete(node)

    def visit_Expr(self, node):
        self.generic_visit(node)
        return self._delete(node) if isinstance(node.value, (ast.Call, ast.Await)) else node

    def _delete(self, node):
        if self.in_body and self._site(node, "del", "statement -> pass"):
            return ast.copy_location(ast.Pass(), node)
        return node


def sites(source: str) -> list[Mutant]:
    s = _Sites(lines=source.splitlines())
    s.visit(ast.parse(source))
    return s.found


def apply(source: str, index: int) -> str:
    """`source` with mutant `index` applied."""
    s = _Sites(index)
    tree = ast.fix_missing_locations(s.visit(ast.parse(source)))
    if index >= len(s.found):
        raise IndexError(f"no mutant {index}: {len(s.found)} sites")
    return ast.unparse(tree)


def diff(source: str, index: int, context: int = 2) -> str:
    """Mutant `index` as a diff against the source, both through `ast.unparse`."""
    a = ast.unparse(ast.parse(source)).splitlines()
    b = apply(source, index).splitlines()
    lines = list(difflib.unified_diff(a, b, lineterm="", n=context))
    return "\n".join(lines[2:])  # without the ---/+++ header


@dataclass(frozen=True)
class Outcome:
    mutant: Mutant
    status: str      # "killed" | "survived" | "timeout" | "error"
    seconds: float
    first_fail: str  # the first failing test, when killed


def order_tests(root: Path, target: str, tests_dir: str = "tests") -> list[str]:
    """Test files that import the target's package first (smallest first), then the rest."""
    mod = target[:-3].replace("/", ".").replace("\\", ".")
    pkg = mod.rsplit(".", 1)[0]
    files = sorted((p for p in (root / tests_dir).glob("test_*.py") if p.name != "test_mutate.py"),
                   key=lambda p: p.stat().st_size)

    def rank(p: Path) -> int:
        t = p.read_text(encoding="utf-8")
        return 0 if mod in t else 1 if pkg in t else 2
    return [p.relative_to(root).as_posix() for p in sorted(files, key=rank)]


def _copy(root: Path, dst: Path, parts: list[str]) -> Path:
    """Copies each part (relative to `root`, `../` allowed) to the same place under `dst`, so
    paths the tests build from the repo root still resolve; returns the copy of `root`."""
    ignore = shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc")
    top = root.resolve().parent
    for part in parts:
        src = (root / part).resolve()
        out = dst / src.relative_to(top)
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, out, ignore=ignore)
        else:
            shutil.copy2(src, out)
    return dst / root.resolve().name


_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def _pytest(cwd: Path, tests: list[str], timeout: float) -> tuple[str, float, str]:
    env = {**os.environ, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-rfE", "-p", "no:cacheprovider", *tests]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", time.monotonic() - t0, ""
    dt = time.monotonic() - t0
    m = _FAILED.search(p.stdout)
    first = m.group(1) if m else ""
    if p.returncode == 0:
        return "survived", dt, ""
    if p.returncode == 1:
        return "killed", dt, first
    tail = p.stdout.strip().splitlines()
    return "error", dt, first or (tail[-1] if tail else f"exit {p.returncode}")


def run(root: Path, target: str, mutants: list[Mutant], workers: int = 4,
        parts: tuple[str, ...] = ("emu", "tests", "pyproject.toml", "../docs/design"), tests: list[str] | None = None,
        timeout: float | None = None, work: Path | None = None, progress=None) -> tuple[float, list[Outcome]]:
    """Runs every mutant; returns the clean run's seconds and the outcomes in mutant order. The
    clean run goes on every worker at once, so the time limit (3 x its slowest, unless `timeout`
    is given) is measured under the load the mutants run with: a survivor runs every test, and
    must not pass for a hang."""
    source = (root / target).read_text(encoding="utf-8")
    tests = tests or order_tests(root, target)
    base = Path(tempfile.mkdtemp(prefix="mutate-", dir=work))
    dirs: queue.Queue[Path] = queue.Queue()
    n = max(1, workers)
    try:
        copies = []
        for i in range(n):
            d = base / f"w{i}"
            d.mkdir()
            copies.append(_copy(root, d, list(parts)))
        with ThreadPoolExecutor(max_workers=n) as ex:
            cleans = list(ex.map(lambda d: _pytest(d, tests, 3600), copies))
        for status, _, first in cleans:
            if status != "survived":
                raise RuntimeError(f"the tests fail with no mutant ({status}: {first})")
        clean = max(dt for _, dt, _ in cleans)
        limit = timeout or max(30.0, 3 * clean)
        for d in copies:
            dirs.put(d)

        def one(m: Mutant) -> Outcome:
            d = dirs.get()
            try:
                (d / target).write_text(apply(source, m.index), encoding="utf-8")
                status, dt, first = _pytest(d, tests, limit)
                (d / target).write_text(source, encoding="utf-8")
                out = Outcome(m, status, round(dt, 1), first)
                if progress:
                    progress(out)
                return out
            finally:
                dirs.put(d)
        with ThreadPoolExecutor(max_workers=n) as ex:
            return clean, list(ex.map(one, mutants))
    finally:
        shutil.rmtree(base, ignore_errors=True)


EQUIVALENTS = ROOT / "tests" / "mutation_equivalents.json"


def load_equivalents(path: Path = EQUIVALENTS) -> dict[tuple, str]:
    """The reviewed equivalent mutants, keyed (file, func, desc, code, nth), each with why it
    changes nothing a program can observe."""
    if not path.exists():
        return {}
    return {(e["file"], e["func"], e["desc"], e["code"], e.get("nth", 0)): e["why"]
            for e in json.loads(path.read_text(encoding="utf-8"))}


def summary(outcomes: list[Outcome], file: str = "", equivalents: dict[tuple, str] | None = None) -> str:
    """Counts, the score with and without the reviewed equivalents, and the open survivors."""
    equivalents = equivalents or {}
    n = len(outcomes)
    count = {k: sum(o.status == k for o in outcomes) for k in ("killed", "timeout", "error", "survived")}
    survivors = [o.mutant for o in outcomes if o.status == "survived"]
    open_ = [m for m in survivors if m.key(file) not in equivalents]
    equiv = len(survivors) - len(open_)
    dead = n - count["survived"]
    lines = [f"{n} mutants: {count['killed']} killed, {count['timeout']} timed out, {count['error']} errors, "
             f"{count['survived']} survived ({equiv} reviewed as equivalent); score "
             f"{100 * dead / max(1, n):.1f}%, {100 * dead / max(1, n - equiv):.1f}% without the equivalents"]
    for m in open_:
        lines.append(f"  #{m.index:<5} line {m.line:<5} {m.func:<32} {m.op:<5} {m.desc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emu.mutate", description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="a source file under toolchain/, e.g. emu/model/engine.py")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sample", type=int, help="run this many mutants, drawn at random")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--func", help="only mutants in functions matching this regex")
    ap.add_argument("--only", help="comma-separated mutant indices")
    ap.add_argument("--list", action="store_true", help="list the mutants and stop")
    ap.add_argument("--show", action="store_true", help="print each mutant as a diff and stop")
    ap.add_argument("--tests", help="comma-separated test files to run instead of all of them")
    ap.add_argument("--timeout", type=float, help="seconds before a mutant counts as a hang (default: 3 x the clean run under load)")
    ap.add_argument("--json", type=Path, help="write the outcomes here")
    ap.add_argument("--report", type=Path, help="summarise outcomes written by --json, and stop")
    a = ap.parse_args(argv)
    source = (ROOT / a.target).read_text(encoding="utf-8")
    ms = sites(source)
    if a.report:  # by index, so the source must be the one the run mutated
        outs = [Outcome(ms[o["index"]], o["status"], o["seconds"], o["first_fail"])
                for o in json.loads(a.report.read_text(encoding="utf-8"))]
        print(summary(outs, a.target, load_equivalents()))
        return 0
    if a.func:
        ms = [m for m in ms if re.search(a.func, m.func)]
    if a.only:
        keep = {int(x) for x in a.only.split(",")}
        ms = [m for m in ms if m.index in keep]
    if a.sample and a.sample < len(ms):
        ms = sorted(random.Random(a.seed).sample(ms, a.sample), key=lambda m: m.index)
    if a.list:
        for m in ms:
            print(f"#{m.index:<5} line {m.line:<5} {m.func:<32} {m.op:<5} {m.desc}")
        return 0
    if a.show:
        for m in ms:
            print(f"#{m.index} line {m.line} {m.func}: {m.desc}")
            print(diff(source, m.index))
        return 0
    done = [0]

    def progress(o: Outcome) -> None:
        done[0] += 1
        print(f"[{done[0]}/{len(ms)}] #{o.mutant.index} {o.status} {o.seconds}s {o.first_fail}", flush=True)
    tests = a.tests.split(",") if a.tests else None
    clean, outs = run(ROOT, a.target, ms, workers=a.workers, tests=tests, timeout=a.timeout, progress=progress)
    print(f"clean run {clean:.0f}s")
    print(summary(outs, a.target, load_equivalents()))
    if a.json:
        a.json.write_text(json.dumps([{**asdict(o.mutant), "status": o.status, "seconds": o.seconds,
                                       "first_fail": o.first_fail} for o in outs], indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
