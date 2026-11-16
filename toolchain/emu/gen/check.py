"""Run a random program in every role and check it: the rule cross-check (verification.md §1).

A case is a recipe plus its environment: each engine's clock and phase and the host words. The
compiler either rejects the program (a timing rule: recorded, not a failure; any other rule: a
generator bug) or accepts it, and then on the model, with every clock within the description's
tolerance and no glitches, these must hold:

| Oracle | Fails when |
|---|---|
| `violation` | an engine logs a violation (isa §7: OVERRUN, EARLY, TIMEOUT, LEVEL, EDGE, ...) |
| `contention` | two drivers fight on a wire |
| `completion` | an engine doesn't halt, or the run isn't quiescent, by the tick budget |
| `agreement` | the engines' timed steps (pc, op, XFER value), wire registers r1-r4 or RX words differ |
| `value` | an XFER's value isn't what the recipe says (`expected_values`: src, host words, prep ops), so wrong semantics that every role shares fail too |
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, replace

from ..compiler import CompileError, compile_desc
from ..isa import Dst, Role, unpack_xfer
from ..model.engine import VIOLATIONS
from ..model.system import EngineSpec, System
from .randdesc import PARTIES, Recipe, build, expected_values, random_recipe

ROLES = (Role.P0, Role.P1, Role.MON)
HOST_WORDS = 24      # per party: more than any recipe pops
MAX_TICKS = 400_000


@dataclass(frozen=True)
class Env:
    clocks: tuple[tuple[int, int], ...]   # per engine, in ROLES order
    phases: tuple[int, ...]
    words: tuple[tuple[int, ...], ...]    # host words for P0 and P1
    late: tuple[int, int] | None = None   # (first tick, gap): the host writes one word at a time

    @staticmethod
    def draw(rng: random.Random, ppm: int, late_ok: bool = False) -> "Env":
        """Clocks within the tolerance: rates num/1000 with the slowest no more than ppm below 1.
        Every engine starts at tick 0: the timing rules assume the parties start together (from
        reset, instruction 0 starts at now = 2 in every role), so start skew is outside them."""
        lo = 1000 - ppm // 1000
        if rng.random() < 0.5:  # the corners, where the rules are tight
            clocks = tuple((rng.choice((lo, 1000)), 1000) for _ in ROLES)
        else:
            clocks = tuple((rng.randint(lo, 1000), 1000) for _ in ROLES)
        phases = tuple(0 for _ in ROLES)
        words = tuple(tuple(rng.randrange(1 << 16) for _ in range(HOST_WORDS)) for _ in range(2))
        late = (rng.randint(0, 1500), rng.randint(1, 300)) if late_ok and rng.random() < 0.4 else None
        return Env(clocks, phases, words, late)



@dataclass(frozen=True)
class Case:
    recipe: Recipe
    env: Env

    def to_json(self) -> str:
        return json.dumps({"recipe": self.recipe.to_json(), "env": asdict(self.env)}, indent=1)

    @staticmethod
    def from_json(text: str) -> "Case":
        d = json.loads(text)
        e = d["env"]
        env = Env(tuple(tuple(c) for c in e["clocks"]), tuple(e["phases"]), tuple(tuple(w) for w in e["words"]),
                  tuple(e["late"]) if e.get("late") else None)
        return Case(Recipe.from_json(d["recipe"]), env)


@dataclass(frozen=True)
class Result:
    verdict: str             # "ok" | "rejected" | "structural" | "fail"
    rules: tuple[str, ...]   # the rejecting rules, or the failed oracles
    detail: str = ""


def compile_recipe(r: Recipe):
    """(image, None), or (None, Result) when the compiler rejects it."""
    try:
        return compile_desc(build(r)), None
    except CompileError as e:
        rules = tuple(sorted({x.rule for x in e.diags}))
        # the timing rules and R2-edge are left to the compiler; any other rule is a generator bug
        verdict = "rejected" if all(x.startswith("T") or x == "R2-edge" for x in rules) else "structural"
        return None, Result(verdict, rules, str(e))


def run_case(case: Case) -> Result:
    img, rejected = compile_recipe(case.recipe)
    if rejected is not None:
        return rejected
    env = case.env
    specs = [EngineSpec(img, r, clock=c, phase=p) for r, c, p in zip(ROLES, env.clocks, env.phases)]
    s = System(specs)
    for i in (0, 1):
        if env.late is None:
            s.host_push(i, list(env.words[i]))
        else:  # a slow host: POPs stall, and T5-stall must keep the others waiting
            for k, w in enumerate(env.words[i]):
                s.host_push(i, [w], at_tick=env.late[0] + k * env.late[1])
    t = s.run(MAX_TICKS)
    fails: list[tuple[str, str]] = []
    if t.contention:
        fails.append(("contention", f"first at tick, wire {t.contention[0]}"))
    for et in t.engines:
        bad = [e for e in et.events if e.kind in VIOLATIONS]
        if bad:
            e = bad[0]
            fails.append(("violation", f"{et.role.name}: {e.kind} at pc {e.pc}, cycle {e.cycle} (measured {e.measured})"))
    if not (t.quiescent and all(et.halted for et in t.engines)):
        fails.append(("completion", "not halted: " + ", ".join(et.role.name for et in t.engines if not et.halted)))
    steps = [[(r.pc, r.op, r.value if r.op == "XFER" else None) for r in et.records] for et in t.engines]
    for et, st in zip(t.engines[1:], steps[1:]):
        if st != steps[0]:
            k = next((i for i, (x, y) in enumerate(zip(steps[0], st)) if x != y), min(len(st), len(steps[0])))
            fails.append(("agreement", f"{et.role.name} vs P0 at step {k}: "
                                       f"{st[k] if k < len(st) else 'end'} vs {steps[0][k] if k < len(steps[0]) else 'end'}"))
            break
    else:
        for et in t.engines[1:]:
            if et.regs[1:5] != t.engines[0].regs[1:5]:
                fails.append(("agreement", f"{et.role.name} vs P0: r1-r4 {et.regs[1:5]} vs {t.engines[0].regs[1:5]}"))
                break
    instrs = img.instrs()
    for et in t.engines:  # dst = FIFO pushes what a follower received (isa §4.3); the owner pushes nothing
        want = [r.value for r in et.records if r.op == "XFER" and r.mode == "follow"
                and unpack_xfer(instrs[r.pc].operand)[2] == Dst.FIFO]
        if et.rx != want:
            fails.append(("agreement", f"{et.role.name}: rx {et.rx}, but it followed {want} into the FIFO"))
    want = expected_values(case.recipe, dict(zip(PARTIES, (list(w) for w in env.words))))
    got = [r.value for r in t.engines[0].records if r.op == "XFER"]
    if got != want:
        k = next((i for i, (x, y) in enumerate(zip(got, want)) if x != y), min(len(got), len(want)))
        fails.append(("value", f"XFER {k}: {got[k] if k < len(got) else 'none'}, the recipe says "
                                 f"{want[k] if k < len(want) else 'none'}"))
    if fails:
        return Result("fail", tuple(sorted({f[0] for f in fails})), "; ".join(f"{o}: {d}" for o, d in fails))
    return Result("ok", ())


def simplifications(case: Case):
    """Smaller cases, most reduction first: fewer frames, simpler frames, then nominal clocks."""
    r, env = case.recipe, case.env
    fr = r.frames
    for i in range(len(fr)):
        yield replace(case, recipe=replace(r, frames=fr[:i] + fr[i + 1:]))
    for i, f in enumerate(fr):
        for g in (replace(f, prep=()), replace(f, skip_on=None), replace(f, n=1), replace(f, src="zero"),
                  replace(f, dst="discard"), replace(f, msb=False), replace(f, wait=None)):
            if g != f:
                yield replace(case, recipe=replace(r, frames=fr[:i] + (g,) + fr[i + 1:]))
    if r.loops > 1:
        yield replace(case, recipe=replace(r, loops=1))
    if env.late is not None:
        yield replace(case, env=replace(env, late=None))
    for i in range(len(env.clocks)):
        if env.clocks[i] != (1, 1):
            yield replace(case, env=replace(env, clocks=env.clocks[:i] + ((1, 1),) + env.clocks[i + 1:]))


def shrink(case: Case, budget: int = 400) -> Case:
    """Greedy, to a fixpoint: take the first simplification that still fails the same oracles."""
    want = run_case(case).rules
    tries = 0
    progress = True
    while progress and tries < budget:
        progress = False
        for c in simplifications(case):
            tries += 1
            res = run_case(c)
            if res.verdict == "fail" and res.rules == want:
                case, progress = c, True
                break
            if tries >= budget:
                break
    return case


@dataclass
class Report:
    runs: int = 0
    accepted: int = 0
    rejected: dict[str, int] = None
    structural: list[Case] = None
    failures: list[tuple[Case, Result]] = None

    def __post_init__(self):
        self.rejected = self.rejected or {}
        self.structural = self.structural or []
        self.failures = self.failures or []


def underrun_free(r: Recipe) -> bool:
    """No XFER that sends from the FIFO without owning the clock: with a late host such a step sends
    ONES and logs UNDERRUN, as specified (isa §4.3), which the value oracle can't predict."""
    return not any(f.kind == "clocked" and f.src == "fifo" and r.lines[r.lane.clk].owner != f.owner
                   for f in r.frames)


def campaign(seed: int, iters: int, envs: int = 2) -> Report:
    """`iters` random recipes from `seed`, each accepted one in `envs` random environments."""
    rng = random.Random(seed)
    rep = Report()
    for _ in range(iters):
        r = random_recipe(rng.randrange(1 << 30))
        img, rejected = compile_recipe(r)
        if rejected is not None:
            if rejected.verdict == "structural":
                rep.structural.append(Case(r, Env.draw(rng, r.ppm)))
            for rule in rejected.rules:
                rep.rejected[rule] = rep.rejected.get(rule, 0) + 1
            continue
        rep.accepted += 1
        for _ in range(envs):
            case = Case(r, Env.draw(rng, r.ppm, late_ok=underrun_free(r)))
            rep.runs += 1
            res = run_case(case)
            if res.verdict == "fail":
                rep.failures.append((case, res))
                break
    return rep
