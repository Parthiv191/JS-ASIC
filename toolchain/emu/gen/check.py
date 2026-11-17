"""Run a random program in every role and check it: the rule cross-check (verification.md §1).

A case is a recipe plus its environment: each engine's clock and phase and the host words. The
compiler either rejects the program (a timing rule: recorded, not a failure; any other rule: a
generator bug) or accepts it, and then on the model, with every clock within the description's
tolerance and no glitches, these must hold:

| Oracle | Fails when |
|---|---|
| `violation` | an engine logs a violation (isa §7: OVERRUN, EARLY, TIMEOUT, LEVEL, EDGE, ...) |
| `contention` | two drivers fight on a wire |
| `completion` | an engine doesn't halt, or the run isn't quiescent, by the tick budget, or a party halts early where the recipe has it run to the end, or the reverse (`expected_halts`) |
| `agreement` | the engines' timed steps (pc, op, XFER value), wire registers r1-r4 or RX words differ |
| `value` | an XFER's value isn't what the recipe says (`expected_values`: src, host words, prep ops), so wrong semantics that every role shares fail too |
| `bus` | on an I2C bus, an SDA edge with SCL high on the wire that no COND START/STOP made, or a follower's detector reporting a START or STOP that its COND START/STOP steps don't match (isa §4.7, §5 R1-bus) |

A party that halts in its own block (the bus family) takes part in every step up to its HALT, so
its steps are a prefix of MON's, which runs them all. With lead blocks (the bus family again), the
steps in role blocks are the leader's alone, and a party that took an exit, or waited one out in
the successor (a bus exit), skips some of MON's: outside the blocks, each party's steps are then a
subsequence of MON's. The path then depends on the exits, which `expected_values` doesn't follow,
so `value` sits those out. A lead block's leader whose exit follows the master's bus COND may end
waiting in that COND, or at abort_pc's first step after it, once the master has halted with no
such COND made since: a target waiting for a controller that is done (isa §5 R3-lead), which
`completion` takes as an end. And `bus` lets a detector report a STOP that a followed START
ignored, or a START that a followed STOP without ABORT did (§4.2), in that step.
"""
from __future__ import annotations

import json
import random
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace

from ..compiler import CompileError, compile_desc
from ..isa import F_ABORT, BusKind, CondKind, Dst, Op, Role, unpack_cond, unpack_role_if, unpack_xfer
from ..model.engine import VIOLATIONS
from ..model.system import EngineSpec, System
from .randdesc import (HOST_WORDS, PARTIES, Recipe, build, expected_halts, expected_values, random_bus_recipe,
                       random_recipe)

ROLES = (Role.P0, Role.P1, Role.MON)
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
    """(image, None), or (None, Result) when the compiler rejects it. The walks that check levels
    (R2-edge, R1-bus's) and timing (T1-T5) are left to the compiler; a program that fails without
    them is a generator bug ("structural")."""
    d = build(r)
    try:
        return compile_desc(d), None
    except CompileError as e:
        rules = tuple(sorted({x.rule for x in e.diags}))
        try:
            compile_desc(d, check_levels=False, check_timing=False)
            verdict = "rejected"
        except CompileError as st:
            # lead blocks are drawn with their exits at random, for R3-lead to judge
            verdict = ("rejected" if r.abort is not None and {x.rule for x in st.diags} <= {"R3-lead"}
                       else "structural")
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
    instrs = img.instrs()
    lead = case.recipe.abort is not None
    if not (t.quiescent and all(et.halted or lead and waits_for_master(img, t, et) for et in t.engines)):
        fails.append(("completion", "not halted: " + ", ".join(et.role.name for et in t.engines if not et.halted)))
    blocks = {q for pc, ins in enumerate(instrs) if ins.op == Op.ROLE_IF
              for q in range(pc + 1, pc + 1 + unpack_role_if(ins.operand)[1])} if lead else set()
    steps = [[(r.pc, r.op, r.value if r.op == "XFER" else None) for r in et.records if r.pc not in blocks]
             for et in t.engines]
    last = len(instrs) - 1                                       # the final HALT
    full = [bool(et.starts) and et.starts[-1][0] == last for et in t.engines]
    ref = steps[2]                                               # MON runs every timed step
    for i, et in enumerate(t.engines[:2]):
        st = steps[i]
        if lead and not _subsequence(st, ref):
            fails.append(("agreement", f"{et.role.name}'s steps outside the lead blocks aren't a "
                                       f"subsequence of MON's: {st[:12]} vs {ref[:12]}"))
            break
        if not lead and st != ref and (full[i] or ref[:len(st)] != st):
            k = next((j for j, (x, y) in enumerate(zip(ref, st)) if x != y), min(len(st), len(ref)))
            fails.append(("agreement", f"{et.role.name} vs MON at step {k}: "
                                       f"{st[k] if k < len(st) else 'end'} vs {ref[k] if k < len(ref) else 'end'}"))
            break
    else:
        for i, et in enumerate(t.engines[:2]):  # (with lead blocks every register is tainted at abort_pc, R2)
            if full[i] and not lead and et.regs[1:5] != t.engines[2].regs[1:5]:
                fails.append(("agreement", f"{et.role.name} vs MON: r1-r4 {et.regs[1:5]} vs {t.engines[2].regs[1:5]}"))
                break
    for et in t.engines:  # dst = FIFO pushes what a follower received (isa §4.3); the owner pushes nothing
        want = [r.value for r in et.records if r.op == "XFER" and r.mode == "follow"
                and unpack_xfer(instrs[r.pc].operand)[2] == Dst.FIFO]
        if et.rx != want:
            fails.append(("agreement", f"{et.role.name}: rx {et.rx}, but it followed {want} into the FIFO"))
    early = expected_halts(case.recipe)
    for i, (et, p) in enumerate(zip(t.engines[:2], PARTIES)):
        if et.halted and full[i] == (p in early):
            fails.append(("completion", f"{et.role.name} {'ran to the end' if full[i] else 'halted early'}, "
                                        f"but the recipe has it {'halt in its block' if full[i] else 'run to the end'}"))
    want = expected_values(case.recipe, dict(zip(PARTIES, (list(w) for w in env.words))))
    got = [r.value for r in t.engines[0 if full[0] else 2].records if r.op == "XFER"]
    if not lead and got != want:
        k = next((i for i, (x, y) in enumerate(zip(got, want)) if x != y), min(len(got), len(want)))
        fails.append(("value", f"XFER {k}: {got[k] if k < len(got) else 'none'}, the recipe says "
                                 f"{want[k] if k < len(want) else 'none'}"))
    if img.bus.kind == BusKind.I2C:
        fails += [("bus", m) for m in bus_mismatches(img, t, ignores=lead)]
    if fails:
        return Result("fail", tuple(sorted({f[0] for f in fails})), "; ".join(f"{o}: {d}" for o, d in fails))
    return Result("ok", ())


def _subsequence(a: list, b: list) -> bool:
    rest = iter(b)
    return all(x in rest for x in a)


def _lead_blocks(instrs) -> list[tuple[int, int, int]]:
    """(ROLE_IF pc, body end, role mask) of the role blocks that hold a timed step: lead blocks."""
    out = []
    for pc, ins in enumerate(instrs):
        if ins.op == Op.ROLE_IF:
            mask, n = unpack_role_if(ins.operand)
            if any(instrs[q].op in (Op.COND, Op.HOLD, Op.XFER) for q in range(pc + 1, pc + 1 + n)):
                out.append((pc, pc + 1 + n, mask))
    return out


def _wire_bus_events(img, t) -> list[tuple[str, int]]:
    """The START/STOPs on an I2C wire: SDA edges with SCL high (the tick before and at it)."""
    sda, scl, edges = img.bus.a, img.bus.b, t.wire_edges

    def level(line: int, tick: int) -> int:  # OD lines, released at RUN: 1 until the first edge
        return 1 ^ (bisect_right(edges[line], tick) & 1)
    return [("START" if level(sda, tk) == 0 else "STOP", tk) for tk in edges[sda]
            if level(scl, tk) == 1 and level(scl, tk - 1) == 1]


def waits_for_master(img, t, et) -> bool:
    """`et`, a lead block's leader whose exit follows the master's bus COND, ends waiting in that
    COND, or in abort_pc's first step after it (an unbounded COND of the master's it follows,
    T5-abort), with the master halted and no such COND made on the wire since that step started
    (a START/STOP of that kind; for a FALL/RISE, an edge of its line to that level)."""
    instrs, master = img.instrs(), int(img.lines[img.bus.b].owner)
    if (not et.starts or et.role == Role.MON or int(et.role) == master or not t.engines[master].halted
            or img.bus.kind != BusKind.I2C):
        return False
    pc, cyc = et.starts[-1]
    ins = instrs[pc]
    if ins.op != Op.COND or int(ins.owner) != master:
        return False
    kind = CondKind(unpack_cond(ins.operand)[0])
    mine = [(lo, hi) for lo, hi, mask in _lead_blocks(instrs) if mask == 1 << int(et.role)]
    follows = any(instrs[q].op == Op.COND and unpack_cond(instrs[q].operand)[0] in (CondKind.START, CondKind.STOP)
                  and int(instrs[q].owner) == master for lo, hi in mine for q in range(lo, hi))
    first = next((q for q in range(img.abort_pc, len(instrs)) if instrs[q].op in (Op.COND, Op.HOLD, Op.XFER)),
                 None)
    in_exit = kind in (CondKind.START, CondKind.STOP) and any(lo <= pc < hi for lo, hi in mine)
    if not (in_exit or follows and pc == first):
        return False
    since = et.cycle_tick[min(max(cyc - 1, 0), len(et.cycle_tick) - 1)] if et.cycle_tick else 0
    if kind in (CondKind.START, CondKind.STOP):
        return not any(k == kind.name and tk >= since for k, tk in _wire_bus_events(img, t))
    ln = img.lines[ins.line]
    init, level = (1 if ln.kind == "od" else ln.idle), (0 if kind == CondKind.FALL else 1)
    return not any(tk >= since and init ^ ((k + 1) & 1) == level for k, tk in enumerate(t.wire_edges[ins.line]))

def bus_mismatches(img, t, ignores: bool = False) -> list[str]:
    """The `bus` oracle on an I2C bus. On the wire, an SDA edge while SCL is high (the tick before
    and at it) is a START (SDA falls) or a STOP (rises) to every device on the bus, so each must be
    one of the COND START/STOPs that SCL's owner made (isa §5 R1-bus): a stray one is a failure
    even where the engines' detectors happen to miss it (SCL fell within their confirm window). And
    each follower's detector must report exactly the STARTs and STOPs its COND START/STOPs took,
    in order (a halted engine detects nothing more), but for one flag per bus exit, which takes a
    flag with no COND: then what they took is in what it saw, in order, with one more for each
    exit (the extra one's kind and place aren't checked, and a COND that takes an exit's flag, as
    abort_pc's COND START may on a repeated START, would read as a mismatch: no generated program
    has one). Kinds and order only: a run where a STOP goes missing and
    a stray one comes later matches (the followers then hang on the first, which `completion`
    sees)."""
    instrs, scl = img.instrs(), img.bus.b

    def ignored(et, cycle: int, kind: str) -> bool:
        k = bisect_right([c for _, c in et.starts], cycle) - 1
        if k < 0:
            return False
        ins = instrs[et.starts[k][0]]
        if ins.op != Op.COND:
            return False
        ck = CondKind(unpack_cond(ins.operand)[0])
        return (ck == CondKind.START and kind == "STOP"
                or ck == CondKind.STOP and kind == "START" and not ins.flags & F_ABORT)

    def bus_conds(et) -> list[str]:
        return [k.name for r in et.records if r.op == "COND"
                and (k := unpack_cond(instrs[r.pc].operand)[0]) in (CondKind.START, CondKind.STOP)]

    out = []
    wire = _wire_bus_events(img, t)
    master = next(et for et in t.engines if int(et.role) == int(img.lines[scl].owner))
    made = bus_conds(master)
    if [k for k, _ in wire] != made:
        k = next((i for i, (x, y) in enumerate(zip(wire, made)) if x[0] != y), min(len(wire), len(made)))
        out.append(f"wire: SDA edge {k} with SCL high is "
                   f"{f'a {wire[k][0]} at tick {wire[k][1]}' if k < len(wire) else 'missing'}, "
                   f"the controller's COND made {made[k] if k < len(made) else 'none'}")
    for et in t.engines:
        if et is master:
            continue  # the master line's owner runs no detector (isa §4.7)
        # `ignores` (lead blocks): a detector event that came while the engine was in a followed
        # START, a STOP, or in a followed STOP without ABORT, a START, is that step's to ignore (§4.2)
        seen = [k for c, k in et.bus_events if not (ignores and ignored(et, c, k))]
        took = bus_conds(et)
        exits = sum(e.kind == "BUS" for e in et.events)
        rest = iter(seen)
        if len(seen) != len(took) + exits or not all(k in rest for k in took):
            out.append(f"{et.role.name}: its detector saw {seen}, its COND START/STOPs took {took}"
                       + (f" and {exits} bus exits" if exits else ""))
    return out


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


FAMILIES = {"lines": random_recipe, "i2c": random_bus_recipe}


def campaign(seed: int, iters: int, envs: int = 2, family: str = "lines") -> Report:
    """`iters` random recipes of `family` from `seed`, each accepted one in `envs` random
    environments."""
    rng = random.Random(seed)
    rep = Report()
    for _ in range(iters):
        r = FAMILIES[family](rng.randrange(1 << 30))
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
