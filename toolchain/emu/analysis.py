"""Static checks over the lowered program: branch taint (R2) and timing rules (T1–T5).

Formulas are docs/design/isa-v0.md §6. All arithmetic is integer (ppm), so
boundary cases don't depend on floating point.
"""
from __future__ import annotations

from dataclasses import dataclass

from .compiler import Diagnostic, Lowered
from .image import Profile
from .isa import (
    EDGE_LAT as E, F_ABORT, F_CHK, F_REG, INF, ISSUE, NUM_REGS, QUANT as Q, U, BrCond, Dst, Instr, Op, Owner,
    RawOp, Role, Src,
    reg_of, unpack_role_if, unpack_xfer,
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
        if BrCond(ins.sub) == BrCond.ALWAYS:
            return [ins.operand]
        return nxt + [ins.operand]
    if ins.op == Op.RAW and ins.sub == RawOp.JPIN:
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
    state: dict[int, int] = {0: all_tainted}  # registers keep their values across RUN (isa §1)
    work = [0]
    while work:
        pc = work.pop()
        t = state[pc]
        ins = instrs[pc]
        inblock = _in_block(low, pc)
        cond = BrCond(ins.sub) if ins.op == Op.BR else None
        if cond == BrCond.TO and not inblock:
            diags.add("R2-branch", pc, "BR TO outside a role block")
        elif cond in (BrCond.Z, BrCond.NZ) and not inblock and t >> ins.ra & 1:
            diags.add("R2-branch", pc, f"branch on r{ins.ra}, whose value can differ between roles")
        if ins.op == Op.RAW and ins.sub == RawOp.JPIN and not inblock:
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
        return put(ins.rd, inblock)
    if ins.op == Op.ALU:
        tainted = inblock or bool(t >> ins.ra & 1) or bool(ins.flags & F_REG and t >> (ins.operand & 7) & 1)
        return put(ins.rd, tainted)
    if ins.op == Op.POP:
        return put(ins.rd, True)
    if ins.op == Op.RAW and ins.sub in (RawOp.GET, RawOp.EVT):
        return put(ins.rd, True)
    if ins.op == Op.XFER:
        dst = unpack_xfer(ins.operand)[2]
        if dst >= Dst.R0:
            # on an open-drain line the wire can differ from what the owner sent
            return put(reg_of(dst), inblock or ins.line in od_lanes)
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
    od: bool = False  # an XFER whose data line is open-drain: its owner reads the bits back
    d2_owner: Role | None = None  # duplex: the party that drives data2 (isa §4.3)
    ldelay: int = 0

    def does(self, r: Role) -> bool:
        return r != Role.MON and (self.owner == Owner.SHARED or int(self.owner) == int(r))

    def is_clk_owner(self, r: Role) -> bool:
        return self.clocked and r != Role.MON and self.clk_owner == r

    @property
    def owner_role(self) -> Role | None:
        return Role(int(self.owner)) if self.owner in (Owner.P0, Owner.P1) else None

    @property
    def data_owners(self) -> set[Role]:
        """The parties that drive a data line of the step: data's owner, and data2's."""
        return {r for r in (self.owner_role, self.d2_owner) if r is not None}

    def drives_all(self, r: Role) -> bool:
        """r drives every data line of the step, so it follows none."""
        return self.does(r) and self.d2_owner is None


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
            lane = img.lanes[ins.line]
            kw["od"] = any(x is not None and img.lines[x].kind == "od" for x in (lane.data, lane.data2))
            if lane.clk is not None:
                kw.update(clocked=True, cpha=lane.cpha, clk_owner=Role(int(img.lines[lane.clk].owner)),
                          ldelay=lane.ldelay)
                if lane.data2 is not None and img.lines[lane.data2].owner in (Owner.P0, Owner.P1):
                    kw["d2_owner"] = Role(int(img.lines[lane.data2].owner))
        out[pc] = _Step(pc, ins.op, ins.owner, prof, **kw)
    return out


def completion(s: _Step, r: Role) -> tuple[int, int]:
    """Cycles (lo, hi), relative to the step's end tb, in which role r finishes step s (isa §6).

    An owner's HOLD drives, and completes, when it starts, which is up to ISSUE late (isa §4.2;
    T5-overrun bounds the start). So does a 1-bit unclocked XFER; with more bits the last one is
    planned past ISSUE, so the lateness is absorbed."""
    nom = s.prof.nom
    if s.op == Op.COND:
        c = 0 if s.does(r) else E
        return c, c
    if s.op == Op.HOLD or not s.clocked:
        if s.does(r) and not s.od:  # an OD owner reads back, so it finishes as a follower does
            return -nom, max(ISSUE - s.n * nom, -nom)
        c = -nom + nom // 2 + E
        return c, c
    if s.is_clk_owner(r):
        # it completes at its last edge, or at its last sample if it follows a data line
        # (an OD owner reads its bits back, so it completes like a follower, isa §6)
        c = 0 if s.drives_all(r) and not s.od else (max(0, E - nom) if s.cpha == 0 else E)
        return c, c
    return E, E


def _cost(ins: Instr) -> int:
    """Cycles an untimed instruction adds to `L` (isa §4.0, §6): U, and RAW DELAY n rounded up to a
    multiple of U. POP counts U; a stall is T5-stall's job."""
    if ins.op == Op.RAW and ins.sub == RawOp.DELAY:
        return -(-max(1, ins.operand) // U) * U
    return U


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
                if dr + Q + ISSUE >= P // 2:
                    diags.add("T1-sample", pc, f"last sample of slot length {P} at anchor distance {a}: "
                                               f"needs drift {dr} + QUANT {Q} + ISSUE {ISSUE} = {dr + Q + ISSUE} "
                                               f"< P//2 = {P // 2} ({ppm} ppm)")
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
            if s.ldelay and s.ldelay > H - E - 2:
                diags.add("T4-profile", pc, f"ldelay {s.ldelay} > H - EDGE_LAT - 2 = {H - E - 2}")
            for o in sorted(s.data_owners - {s.clk_owner}):
                if H < E + 1 + s.ldelay + drift(ppm, 2 * H):
                    diags.add("T3-latency", pc, f"half period {H} < EDGE_LAT + 1 + ldelay {s.ldelay} + drift: "
                                                f"{o.name} owns a data line, follows the clock and cannot "
                                                f"launch in time")

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

    _check_stalls(low, steps, timed, diags)

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
                    if prev is None:  # reset completes at 0, so instruction 0 starts at now = 2
                        st[r] = (2 + lo, 2 + hi)
                    else:             # the first own cycle >= c + 1: c + 1 or c + ISSUE (isa §4.0)
                        c_lo, c_hi = completion(prev, r)
                        st[r] = (c_lo + 1 + lo, c_hi + ISSUE + hi)
            if s.owner_role not in st:
                continue
            # anchor distance along this edge only (untimed instructions don't move it)
            a_edge = 0 if prev is None else max(
                (_advance(prev, t)[0] for t in states.get(prev.pc, set())), default=0)
            _pair_checks(s, st, a_edge, ppm, diags)
    return diags.list()


def _reach(instrs: list[Instr], starts: list[int], role: Role) -> set[int]:
    """Every pc `role` can execute from `starts`."""
    seen: set[int] = set()
    work = [s for s in starts if s < len(instrs)]
    while work:
        pc = work.pop()
        if pc in seen:
            continue
        seen.add(pc)
        work += successors(instrs, pc, role)
    return seen


def _makes_others_wait(s: _Step, r: Role) -> bool:
    """An unbounded COND that r does, or an unbounded clocked XFER r clocks (whoever owns its
    data): the others wait for r however long it stalled. A bounded one times them out."""
    if s.prof.bounded:
        return False
    if s.op == Op.COND:
        return s.owner_role == r
    return s.op == Op.XFER and s.is_clk_owner(r)


def _check_stalls(low: Lowered, steps: dict[int, _Step], timed: set[int], diags: _Diags) -> None:
    """T5-stall (isa §6): a POP can wait for the host for any time, so the next timed step on
    every path must be the staller's own and make the others wait for it. An owner's src = FIFO
    XFER waits inside the step (unless the clock is someone else's: then it sends ONES), so it
    must itself make the others wait: only a clocked XFER its owner clocks does."""
    instrs = low.instrs
    starts = [0] + ([low.image.abort_pc] if low.image.abort_pc else [])
    for r in ROLES:  # MON owns no step, so nothing after a MON POP makes the others wait
        for pc in sorted(_reach(instrs, starts, r)):
            ins = instrs[pc]
            if ins.op == Op.POP:
                for nxt in sorted(gaps(instrs, timed, pc + 1, r)):
                    if not _makes_others_wait(steps[nxt], r):
                        diags.add("T5-stall", nxt, f"{r.name} can stall at the POP at pc {pc}; its next "
                                                   f"timed step must be one it owns that makes the others "
                                                   f"wait (an unbounded COND it does, or an unbounded "
                                                   f"clocked XFER it clocks)")
            elif (ins.op == Op.XFER and (steps[pc].does(r) or r == steps[pc].d2_owner)
                  and unpack_xfer(ins.operand)[1] == Src.FIFO):  # it drives a data line from TX
                s = steps[pc]
                if (not s.clocked or s.is_clk_owner(r)) and not _makes_others_wait(s, r):
                    diags.add("T5-stall", pc, f"{r.name} owns this src = FIFO XFER and waits here while "
                                              f"TX is empty, so its followers lose the timeline; only an "
                                              f"unbounded clocked XFER its owner clocks may take src = "
                                              f"FIFO (or POP into a register before an unbounded COND)")


def _pair_checks(s: _Step, st: dict[Role, tuple[int, int]], amax: int, ppm: int, diags: _Diags) -> None:
    p = s.prof
    pc = s.pc
    o = s.owner_role
    o_lo, o_hi = st[o]
    followers = [r for r in st if not s.does(r)]
    if s.op == Op.COND:
        offset = p.nom
        if p.bounded and o_hi > (offset if offset > 0 else ISSUE):
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
    # HOLD / XFER: the owner's first action is at offset 0 (the issue slot allows ISSUE)
    if s.clocked:
        # No T5-overrun: a clock owner re-bases tb := max(tb, start - H) or max(tb, start) instead
        # of overrunning (isa §4.1, §6). The model runs clocked XFER from step 3b.
        first_obs = p.nom + E  # first leading edge, as seen by a clock follower
        dr = drift(ppm, amax + p.nom)
        for r in st:
            if not s.is_clk_owner(r) and st[r][1] + dr > first_obs:
                diags.add("T5-watch", pc, f"{r.name} starts at {st[r][1]}, after the first clock edge "
                                          f"is visible at {first_obs}")
        for d in sorted(s.data_owners - {s.clk_owner}):
            # with cpha 0 bit 0 is launched when the data owner starts the step
            if s.cpha == 0 and d in st and p.nom < st[d][1] + 1 + drift(ppm, amax + p.nom):
                diags.add("T3-latency", pc, f"data owner {d.name} starts at {st[d][1]}; bit 0 is not valid "
                                            f"before the first capture edge at {p.nom}")
        if p.max != INF:
            # A bounded clock follower counts its timeout from its own start, then from each edge
            # (isa §4.1). The clock owner re-bases to its start (cpha 0) or start - H (cpha 1), so
            # its first edge is at max(0, start) + H or max(H, start), seen EDGE_LAT later.
            c_hi = st[s.clk_owner][1] if s.clk_owner in st else 0
            first = (max(0, c_hi) + p.nom if s.cpha == 0 else max(p.nom, c_hi)) + E
            for r in st:
                if not s.is_clk_owner(r) and first + dr - st[r][0] > p.max + Q:
                    diags.add("T3-latency", pc, f"{r.name} may start at {st[r][0]} and time out: the first "
                                                f"clock edge may be visible only at {first} (drift {dr}), "
                                                f"past max {p.max} + QUANT after it")
            if p.nom + drift(ppm, p.nom) > p.max + Q:
                diags.add("T3-latency", pc, f"a clock phase of {p.nom} plus drift outlasts max {p.max} + "
                                            f"QUANT: a bounded clock follower would time out")
        return
    if o_hi > ISSUE:
        diags.add("T5-overrun", pc, f"owner starts at {o_hi}, more than ISSUE after the step start")
    sample = p.nom // 2 + E
    dr = drift(ppm, amax + p.nom)
    for r in followers:
        if st[r][1] + dr > sample:
            diags.add("T5-watch", pc, f"{r.name} starts at {st[r][1]}, after its first sample at {sample}")
