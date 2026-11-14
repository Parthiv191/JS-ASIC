"""Static checks over the lowered program: branch taint (R2) and timing rules (T1–T5).

Formulas are docs/design/isa-v0.md §6. All arithmetic is integer (ppm), so
boundary cases don't depend on floating point.
"""
from __future__ import annotations

from dataclasses import dataclass

from .compiler import Diagnostic, Lowered
from .image import Profile
from .isa import (
    EDGE_LAT as E, F_ABORT, F_CHK, F_REG, INF, ISSUE, NUM_REGS, QUANT as Q, U, BrCond, BusKind, Dst, Instr, Op, Owner,
    CondKind, FzOp, RawOp, Role, Src, FZ_END_CYCLES, FZ_NEXT_CYCLES, FZ_TOK_CYCLES,
    reg_of, unpack_cond, unpack_role_if, unpack_xfer,
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


def _role_successors(instrs: list[Instr], pc: int, role: Role) -> list[int]:
    """Where `role` can go from pc, a pc past the last word included: ROLE_IF follows the role's
    own path (descriptions have no BR ROLE)."""
    ins = instrs[pc]
    if ins.op == Op.HALT:
        return []
    if ins.op == Op.BR:
        return [ins.operand] if BrCond(ins.sub) == BrCond.ALWAYS else [pc + 1, ins.operand]
    if ins.op == Op.RAW and ins.sub == RawOp.JPIN:
        return [pc + 1, ins.operand & 0x7FF]
    if ins.op == Op.ROLE_IF:
        mask, count = unpack_role_if(ins.operand)
        return [pc + 1] if mask >> int(role) & 1 else [pc + 1 + count]
    return [pc + 1]


def check_end(desc, low: Lowered) -> list[Diagnostic]:
    """S-end (isa §5): no role's path runs past the last word, by falling through it or by skipping
    a role block at the end. The model halts there; the chip has no program length and runs
    whatever the SRAM holds next (NOP is opcode 0). A label at the end names no step, so no branch
    or abort_pc gets there (S-label); a bus exit goes to abort_pc only from a step with ABORT."""
    instrs, diags = low.instrs, _Diags(desc, low)
    n = len(instrs)
    if not n:
        diags.add("S-end", 0, "the program is empty: the chip would run whatever the SRAM holds")
        return diags.list()
    entry = [0] + ([low.image.abort_pc] if any(_aborts(ins) for ins in instrs) else [])
    for role in ROLES:
        seen: set[int] = set()
        work = list(entry)  # abort_pc names a step, so it is < n
        while work:
            pc = work.pop()
            if pc in seen:
                continue
            seen.add(pc)
            for s in _role_successors(instrs, pc, role):
                if s < n:
                    work.append(s)
                elif instrs[pc].op == Op.ROLE_IF:
                    diags.add("S-end", pc, f"{role.name} skips this role block, the program's last, "
                                           f"and runs past the end: put a step (halt) after it")
                else:
                    diags.add("S-end", pc, f"{role.name} runs past the program's last word here, where "
                                           f"the chip runs whatever the SRAM holds: end with halt or a branch")
    return diags.list()


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
    od_lanes = {i for i, la in enumerate(img.lanes)
                if any(x is not None and img.lines[x].kind == "od" for x in (la.data, la.data2))}
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


# ----------------------------------------------------------------------------- R2-edge
_LG, _SHUT = "LG", "SHUT"   # a pending OD let-go (low until the lane's next clock edge); a shut gate
_SHUT1 = "SHUT?"            # a gated line shut on some paths, released or driven 1 on the others
_READS_1 = (_SHUT, _SHUT1, 1)


def _join(x, y):
    """Two paths' drives: the same, a gated line that reads 1 on both, or unknown."""
    if x == y:
        return x
    return _SHUT1 if x in _READS_1 and y in _READS_1 else None
_PARTIES = (Role.P0, Role.P1)


def check_levels(desc, low: Lowered) -> list[Diagnostic]:
    """R2-edge (isa §5). A COND its owner does drives the post-edge level (isa §4.2): if the wire is
    already there, there is no edge and the others wait for one that never comes. So on every path
    the owner can take, the wire is at the pre-edge level before the COND.

    The walk, once per party r along r's paths, tracks every party's drive on every line:
    - a PP line is its owner's drive. It is the idle level from reset, unless the line is gated;
    - an OD line is the wired-AND of every party's drive. It starts released (1), whatever its idle;
    - a drive comes from a HOLD, a COND's post-edge level, an XFER from ZERO or ONES (a PP data line
      keeps its last bit; a clocked OD one holds it until the lane's next clock edge, the let-go),
      a lane's clock at its idle level after a clocked XFER, or RAW SET/REL;
    - a gated line is released while its gate is shut: at RUN and after a SELECT STOP, until the
      next timed step its engine runs. RAW SET does nothing then (isa §2). Where a path with the
      gate shut meets one where the line is released or 1, it reads 1 (_SHUT1);
    - a role block r skips leaves unknown whatever its parties may drive in it;
    - anything else, and paths that disagree, leave a drive unknown.

    COND START/STOP are the edges on `bus_a` (START = FALL, STOP = RISE); on I2C, SCL must be high
    too. The paths from an abort or bus exit into abort_pc are not walked yet (isa §5, open)."""
    instrs = low.instrs
    diags = _Diags(desc, low)
    img = low.image
    if not instrs:
        return []
    lines, lanes = img.lines, img.lanes
    kind = img.bus.kind
    bus_a = img.bus.a if kind in (BusKind.I2C, BusKind.SELECT) else None
    scl = img.bus.b if kind == BusKind.I2C else None
    span_at = {pc: sp for sp in low.spans for pc in range(sp.start, sp.end)}
    block_at = {sp.role_if_pc: sp for sp in low.spans}

    def runs(pc: int) -> tuple:
        sp = span_at.get(pc)
        return _PARTIES if sp is None else tuple(q for q in _PARTIES if q in sp.roles)

    def does(owner, q) -> bool:
        return owner == Owner.SHARED or int(owner) == int(q)

    def edge_line(ins: Instr):
        """(kind, line) of a COND: START/STOP are on bus_a."""
        k = CondKind(unpack_cond(ins.operand)[0])
        return k, (bus_a if k in (CondKind.START, CondKind.STOP) else ins.line)

    def drives(pc: int) -> list[tuple[Role, int]]:
        """(party, line) pairs the instruction at pc may drive."""
        ins, ex = instrs[pc], runs(pc)
        out: list[tuple[Role, int]] = []
        if ins.op in (Op.COND, Op.HOLD):
            line = edge_line(ins)[1] if ins.op == Op.COND else ins.line
            if line is not None:
                out += [(q, line) for q in ex if does(ins.owner, q)]
        elif ins.op == Op.XFER:
            la = lanes[ins.line]
            out += [(q, la.data) for q in ex if does(ins.owner, q)]
            for ln in (la.data2, la.clk):
                if ln is not None:
                    out += [(q, ln) for q in ex if does(lines[ln].owner, q)]
        elif ins.op == Op.RAW and ins.sub in (RawOp.SET, RawOp.REL):
            out += [(q, ins.ra) for q in ex]
        return out

    touched = {sp.role_if_pc: {(q, ln) for pc in range(sp.start, sp.end) for q, ln in drives(pc)}
               for sp in low.spans}

    def wire(st, i):
        ln = lines[i]
        if ln.kind == "pp":
            v = st[int(ln.owner)][i]
            return 1 if v in (_SHUT, _SHUT1) else v
        vals = [st[int(q)][i] for q in _PARTIES]
        if any(v in (0, _LG) for v in vals):
            return 0
        return None if any(v is None for v in vals) else 1

    def edge_on(st, clk: int) -> None:
        """An edge on a lane's clock: that lane's let-go is taken (isa §4.3)."""
        for la in lanes:
            if la.clk == clk:
                for dl in (la.data, la.data2):
                    if dl is not None:
                        for q in _PARTIES:
                            if st[int(q)][dl] == _LG:
                                st[int(q)][dl] = 1

    def check(st, pc: int, ins: Instr, r: Role) -> None:
        k, line = edge_line(ins)
        if line is None:
            return
        name = lines[line].name
        if k in (CondKind.FALL, CondKind.START):
            ok, why = wire(st, line) == 1, f"{name} may not be high here"
        elif lines[line].kind == "pp":
            ok, why = wire(st, line) == 0, f"{name} may not be low here"
        else:  # an OD RISE: the doers' release lets it up only if nobody else holds it low
            mine = [st[int(q)][line] for q in _PARTIES if does(ins.owner, q)]
            others = [st[int(q)][line] for q in _PARTIES if not does(ins.owner, q)]
            ok = any(v in (0, _LG) for v in mine) and all(v == 1 for v in others)
            why = f"{name} may not be low by the doer's drive alone here"
        if scl is not None and k in (CondKind.START, CondKind.STOP) and wire(st, scl) != 1:
            ok, why = False, f"SCL ({lines[scl].name}) may not be high here"
        if not ok:
            diags.add("R2-edge", pc, f"COND {k.name} by {r.name}: {why}, so its drive makes no edge "
                                     f"and the others wait for one (isa §4.2)")

    def step(st, pc: int) -> None:
        ins, ex = instrs[pc], runs(pc)
        if ins.op in (Op.COND, Op.HOLD, Op.XFER):
            # a timed step opens the gate of every engine that runs it (isa §2): its gated lines
            # stay released, at the pull-up, but a RAW SET now drives them
            for q in ex:
                for i, ln in enumerate(lines):
                    if ln.gated and int(ln.owner) == int(q) and st[int(q)][i] in (_SHUT, _SHUT1):
                        st[int(q)][i] = 1
        if ins.op in (Op.COND, Op.HOLD):
            if ins.op == Op.COND:
                k, line = edge_line(ins)
                level = 0 if k in (CondKind.FALL, CondKind.START) else 1
            else:
                k, line, level = None, ins.line, ins.operand & 1
            if line is None:
                return
            for q in ex:
                if does(ins.owner, q):
                    st[int(q)][line] = level
            edge_on(st, line)
            # the STOP shuts the gate it opened: for a follower that holds only because T5-watch
            # has it start the COND STOP by the confirm cycle (a later start would reopen it)
            if k == CondKind.STOP and kind == BusKind.SELECT:
                for i, ln in enumerate(lines):
                    if ln.gated:
                        st[int(ln.owner)][i] = _SHUT
        elif ins.op == Op.XFER:
            la = lanes[ins.line]
            if la.clk is not None:
                edge_on(st, la.clk)
            const = {Src.ZERO: 0, Src.ONES: 1}.get(unpack_xfer(ins.operand)[1])
            for q, dl in drives(pc):
                if dl == la.clk:
                    st[int(q)][dl] = la.cpol
                elif lines[dl].kind == "pp" or const == 1:
                    st[int(q)][dl] = const
                else:
                    st[int(q)][dl] = _LG if la.clk is not None and const == 0 else None
        elif ins.op == Op.RAW and ins.sub in (RawOp.SET, RawOp.REL):
            od = lines[ins.ra].kind == "od"
            for q in ex:
                cur = st[int(q)][ins.ra]
                if cur == _SHUT:
                    continue  # a shut gate ignores RAW (isa §2)
                if lines[ins.ra].gated and cur is None:
                    st[int(q)][ins.ra] = None
                elif cur == _SHUT1:  # ignored where the gate is shut, driven where it is open
                    st[int(q)][ins.ra] = _SHUT1 if ins.sub == RawOp.SET and ins.operand & 1 else None
                elif ins.sub == RawOp.SET:
                    st[int(q)][ins.ra] = ins.operand & 1
                else:
                    st[int(q)][ins.ra] = 1 if od else None

    init = []
    for q in _PARTIES:
        row = []
        for ln in lines:
            if ln.kind == "od":
                row.append(1)
            elif ln.gated:
                row.append(_SHUT if int(ln.owner) == int(q) else 1)
            else:
                row.append(ln.idle)
        init.append(tuple(row))
    for r in _PARTIES:
        state: dict[int, tuple] = {0: tuple(init)}
        work = [0]
        while work:
            pc = work.pop()
            st = [list(row) for row in state[pc]]
            ins = instrs[pc]
            if ins.op == Op.COND and r in runs(pc) and does(ins.owner, r):
                check(st, pc, ins, r)
            step(st, pc)
            sp = block_at.get(pc)
            if sp is not None and r not in sp.roles:  # r skips it: what its parties drive there is unknown
                for q, ln in touched[pc]:
                    st[int(q)][ln] = None
            out = tuple(tuple(row) for row in st)
            for s in successors(instrs, pc, r):
                old = state.get(s)
                new = out if old is None else tuple(tuple(_join(x, y) for x, y in zip(o, n))
                                                    for o, n in zip(old, out))
                if new != old:
                    state[s] = new
                    work.append(s)
    return diags.list()


def _aborts(ins: Instr) -> bool:
    return ins.op in (Op.COND, Op.HOLD, Op.XFER) and bool(ins.flags & F_ABORT)


def _entries(low: Lowered) -> list[int]:
    """Where a run can start: pc 0, and abort_pc once a step has ABORT (an abort or a bus exit
    restarts there; with none, nothing jumps to it)."""
    apc = low.image.abort_pc
    return [0] + ([apc] if apc and any(_aborts(ins) for ins in low.instrs) else [])


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
    if ins.op == Op.COND:
        _, meas, rd = unpack_cond(ins.operand)
        if meas:  # isa §5 R2: COND meas taints its register
            return put(rd, True)
        return t
    if ins.op == Op.RAW and ins.sub in (RawOp.GET, RawOp.EVT):
        return put(ins.rd, True)
    if ins.op == Op.FZ and ins.sub in (FzOp.NEXT, FzOp.END):  # the fuzzing party's alone
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
    clk_line: int | None = None
    cpha: int = 0
    chk: bool = False
    abort: bool = False
    od: bool = False  # an XFER whose data line is open-drain: its owner reads the bits back
    d2_owner: Role | None = None  # duplex: the party that drives data2 (isa §4.3)
    od_drivers: frozenset = frozenset()  # parties that may drive an OD data line of the lane
    stretch: bool = False   # a clocked XFER on a stretch lane
    cpol: int = 0
    stretch_wait: bool = False  # a COND RISE on a stretch lane's clock (never HOLD 1, S-bus): its owner waits
    bus_wait: int = 0  # COND START/STOP: a follower completes bus_confirm + 1 after the edge is seen
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
    stretch_clocks = {la.clk for la in img.lanes if la.stretch and la.clk is not None}
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
                kw.update(clocked=True, cpha=lane.cpha, clk_owner=Role(int(img.lines[lane.clk].owner)), clk_line=lane.clk,
                          ldelay=lane.ldelay, stretch=bool(lane.stretch), cpol=lane.cpol)
                if lane.data2 is not None and img.lines[lane.data2].owner in (Owner.P0, Owner.P1):
                    kw["d2_owner"] = Role(int(img.lines[lane.data2].owner))
                od = set()
                for x in (lane.data, lane.data2):
                    if x is not None and img.lines[x].kind == "od":
                        ow = img.lines[x].owner
                        od |= {Role.P0, Role.P1} if ow == Owner.SHARED else {Role(int(ow))}
                kw["od_drivers"] = frozenset(od)
        if ins.op == Op.COND and unpack_cond(ins.operand)[0] in (CondKind.START, CondKind.STOP):
            kw["bus_wait"] = img.bus.confirm_cycles + 1
        elif ins.op == Op.COND and ins.line in stretch_clocks:
            # isa §4.3: COND RISE clk waits for the release to be seen
            kw["stretch_wait"] = ins.op == Op.COND and unpack_cond(ins.operand)[0] == CondKind.RISE
        out[pc] = _Step(pc, ins.op, ins.owner, prof, **kw)
    return out


def completion(s: _Step, r: Role) -> tuple[int, int]:
    """Cycles (lo, hi), relative to the step's end tb, in which role r finishes step s (isa §6).

    An owner's HOLD drives, and completes, when it starts, which is up to ISSUE late (isa §4.2;
    T5-overrun bounds the start). So does a 1-bit unclocked XFER; with more bits the last one is
    planned past ISSUE, so the lateness is absorbed."""
    nom = s.prof.nom
    wait = E if s.stretch_wait and s.does(r) else 0  # it sees its own release EDGE_LAT later
    if s.op == Op.COND:
        c = wait if s.does(r) else E + s.bus_wait  # START/STOP: E + bus_confirm + 1 (isa §6)
        return c, c
    if s.op == Op.HOLD or not s.clocked:
        if s.does(r) and not s.od:  # an OD owner reads back, so it finishes as a follower does
            return -nom + wait, max(ISSUE - s.n * nom, -nom) + wait
        c = -nom + nom // 2 + E
        return c, c
    if s.is_clk_owner(r):
        # it completes at its last edge, or at its last sample if it follows a data line
        # (an OD owner reads its bits back, so it completes like a follower, isa §6)
        c = 0 if s.drives_all(r) and not s.od else (max(0, E - nom) if s.cpha == 0 else E)
        if s.stretch and s.cpol == 1:  # its last edge is a release: it completes when it sees it
            c = max(c, E)
        return c, c
    return E, E


FZ_CYCLES = {FzOp.NEXT: FZ_NEXT_CYCLES, FzOp.TOK: FZ_TOK_CYCLES, FzOp.END: FZ_END_CYCLES}


def _cost(ins: Instr) -> int:
    """Cycles an untimed instruction adds to `L` (isa §4.0, §6): U, RAW DELAY n rounded up to a
    multiple of U, and an FZ op's fixed total (isa §4.6). POP counts U; a stall is T5-stall's job."""
    if ins.op == Op.RAW and ins.sub == RawOp.DELAY:
        return -(-max(1, ins.operand) // U) * U
    if ins.op == Op.FZ:
        return FZ_CYCLES.get(ins.sub, U)
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
    state: dict[int, set[tuple[int, int, int]]] = {}

    def walk(start: int) -> None:
        state[start] = {(0, 0, 0)}
        work = [start]
        while work:
            pc = work.pop()
            out = {_advance(steps.get(pc), t) for t in state[pc]}
            for s in successors(instrs, pc):
                cur = state.setdefault(s, set())
                new = out - cur
                if not new:
                    continue
                if any(t[0] > A_CAP for t in new) or len(cur | new) > SET_CAP:
                    diags.add("T1-unanchored", s, "the timeline is never re-anchored here (the anchor distance "
                                                  "passes 2^22, or more than 64 anchor states meet): add a "
                                                  "followed COND or a clocked XFER")
                    continue
                cur |= new
                work.append(s)

    # an abort or a bus exit restarts at abort_pc, whose first timed step re-anchors (T5-abort):
    # code that only they reach is checked from there (S-label: abort_pc names a step)
    for start in _entries(low):
        if start not in state:
            walk(start)
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

    # T5-startup: instruction 0 starts at now = 2, when every engine sees an idle-0 line's start-up 1
    # (isa §2) as a rise, and a PP one falling back to idle at now = 3: a step there that watches
    # for that edge takes it as the other party's
    i0, img = instrs[0], low.image
    watched = []
    if i0.op == Op.COND:
        k = CondKind(unpack_cond(i0.operand)[0])
        if k == CondKind.RISE or (k == CondKind.FALL and img.lines[i0.line].kind == "pp"):
            watched.append(i0.line)
    elif i0.op == Op.XFER and img.lanes[i0.line].clk is not None:
        watched.append(img.lanes[i0.line].clk)
    for ln in watched:
        if img.lines[ln].idle == 0:
            diags.add("T5-startup", 0, f"instruction 0 watches {img.lines[ln].name}, an idle-0 line: it starts at "
                                       f"now = 2, when every engine sees the line's start-up 1 (isa §2) as a rise; "
                                       f"put an untimed instruction first")

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
        if s.stretch and p.nom < E + 2:
            diags.add("T4-profile", pc, f"a stretch lane needs H >= EDGE_LAT + 2 = {E + 2}, got {p.nom}")
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
            # a party that drives a data line, or may hold an OD let-go from an earlier step on
            # the lane (it releases with a launch's latency), and follows the clock
            for o in sorted((s.data_owners | s.od_drivers) - {s.clk_owner}):
                if H < E + 1 + s.ldelay + drift(ppm, 2 * H):
                    diags.add("T3-latency", pc, f"half period {H} < EDGE_LAT + 1 + ldelay {s.ldelay} + drift: "
                                                f"{o.name} drives a data line (or lets one go), follows the "
                                                f"clock and cannot launch in time")

    # an abort restarts only the roles that fail: they must re-sync on the other party's next event
    aborting = {r for s in steps.values() if s.abort for r in ROLES if not s.does(r)}
    apc = low.image.abort_pc
    bus_on = int(low.image.bus.kind) != 0
    # FZ ops that cost more than U on the way to that step: the timing rules don't time the path
    # from an abort or a bus exit into abort_pc, so its 40-48 cycles could miss the re-sync
    no_fz = [Instr(Op.NOP) if ins.op == Op.FZ else ins for ins in instrs]
    for r in sorted(aborting):
        plain = gaps(no_fz, timed, apc, r)
        for pc, (_, hi) in gaps(instrs, timed, apc, r).items():
            if hi != plain[pc][1]:
                diags.add("T5-abort", pc, f"{r.name} can run fz.next or fz.end between abort_pc {apc} and "
                                          f"this step: the timing rules don't time that path, so it "
                                          f"could miss the re-sync (isa §6); end the iteration after it")
        for pc in gaps(instrs, timed, apc, r):
            s = steps[pc]
            # it follows the COND, or does a COND START/STOP (on a bus the others exit on it)
            if not (s.op == Op.COND and not s.prof.bounded and (not s.does(r) or s.bus_wait)):
                diags.add("T5-abort", pc, f"{r.name} can abort and restart at pc {apc}; the first timed "
                                          f"step it reaches there must be an unbounded COND it follows, or "
                                          f"a COND START/STOP it does, so the two parties meet again")
            elif bus_on and s.abort and not s.bus_wait:
                # a bus exit leaves its flag set, so this step's ABORT would exit again at once
                diags.add("T5-abort", pc, f"on a bus the first step at abort_pc may have ABORT only if "
                                          f"it is a followed COND START/STOP: after a bus exit the flag "
                                          f"stays set, so it would exit again, forever")

    _check_stalls(low, steps, timed, diags)

    # pairwise checks: predecessor timed step (or reset) -> next timed step
    preds: list[tuple[_Step | None, int]] = [(None, 0)] + [(s, pc + 1) for pc, s in steps.items()]
    runs = {r: _reach(instrs, _entries(low), r) for r in ROLES}
    for prev, start in preds:
        # only the roles that can run prev (a step in a lead block is the leader's alone)
        per_role = {r: gaps(instrs, timed, start, r) if prev is None or prev.pc in runs[r] else {}
                    for r in ROLES}
        for nxt_pc in set().union(*per_role.values()):
            s = steps[nxt_pc]
            if (prev is not None and prev.op == Op.COND and prev.prof.bounded and not prev.abort
                    and not (s.op == Op.COND and not s.prof.bounded)):
                late = prev.prof.max - prev.prof.nom + Q + E + 1
                diags.add("T5-timeout", prev.pc, f"a follower that times out here finishes up to {late} "
                                                 f"cycles late, off the owner's timeline; the next timed "
                                                 f"step (pc {nxt_pc}) must be an unbounded COND, or add abort")
            if prev is not None and prev.bus_wait and s.bus_wait:
                # a bus edge inside the previous one's confirm window replaces its candidate
                # (isa §4.7): that START or STOP is never confirmed, and its followers hang
                confirm = s.bus_wait - 1
                if s.prof.nom - drift(ppm, s.prof.nom) - Q < confirm:
                    diags.add("T5-watch", nxt_pc, f"this bus edge comes {s.prof.nom} after the previous "
                                                  f"one: it needs nom - drift - QUANT >= bus_confirm "
                                                  f"{confirm}, or it replaces that one's candidate")
            if s.owner_role is None:
                continue  # SHARED steps: Phase 4 (ARB)
            st = {r: _start(prev, r, *per_role[r][nxt_pc]) for r in ROLES if nxt_pc in per_role[r]}
            if s.owner_role not in st:
                continue
            clk = None  # the clock owner's start when it doesn't run s: its start of a lead block's S
            if s.clocked and s.clk_owner not in st:
                sp, succ = _lead_clock(low, steps, s)
                if succ is not None and (prev is None or not sp.start <= prev.pc < sp.end)                         and sp.end in per_role[s.clk_owner]:
                    clk = _start(prev, s.clk_owner, *per_role[s.clk_owner][sp.end])
            # anchor distance along this edge only (untimed instructions don't move it)
            a_edge = 0 if prev is None else max(
                (_advance(prev, t)[0] for t in states.get(prev.pc, set())), default=0)
            _pair_checks(s, st, a_edge, ppm, diags, clk)
    return diags.list()


def _start(prev: _Step | None, r: Role, lo: int, hi: int) -> tuple[int, int]:
    """r's (earliest, latest) start of a step, `lo`..`hi` untimed cycles after prev (isa §6, L)."""
    if prev is None:  # reset completes at 0, so instruction 0 starts at now = 2
        return 2 + lo, 2 + hi
    c_lo, c_hi = completion(prev, r)  # the first own cycle >= c + 1: c + 1 or c + ISSUE (isa §4.0)
    return c_lo + 1 + lo, c_hi + ISSUE + hi


def _lead_clock(low: Lowered, steps: dict[int, _Step], s: _Step):
    """(block, S) for a clocked step in a lead block whose clock the other party owns: that party
    skips the block and clocks its fall-through successor S meanwhile, so S's edges are the
    step's, if S is a clocked XFER on the same clock with the same half period and cpha, and at
    least as many bits (else S is None)."""
    for sp in low.spans:
        if sp.lead and sp.start <= s.pc < sp.end and s.clk_owner not in sp.roles:
            succ = steps.get(sp.end)
            same = (succ is not None and succ.clocked and succ.clk_line == s.clk_line
                    and succ.prof.nom == s.prof.nom and succ.cpha == s.cpha and succ.n >= s.n)
            return sp, succ if same else None
    return None, None


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


def _makes_others_wait(s: _Step, r: Role, low: Lowered | None = None,
                       steps: dict[int, _Step] | None = None) -> bool:
    """An unbounded COND that r does, or an unbounded clocked XFER r clocks (whoever owns its
    data): the others wait for r however long it stalled. A bounded one times them out. A step
    in a lead block r leads, if the block's successor makes the others wait for r: they are in
    it while r decides (R3-lead). Not the successor of another party's lead block that holds a
    bounded step on r's clock: that step gets the successor's edges, and times out while r stalls."""
    if low is not None and steps is not None:
        for sp in low.spans:
            if sp.lead and r in sp.roles and sp.start <= s.pc < sp.end:
                succ = steps.get(sp.end)
                return succ is not None and _makes_others_wait(succ, r)
        for sp in low.spans:  # r clocks s for another party's lead block that falls through to it
            if sp.lead and sp.end == s.pc and r not in sp.roles and any(
                    t.clocked and t.clk_owner == r and t.prof.bounded
                    for t in (steps.get(pc) for pc in range(sp.start, sp.end)) if t is not None):
                return False  # the leader's bounded step there gets s's edges: it times out while r stalls
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
    starts = _entries(low)
    for r in ROLES:  # MON owns no step, so nothing after a MON POP makes the others wait
        for pc in sorted(_reach(instrs, starts, r)):
            ins = instrs[pc]
            if ins.op == Op.POP:
                for nxt in sorted(gaps(instrs, timed, pc + 1, r)):
                    if not _makes_others_wait(steps[nxt], r, low, steps):
                        diags.add("T5-stall", nxt, f"{r.name} can stall at the POP at pc {pc}; its next "
                                                   f"timed step must be one it owns that makes the others "
                                                   f"wait (an unbounded COND it does, or an unbounded "
                                                   f"clocked XFER it clocks)")
            elif (ins.op == Op.XFER and (steps[pc].does(r) or r == steps[pc].d2_owner)
                  and unpack_xfer(ins.operand)[1] == Src.FIFO):  # it drives a data line from TX
                s = steps[pc]
                if (not s.clocked or s.is_clk_owner(r)) and not _makes_others_wait(s, r, low, steps):
                    diags.add("T5-stall", pc, f"{r.name} owns this src = FIFO XFER and waits here while "
                                              f"TX is empty, so its followers lose the timeline; only an "
                                              f"unbounded clocked XFER its owner clocks may take src = "
                                              f"FIFO (or POP into a register before an unbounded COND)")


def _pair_checks(s: _Step, st: dict[Role, tuple[int, int]], amax: int, ppm: int, diags: _Diags,
                 clk: tuple[int, int] | None = None) -> None:
    """The checks between a step and its predecessor, from each role's start (`st`). `clk` is the
    clock owner's start when it doesn't run a clocked step itself (_lead_clock), else None."""
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
        # a followed START/STOP needs its follower waiting only by the confirm: the detector runs
        # every cycle and the flag is sticky (isa §4.7), so bus_wait = bus_confirm + 1 more
        seen_by = event + E + s.bus_wait
        for r in followers:
            if st[r][1] + dr > seen_by:
                diags.add("T5-watch", pc, f"{r.name} starts watching at {st[r][1]} but the event is "
                                          f"visible at {seen_by} (drift {dr})")
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
            c_start = st.get(s.clk_owner, clk)
            if c_start is None:  # its edges may never come (it HALTed, or S gives no start)
                for r in st:
                    if not s.is_clk_owner(r):
                        diags.add("T3-latency", pc, f"{r.name} may time out: the clock owner "
                                                    f"{s.clk_owner.name} doesn't run this step; it clocks it "
                                                    f"only as a lead block's successor on the same clock "
                                                    f"with the same half period, after a step before the block")
                return
            c_hi = c_start[1]
            first = (max(0, c_hi) + p.nom if s.cpha == 0 else max(p.nom, c_hi)) + E
            df = drift(ppm, amax + first)  # the follower's timeout runs until that edge
            for r in st:
                if not s.is_clk_owner(r) and first + df - st[r][0] > p.max + Q:
                    diags.add("T3-latency", pc, f"{r.name} may start at {st[r][0]} and time out: the first "
                                                f"clock edge may be visible only at {first} (drift {df}), "
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
