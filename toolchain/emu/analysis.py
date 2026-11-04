"""Static checks over the lowered program: branch taint (R2) and timing rules (T1–T5).

Formulas are docs/design/isa-v0.md §6. All arithmetic is integer (ppm), so
boundary cases don't depend on floating point.
"""
from __future__ import annotations

from dataclasses import dataclass

from .compiler import Diagnostic, Lowered
from .image import Profile
from .isa import (
    EDGE_LAT as E, F_ABORT, F_CHK, F_REG, INF, NUM_REGS, QUANT as Q, BrCond, Dst, Instr, Op, Owner, RawOp, Role,
    unpack_role_if, unpack_xfer,
)

ROLES = (Role.P0, Role.P1, Role.MON)
BIG = 1 << 40            # "unbounded" accumulated window / gap
A_CAP = 1 << 22          # anchor distance beyond which a loop counts as unanchored
SET_CAP = 64             # distinct (A, LO, HI) tuples tracked per pc
PATH_CAP = 4096          # simple paths explored between two timed steps


def drift(ppm: int, x: int) -> int:
    """ceil(ppm * x / 1e6) for x >= 0."""
    return -(-ppm * max(0, x) // 1_000_000)


# ----------------------------------------------------------------------------- CFG
def successors(instrs: list[Instr], pc: int, role: Role | None = None) -> list[int]:
    """Successor pcs. With `role`, ROLE_IF follows that role's path only."""
    ins = instrs[pc]
    nxt = [pc + 1] if pc + 1 < len(instrs) else []
    if ins.op == Op.HALT:
        return []
    if ins.op == Op.BR:
        if BrCond(ins.prof & 3) == BrCond.ALWAYS:
            return [ins.operand]
        return nxt + [ins.operand]
    if ins.op == Op.RAW and ins.prof == RawOp.JPIN:
        return nxt + [ins.operand & 0x7FF]
    if ins.op == Op.ROLE_IF:
        mask, count = unpack_role_if(ins.operand)
        skip = pc + 1 + count
        skip_l = [skip] if skip < len(instrs) else []
        if role is None:
            return nxt + [s for s in skip_l if s not in nxt]
        return nxt if mask >> int(role) & 1 else skip_l
    return nxt


def _in_block(low: Lowered, pc: int) -> bool:
    return any(sp.start <= pc < sp.end for sp in low.spans)


class _Diags:
    def __init__(self, desc, low: Lowered):
        self.desc, self.low, self.out = desc, low, {}

    def add(self, rule: str, pc: int, msg: str) -> None:
        if (rule, pc) in self.out:
            return
        step = self.desc.steps[self.low.pc_step[pc]] if self.desc.steps else None
        self.out[(rule, pc)] = Diagnostic(rule, step.index if step else 0, step.loc if step else "?",
                                          f"pc {pc}: {msg}")

    def list(self) -> list[Diagnostic]:
        return list(self.out.values())


# ----------------------------------------------------------------------------- R2
def check_branches(desc, low: Lowered) -> list[Diagnostic]:
    instrs = low.instrs
    diags = _Diags(desc, low)
    if not instrs:
        return []
    img = low.image
    od_lanes = {i for i, la in enumerate(img.lanes) if img.lines[la.data].kind == "od"}
    abort_pc = img.abort_pc if any(_aborts(ins) for ins in instrs) else None
    all_tainted = (1 << NUM_REGS) - 1
    state: dict[int, int] = {0: 0}
    work = [0]
    while work:
        pc = work.pop()
        t = state[pc]
        ins = instrs[pc]
        inblock = _in_block(low, pc)
        cond = BrCond(ins.prof & 3) if ins.op == Op.BR else None
        if cond == BrCond.TO and not inblock:
            diags.add("R2-branch", pc, "BR TO outside a role block")
        elif cond in (BrCond.Z, BrCond.NZ) and not inblock and t >> (ins.pin & 3) & 1:
            diags.add("R2-branch", pc, f"branch on r{ins.pin & 3}, whose value can differ between roles")
        if ins.op == Op.RAW and ins.prof == RawOp.JPIN and not inblock:
            diags.add("R2-branch", pc, "raw pin branch outside a role block")
        out = _taint(ins, t, inblock, od_lanes)
        edges = [(s, out) for s in successors(instrs, pc)]
        if abort_pc is not None and _aborts(ins) and abort_pc < len(instrs):
            # only the roles that fail jump: every register may now differ between roles
            edges.append((abort_pc, all_tainted))
        for s, o in edges:
            old = state.get(s)
            new = o if old is None else old | o
            if new != old:
                state[s] = new
                work.append(s)
    return diags.list()


def _aborts(ins: Instr) -> bool:
    return ins.op in (Op.COND, Op.HOLD, Op.XFER) and bool(ins.flags & F_ABORT)


def _taint(ins: Instr, t: int, inblock: bool, od_lanes: frozenset[int] | set[int] = frozenset()) -> int:
    def put(reg: int, tainted: bool) -> int:
        return t | (1 << reg) if tainted else t & ~(1 << reg)

    if ins.op == Op.LI:
        return put(ins.pin & 3, inblock)
    if ins.op == Op.ALU:
        ra, rd = ins.pin >> 2 & 3, ins.pin & 3
        tainted = inblock or bool(t >> ra & 1) or bool(ins.flags & F_REG and t >> (ins.operand & 3) & 1)
        return put(rd, tainted)
    if ins.op == Op.POP:
        return put(ins.pin & 3, True)
    if ins.op == Op.RAW and ins.prof == RawOp.GET:
        return put(ins.operand & 3, True)
    if ins.op == Op.XFER:
        dst = unpack_xfer(ins.operand)[2]
        if dst >= Dst.R0:
            # on an open-drain line the owner stores what it sent, the followers what they read
            return put(int(dst) - 4, inblock or ins.pin in od_lanes)
    return t


# ----------------------------------------------------------------------------- timing
@dataclass(frozen=True)
class _Step:
    pc: int
    op: Op
    owner: Owner
    prof: Profile
    n: int = 1
    clocked: bool = False
    clk_owner: Role | None = None
    cpha: int = 0
    chk: bool = False
    abort: bool = False

    def does(self, r: Role) -> bool:
        return r != Role.MON and (self.owner == Owner.SHARED or int(self.owner) == int(r))

    def is_clk_owner(self, r: Role) -> bool:
        return self.clocked and r != Role.MON and self.clk_owner == r

    @property
    def owner_role(self) -> Role | None:
        return Role(int(self.owner)) if self.owner in (Owner.P0, Owner.P1) else None


def _steps(low: Lowered) -> dict[int, _Step]:
    img = low.image
    out = {}
    for pc, ins in enumerate(low.instrs):
        if ins.op not in (Op.COND, Op.HOLD, Op.XFER):
            continue
        prof = img.profiles[ins.prof] if ins.prof < len(img.profiles) else img.profiles[0]
        kw = {"abort": bool(ins.flags & F_ABORT)}
        if ins.op == Op.XFER:
            kw["n"] = unpack_xfer(ins.operand)[0]
            kw["chk"] = bool(ins.flags & F_CHK)
            lane = img.lanes[ins.pin]
            if lane.clk is not None:
                kw.update(clocked=True, cpha=lane.cpha, clk_owner=Role(int(img.lines[lane.clk].owner)))
        out[pc] = _Step(pc, ins.op, ins.owner, prof, **kw)
    return out


def completion(s: _Step | None, r: Role) -> int:
    """Cycle (relative to the step's end tb) at which role r finishes step s."""
    if s is None:  # reset: everyone starts executing pc 0 at cycle 0
        return -1
    nom = s.prof.nom
    if s.op == Op.COND:
        return 0 if s.does(r) else E
    if s.op == Op.HOLD:
        return -nom if s.does(r) else -nom + nom // 2 + E
    if not s.clocked:
        return -nom if s.does(r) else -nom + nom // 2 + E
    if s.is_clk_owner(r):
        if s.does(r):
            return 0
        return max(0, E - nom) if s.cpha == 0 else E
    return E


def _cost(ins: Instr) -> int:
    if ins.op == Op.RAW and ins.prof == RawOp.DELAY:
        return max(1, ins.operand)
    if ins.op == Op.ROLE_IF:
        return 1
    return 1


def gaps(instrs: list[Instr], timed: set[int], start: int, role: Role) -> dict[int, tuple[int, int]]:
    """Untimed cycles role spends from `start` until each reachable timed pc: {pc: (min, max)}."""
    found: dict[int, tuple[int, int]] = {}
    cyclic = False
    paths = 0
    stack = [(start, 0, frozenset())]
    while stack:
        pc, g, seen = stack.pop()
        if pc >= len(instrs):
            continue
        if pc in timed:
            lo, hi = found.get(pc, (g, g))
            found[pc] = (min(lo, g), max(hi, g))
            paths += 1
            if paths > PATH_CAP:
                cyclic = True
                break
            continue
        if pc in seen:
            cyclic = True
            continue
        for s in successors(instrs, pc, role):
            stack.append((s, g + _cost(instrs[pc]), seen | {pc}))
    if cyclic:
        found = {pc: (lo, BIG) for pc, (lo, _) in found.items()}
    return found


def _anchor_states(low: Lowered, steps: dict[int, _Step], diags: _Diags) -> dict[int, set[tuple[int, int, int]]]:
    instrs = low.instrs
    state: dict[int, set[tuple[int, int, int]]] = {0: {(0, 0, 0)}}
    work = [0]
    while work:
        pc = work.pop()
        out = {_advance(steps.get(pc), t) for t in state[pc]}
        for s in successors(instrs, pc):
            cur = state.setdefault(s, set())
            new = out - cur
            if not new:
                continue
            if any(t[0] > A_CAP for t in new) or len(cur) + len(new) > SET_CAP:
                diags.add("T1-unanchored", s, "the timeline is never re-anchored around this loop; "
                                              "add a followed COND or a clocked XFER")
                continue
            cur |= new
            work.append(s)
    return state


def _advance(s: _Step | None, t: tuple[int, int, int]) -> tuple[int, int, int]:
    if s is None:
        return t
    if s.op == Op.COND or s.clocked:
        return (0, 0, 0)
    a, lo, hi = t
    p = s.prof
    hi_add = BIG if p.max == INF else s.n * p.max
    return (a + s.n * p.nom, lo + s.n * p.min, min(BIG, hi + hi_add))


def check_timing(desc, low: Lowered) -> list[Diagnostic]:
    instrs = low.instrs
    if not instrs:
        return []
    ppm = low.image.tolerance_ppm
    diags = _Diags(desc, low)
    steps = _steps(low)
    states = _anchor_states(low, steps, diags)
    timed = set(steps)

    for pc, s in steps.items():
        p = s.prof
        tuples = states.get(pc, set())
        # T4: profile sanity
        if not 0 <= p.min <= p.nom <= p.max <= INF:
            diags.add("T4-profile", pc, f"profile {p.name}: need 0 <= min <= nom <= max")
        if s.op in (Op.HOLD, Op.XFER) and p.nom < 1:
            diags.add("T4-profile", pc, f"{s.op.name} needs a profile with nom >= 1 (got {p.name})")
            continue
        if s.clocked and p.nom < 2:
            diags.add("T4-profile", pc, "clocked XFER needs a half period >= 2")
            continue
        # T1: follower sample points
        if s.op == Op.HOLD or (s.op == Op.XFER and not s.clocked):
            P = p.nom
            for a, lo, hi in tuples:
                dr = drift(ppm, a + s.n * P)
                if dr + Q + 1 >= P // 2:
                    diags.add("T1-sample", pc, f"last sample of slot length {P} at anchor distance {a}: "
                                               f"needs drift {dr} + QUANT {Q} + 1 = {dr + Q + 1} < P//2 = {P // 2} "
                                               f"({ppm} ppm)")
        # T2: accumulated windows of CHK unclocked XFER
        if s.op == Op.XFER and not s.clocked and s.chk:
            for a, lo, hi in tuples:
                for j in range(s.n):
                    nominal = a + j * p.nom
                    m = drift(ppm, nominal) + Q
                    wlo, whi = lo + j * p.min, min(BIG, hi + (BIG if p.max == INF else j * p.max))
                    if wlo > nominal - m or whi < nominal + m:
                        hint = ("; a CHK XFER that starts right at an anchor has no window for its first "
                                "edge (isa-v0 §6, D2): drop check on this step, or put a timed step such "
                                "as a start-bit HOLD first") if a == 0 and j == 0 else ""
                        diags.add("T2-window", pc, f"edge window [{wlo},{whi}] at boundary {j} does not "
                                                   f"contain {nominal}±{m}{hint}")
                        break
        # T2 for clocked CHK (clock phase) and T3
        if s.clocked:
            H = p.nom
            m = drift(ppm, H) + Q
            if s.chk and (p.min > H - m or (p.max != INF and p.max < H + m)):
                diags.add("T2-window", pc, f"clock-phase window [{p.min},{p.max}] does not contain {H}±{m}")
            if H - drift(ppm, H) < 2:
                diags.add("T3-latency", pc, f"half period {H} too short for a clock follower")
            if s.owner_role is not None and s.owner_role != s.clk_owner and H < E + 1 + drift(ppm, 2 * H):
                diags.add("T3-latency", pc, f"half period {H} < EDGE_LAT + 1 + drift: a data owner that "
                                            f"follows the clock cannot launch in time")

    # an abort restarts only the roles that fail: they must re-sync on the other party's next event
    aborting = {r for s in steps.values() if s.abort for r in ROLES if not s.does(r)}
    apc = low.image.abort_pc
    for r in sorted(aborting):
        for pc in gaps(instrs, timed, apc, r):
            s = steps[pc]
            if not (s.op == Op.COND and not s.prof.bounded and not s.does(r)):
                diags.add("T5-abort", pc, f"{r.name} can abort and restart at pc {apc}; the first timed "
                                          f"step it reaches there must be an unbounded COND it follows, "
                                          f"so it waits for the other party's next event")

    # pairwise checks: predecessor timed step (or reset) -> next timed step
    preds: list[tuple[_Step | None, int]] = [(None, 0)] + [(s, pc + 1) for pc, s in steps.items()]
    for prev, start in preds:
        per_role = {r: gaps(instrs, timed, start, r) for r in ROLES}
        for nxt_pc in set().union(*per_role.values()):
            s = steps[nxt_pc]
            if (prev is not None and prev.op == Op.COND and prev.prof.bounded and not prev.abort
                    and not (s.op == Op.COND and not s.prof.bounded)):
                late = prev.prof.max - prev.prof.nom + Q + E + 1
                diags.add("T5-timeout", prev.pc, f"a follower that times out here finishes up to {late} "
                                                 f"cycles late, off the owner's timeline; the next timed "
                                                 f"step (pc {nxt_pc}) must be an unbounded COND, or add abort")
            if s.owner_role is None:
                continue  # SHARED steps: Phase 4 (ARB)
            st = {}
            for r in ROLES:
                if nxt_pc in per_role[r]:
                    lo, hi = per_role[r][nxt_pc]
                    c = completion(prev, r)
                    st[r] = (c + 1 + lo, c + 1 + hi)
            if s.owner_role not in st:
                continue
            # anchor distance along this edge only (untimed instructions don't move it)
            a_edge = 0 if prev is None else max(
                (_advance(prev, t)[0] for t in states.get(prev.pc, set())), default=0)
            _pair_checks(s, st, a_edge, ppm, diags)
    return diags.list()


def _pair_checks(s: _Step, st: dict[Role, tuple[int, int]], amax: int, ppm: int, diags: _Diags) -> None:
    p = s.prof
    pc = s.pc
    o = s.owner_role
    o_lo, o_hi = st[o]
    followers = [r for r in st if not s.does(r)]
    if s.op == Op.COND:
        offset = p.nom
        if p.bounded and o_hi > max(offset, 1):
            diags.add("T5-overrun", pc, f"owner starts at {o_hi}, after the planned action at {offset}")
        event = max(p.nom, o_lo)
        dr = drift(ppm, amax + p.nom)
        for r in followers:
            if st[r][1] + dr > event + E:
                diags.add("T5-watch", pc, f"{r.name} starts watching at {st[r][1]} but the event is "
                                          f"visible at {event + E} (drift {dr})")
        if p.bounded or p.min > 0:
            late = max(p.nom, o_hi)
            m_lo, m_hi = event - dr - Q, late + dr + Q
            # the engine checks EARLY only when min > 0 (isa-v0 §4.1)
            if (p.min > 0 and p.min > m_lo) or (p.bounded and p.max < m_hi):
                diags.add("T2-window", pc, f"window [{p.min},{p.max}] does not contain [{m_lo},{m_hi}]")
        return
    # HOLD / XFER: the owner's first action is at offset 0 (issue slot allows 1)
    if s.clocked:
        c_hi = st.get(s.clk_owner, (0, 0))[1]
        if c_hi > 1:
            diags.add("T5-overrun", pc, f"clock owner starts at {c_hi}, after the step start")
        first_obs = p.nom + E  # first leading edge, as seen by a clock follower
        dr = drift(ppm, amax + p.nom)
        for r in st:
            if not s.is_clk_owner(r) and st[r][1] + dr > first_obs:
                diags.add("T5-watch", pc, f"{r.name} starts at {st[r][1]}, after the first clock edge "
                                          f"is visible at {first_obs}")
        if o != s.clk_owner and s.cpha == 0:
            # bit 0 is launched when the data owner starts the step
            if p.nom < o_hi + 1 + drift(ppm, amax + p.nom):
                diags.add("T3-latency", pc, f"data owner starts at {o_hi}; bit 0 is not valid before the "
                                            f"first capture edge at {p.nom}")
        return
    if o_hi > 1:
        diags.add("T5-overrun", pc, f"owner starts at {o_hi}, after the step start")
    sample = p.nom // 2 + E
    dr = drift(ppm, amax + p.nom)
    for r in followers:
        if st[r][1] + dr > sample:
            diags.add("T5-watch", pc, f"{r.name} starts at {st[r][1]}, after its first sample at {sample}")
