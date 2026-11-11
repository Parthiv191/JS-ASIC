"""Cycle-level engine interpreter: ISA v1 encoding, v0 execution (docs/design/isa-v0.md §4).

Each engine runs one Python generator. Every `yield` ends one engine cycle; on
resume `self.now` has advanced by one and `self.inp`/`self.prev_inp` hold this
cycle's synchronized inputs. Code between two yields is that cycle's work.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterator

from ..image import Image
from ..isa import (
    EDGE_LAT, F_ABORT, F_CHK, F_MSB, F_REG, F_RESYNC, INF, ISSUE, MEAS_MAX, NUM_REGS, QUANT, REG_MASK, RX_DEPTH,
    SYNC_LAT,
    TIME_CLAMP, U, AluOp,
    BrCond, CondKind, Dst, Instr, Op, Owner, RawOp, Role, Src, decode, li_imm, reg_of, reserved, unpack_cond,
    unpack_role_if, unpack_xfer,
)

VIOLATIONS = frozenset({"OVERRUN", "EARLY", "TIMEOUT", "LEVEL", "EDGE", "CLK_PHASE", "OVERFLOW", "UNDERRUN"})
SATURATING = frozenset({"OVERRUN", "EARLY", "TIMEOUT", "EDGE", "CLK_PHASE"})  # measured in cycles
# the sticky event register's bits (isa §4.1); 0 START and 1 STOP come with the bus detector
EVT_BIT = {"TIMEOUT": 1 << 2, "LEVEL": 1 << 3, "EDGE": 1 << 3, "CLK_PHASE": 1 << 3,
           "OVERFLOW": 1 << 4, "UNDERRUN": 1 << 5}
EVT_MASK = 0x3F
BIG = 1 << 40  # upper edge of an accumulated window that contains an unbounded step


@dataclass(frozen=True)
class StepRecord:
    pc: int
    op: str
    mode: str          # "do" | "follow"
    value: int | None
    cycle: int         # completion cycle
    tb: int            # timeline register after the step


@dataclass(frozen=True)
class Event:
    kind: str
    pc: int
    cycle: int
    measured: int | None = None
    lo: int | None = None
    hi: int | None = None

    @property
    def violation(self) -> bool:
        return self.kind in VIOLATIONS


@dataclass(frozen=True)
class Sample:
    pc: int
    line: int
    cycle: int
    slot: int          # nominal slot length in cycles (for margin coverage)


@dataclass(frozen=True)
class Branch:
    """A conditional decision: BR Z/NZ/TO/ROLE, RAW JPIN or ROLE_IF (architecture §5 `tr_br`).
    `taken`: the pc left the fall-through path (for ROLE_IF, the block was skipped)."""
    pc: int
    taken: bool
    cycle: int


Gen = Iterator[None]


def _decode(w: int) -> Instr | None:
    try:
        return decode(w)
    except ValueError:  # a free opcode (20-31): ILLEGAL when it runs
        return None


class Engine:
    def __init__(self, image: Image, role: Role, name: str = "", regs: list[int] | None = None,
                 evt: int = 0):
        self.image = image
        self.role = Role(role)
        self.name = name or self.role.name
        self.instrs: list[Instr | None] = [_decode(w) for w in image.words]  # None: a free opcode
        self.lines = image.lines
        self.pc = 0
        # r0-r7 keep their values across RUN; reset clears them (isa §1)
        self.regs = [0] * NUM_REGS if regs is None else [v & REG_MASK for v in regs]
        if len(self.regs) != NUM_REGS:
            raise ValueError(f"need {NUM_REGS} registers, got {len(self.regs)}")
        self.evt = evt & EVT_MASK  # sticky; survives RUN like r0-r7 (reset and SOFT_RESET clear it)
        self.tb = 0
        self.to = False            # timeout flag (isa-v0 §4)
        self.anchor = 0            # cycle of the last re-anchoring event (COND, clocked XFER, abort)
        self.acc = (0, 0, 0)       # accumulated (nom, min, max) of HOLD/XFER bits since the anchor
        self.now = -1
        self.drv: list[int | None] = [None] * len(self.lines)  # before RUN every pin is an input
        # The synchronizer is preloaded with each line's idle level when RUN rises (isa §4.1):
        # the engine sees idle at now 0-1 and the wire from now 2 (as it was at now 0).
        idle = [ln.idle for ln in self.lines]
        self._pipe: list[list[int]] = [idle] * SYNC_LAT
        self.inp: list[int] = idle
        self.prev_inp: list[int] | None = None
        self.tx: deque[int] = deque()
        self.rx: list[int] = []
        self.records: list[StepRecord] = []
        self.events: list[Event] = []
        self.samples: list[Sample] = []
        self.branches: list[Branch] = []
        self.starts: list[tuple[int, int]] = []  # (pc, now) per instruction start: the tr_issue trace
        self._abort_at: int | None = None        # decision cycle of an abort, until abort_pc starts
        self._stalled = False                    # POP waited since the last timed step (isa §4.1)
        self.halted = False
        self.halt_cycle: int | None = None
        self.idle = False
        self._proc = self._run()

    # ------------------------------------------------------------------ clocking
    def cycle(self, wire: list[int]) -> None:
        """Advance one engine cycle with the current (unsynchronized) wire values."""
        if self.halted:
            return
        w = list(wire[: len(self.lines)])
        self.now += 1
        # 24-bit time (isa §4.1): tb and the anchor never trail now by more than 2^22
        floor = self.now - TIME_CLAMP
        self.tb = max(self.tb, floor)
        self.anchor = max(self.anchor, floor)
        self._pipe.insert(0, w)
        self.prev_inp = self.inp
        self.inp = self._pipe.pop()
        self.idle = False
        next(self._proc)

    def edge(self, line: int) -> bool:
        return self.prev_inp[line] != self.inp[line]

    # ------------------------------------------------------------------ helpers
    def drive(self, line: int, value: int) -> None:
        if self.lines[line].kind == "pp":
            self.drv[line] = value & 1
        else:
            self.drv[line] = 0 if value == 0 else None

    def does(self, ins: Instr) -> bool:
        return self.role != Role.MON and (ins.owner == Owner.SHARED or int(ins.owner) == int(self.role))

    def _step_start(self, ins: Instr) -> None:
        """A timed step starts: clear `to`, and end any stall. If the owner stalled since its last
        timed step, an owned step starts on its own time: tb := max(tb, start) (the stall
        re-base, isa §4.1). A followed step takes its timing from the other party instead."""
        self.to = False
        stalled, self._stalled = self._stalled, False
        if stalled and self.does(ins):
            self.tb = max(self.tb, self.now)

    def _push_rx(self, v: int) -> None:
        """Into the RX FIFO; when it is full the word is dropped with OVERFLOW (isa §4.9)."""
        if len(self.rx) >= RX_DEPTH:
            self.log("OVERFLOW")
        else:
            self.rx.append(v & REG_MASK)

    def log(self, kind: str, measured=None, lo=None, hi=None) -> None:
        if kind in SATURATING and measured is not None:
            measured = min(measured, MEAS_MAX)
        self.evt |= EVT_BIT.get(kind, 0)
        self.events.append(Event(kind, self.pc, self.now, measured, lo, hi))

    def wait_until(self, t: int) -> Gen:
        while self.now < t:
            yield

    def _record(self, ins: Instr, mode: str, value: int | None = None) -> None:
        self.records.append(StepRecord(self.pc, ins.op.name, mode, value, self.now, self.tb))

    def _reanchor(self, at: int) -> None:
        self.anchor = at
        self.acc = (0, 0, 0)

    def _accumulate(self, prof, n: int) -> None:
        nom, lo, hi = self.acc
        self.acc = (nom + n * prof.nom, lo + n * prof.min,
                    min(BIG, hi + (BIG if prof.max == INF else n * prof.max)))

    def _late(self, planned: int, slot0: bool) -> None:
        """OVERRUN for an owner action after its planned cycle. The issue slot lets an action
        planned at a step's start (offset 0) happen up to ISSUE cycles late (isa §4.1)."""
        late = self.now - planned
        if late > (ISSUE if slot0 else 0):
            self.log("OVERRUN", measured=late)

    def _abort(self) -> None:
        """Fatal follow failure on a step with ABORT: log, release OD lines, restart the
        timeline at `now`, jump to the abort vector. The step still ends in this cycle."""
        self.log("ABORT", measured=self.pc)
        for i, ln in enumerate(self.lines):
            if ln.kind == "od":
                self.drv[i] = None
        self.tb = self.now
        self._reanchor(self.now)
        self.pc = self.image.abort_pc
        self._abort_at = self.now

    # ------------------------------------------------------------------ interpreter
    @staticmethod
    def _next_start(start: int, done: int) -> int:
        """The successor's start: the first own cycle (even `now`) >= max(done + 1, start + U),
        for an instruction that started at `start` and completed at `done` (isa §4.0)."""
        t = max(done + 1, start + U)
        return t + (t & 1)

    def _run(self) -> Gen:
        # s0 (now = 0) drives each non-gated PP line the role owns to idle, on the wire from
        # now = 1 (isa §2), and presents address 0; the word lands two cycles later, so
        # instruction 0 starts at now = 2 (isa §4.0).
        if self.role != Role.MON:
            for i, ln in enumerate(self.lines):
                if ln.kind == "pp" and not ln.gated and int(ln.owner) == int(self.role):
                    self.drv[i] = ln.idle
        yield
        yield
        while True:
            if self.pc >= len(self.instrs):
                yield from self._halt()
            ins = self.instrs[self.pc]
            start = self.now
            self.starts.append((self.pc, start))
            if ins is None or reserved(ins):  # isa §3: halts in its start cycle, as HALT does
                self.log("ILLEGAL")
                yield from self._halt()
            handler = getattr(self, f"_op_{ins.op.name.lower()}", None)
            if handler is None:
                raise NotImplementedError(f"{ins.op.name} is not in the model yet (pc {self.pc})")
            yield from handler(ins)  # its last yield ends its completion cycle
            if self._abort_at is not None:
                # abort_pc is fetched on the first own cycle >= t + 1 and starts 2 later (isa §4.0)
                t, self._abort_at = self._abort_at, None
                yield from self.wait_until(t + 1 + ((t + 1) & 1) + 2)
            else:
                yield from self.wait_until(self._next_start(start, self.now - 1))

    def _halt(self) -> Gen:
        # isa §4.1: PP lines keep their level, OD lines are released
        for i, ln in enumerate(self.lines):
            if ln.kind == "od":
                self.drv[i] = None
        self.halted = True
        self.halt_cycle = self.now
        self.idle = True
        yield  # cycle() never resumes a halted engine

    def _op_halt(self, ins: Instr) -> Gen:
        yield from self._halt()

    def _op_nop(self, ins: Instr) -> Gen:
        self.pc += 1
        yield

    def _op_li(self, ins: Instr) -> Gen:
        self.regs[ins.rd] = li_imm(ins)
        self.pc += 1
        yield

    def _op_alu(self, ins: Instr) -> Gen:
        a = self.regs[ins.ra]
        b = self.regs[ins.operand & 7] if ins.flags & F_REG else ins.operand
        op = AluOp(ins.sub)
        r = {
            AluOp.ADD: lambda: a + b, AluOp.SUB: lambda: a - b, AluOp.AND: lambda: a & b,
            AluOp.OR: lambda: a | b, AluOp.XOR: lambda: a ^ b, AluOp.SHL: lambda: a << (b & 15),
            AluOp.SHR: lambda: a >> (b & 15), AluOp.EQ: lambda: int(a == b),
            AluOp.NE: lambda: int(a != b), AluOp.LT: lambda: int(a < b),
        }[op]()
        self.regs[ins.rd] = r & REG_MASK
        self.pc += 1
        yield

    def _op_br(self, ins: Instr) -> Gen:
        cond = BrCond(ins.sub)
        if cond == BrCond.ALWAYS:
            taken = True
        else:
            if cond == BrCond.ROLE:
                taken = bool(ins.ra >> int(self.role) & 1)
            elif cond == BrCond.TO:
                taken = self.to
            else:
                zero = self.regs[ins.ra] == 0
                taken = zero if cond == BrCond.Z else not zero
            self.branches.append(Branch(self.pc, taken, self.now))
        self.pc = ins.operand if taken else self.pc + 1
        yield

    def _op_role_if(self, ins: Instr) -> Gen:
        mask, count = unpack_role_if(ins.operand)
        skip = not mask >> int(self.role) & 1
        self.branches.append(Branch(self.pc, skip, self.now))
        self.pc += 1 + count if skip else 1
        yield

    def _op_push(self, ins: Instr) -> Gen:
        self._push_rx(self.regs[ins.ra])
        self.pc += 1
        yield

    def _op_pop(self, ins: Instr) -> Gen:
        while not self.tx:  # retried only on own cycles; idle (waiting on the host) throughout
            self._stalled = True
            self.idle = True
            yield
            self.idle = True
            yield
        self.regs[ins.rd] = self.tx.popleft() & REG_MASK
        self.pc += 1
        yield

    def _op_mark(self, ins: Instr) -> Gen:
        self.log("MARK", measured=ins.operand)
        self.pc += 1
        yield

    def _op_raw(self, ins: Instr) -> Gen:
        op = RawOp(ins.sub)
        line = ins.ra
        nxt = self.pc + 1
        if op == RawOp.EVT:
            self.regs[ins.rd] = self.evt
            self.evt &= ~ins.operand & EVT_MASK
        elif op == RawOp.SET:
            self.drive(line, ins.operand & 1)
        elif op == RawOp.REL:
            self.drv[line] = None
        elif op == RawOp.GET:
            self.regs[ins.rd] = self.inp[line]
        elif op == RawOp.JPIN:
            taken = self.inp[line] == ins.operand >> 11 & 1
            self.branches.append(Branch(self.pc, taken, self.now))
            if taken:
                nxt = ins.operand & 0x7FF
        elif op == RawOp.DELAY:  # n cycles, rounded up to a multiple of U
            self.pc = nxt
            for _ in range(-(-max(1, ins.operand) // U) * U):
                yield
            return
        self.pc = nxt
        yield

    # ------------------------------------------------------------------ timed steps (isa-v0 §4.1-4.3)
    def _op_cond(self, ins: Instr) -> Gen:
        p = self.image.profiles[ins.prof]
        kind, meas, _ = unpack_cond(ins.operand)
        if meas:
            raise NotImplementedError(f"COND MEAS lands with the v1 model (pc {self.pc})")
        if kind not in (CondKind.FALL, CondKind.RISE):
            raise NotImplementedError(f"COND {kind.name} lands in Phase 4 (pc {self.pc})")
        level = 1 if kind == CondKind.RISE else 0
        self._step_start(ins)
        if self.does(ins):
            planned = self.tb + p.nom
            yield from self.wait_until(planned)
            if p.bounded:
                self._late(planned, slot0=p.nom == 0)
            self.drive(ins.line, level)
            self.tb = self.now
            self._reanchor(self.now)
            self._record(ins, "do")
            self.pc += 1
            yield
            return
        while True:
            if self.edge(ins.line) and self.inp[ins.line] == level:
                e = self.now - EDGE_LAT
                m = e - self.tb
                if p.min > 0 and m < p.min - QUANT:
                    self.log("EARLY", measured=m, lo=p.min, hi=p.max)
                self.tb = e
                self._reanchor(e)
                break
            if p.bounded and self.now - EDGE_LAT > self.tb + p.max + QUANT:
                self.log("TIMEOUT", measured=self.now - EDGE_LAT - self.tb, lo=p.min, hi=p.max)
                if ins.flags & F_ABORT:
                    self._abort()
                    yield
                    return
                self.to = True
                self.tb += p.nom
                self._reanchor(self.tb)
                break
            self.idle = not p.bounded  # waiting forever on the other party
            yield
        self._record(ins, "follow")
        self.pc += 1
        yield

    def _op_hold(self, ins: Instr) -> Gen:
        p = self.image.profiles[ins.prof]
        level = ins.operand & 1
        self._step_start(ins)
        planned = self.tb
        if self.does(ins):
            yield from self.wait_until(planned)
            if ins.prof != 0:  # IDLE steps never overrun
                self._late(planned, slot0=True)
            self.drive(ins.line, level)
            self.tb = planned + p.nom
            self._accumulate(p, 1)
            self._record(ins, "do", level)
            self.pc += 1
            yield
            return
        at = planned + p.nom // 2 + EDGE_LAT  # the midpoint of the owner's slot
        if self.now > at:
            self.log("OVERRUN", measured=self.now - at)
        yield from self.wait_until(at)
        v = self.inp[ins.line]
        self.samples.append(Sample(self.pc, ins.line, self.now, p.nom))
        if v != level:
            self.log("LEVEL", measured=v)
            if ins.flags & F_ABORT:
                self._abort()
                yield
                return
        self.tb = planned + p.nom
        self._accumulate(p, 1)
        self._record(ins, "follow", v)
        self.pc += 1
        yield

    def _op_xfer(self, ins: Instr) -> Gen:
        p = self.image.profiles[ins.prof]
        lane = self.image.lanes[ins.line]
        n, src, dst = unpack_xfer(ins.operand)
        msb = bool(ins.flags & F_MSB)
        if src == Src.FZ:
            raise NotImplementedError(f"XFER src=FZ lands in Task 20 (pc {self.pc})")
        if lane.clk is not None:
            yield from self._xfer_clocked(ins, p, lane, n, src, dst, msb)
            return
        self._step_start(ins)
        line, P, mask = lane.data, p.nom, (1 << n) - 1

        def pos(k: int) -> int:  # value bit carried by wire slot k
            return n - 1 - k if msb else k

        if self.does(ins):
            if src == Src.FIFO:
                if not self.tx:
                    while not self.tx:  # checked every cycle: a step isn't tied to own cycles
                        self.idle = True
                        yield
                    self.tb = max(self.tb, self.now)  # the stall re-base: the step starts now
                v = self.tx.popleft()
            elif src == Src.ZERO:
                v = 0
            elif src == Src.ONES:
                v = mask
            else:
                v = self.regs[reg_of(src)]
            v &= mask
            base = self.tb
            if self.lines[line].kind == "pp":  # dst = src; complete on the last bit
                for k in range(n):
                    planned = base + k * P
                    yield from self.wait_until(planned)
                    self._late(planned, slot0=k == 0)
                    self.drive(line, v >> pos(k) & 1)
                    if k < n - 1:
                        yield
            else:
                # an OD owner reads each bit back where a follower samples it (a 0 from someone
                # else shows up), at most one per cycle, and completes on the last one
                got, k, j = 0, 0, 0
                while True:
                    if k < n and self.now >= base + k * P:
                        self._late(base + k * P, slot0=k == 0)
                        self.drive(line, v >> pos(k) & 1)
                        k += 1
                    if self.now >= base + j * P + P // 2 + EDGE_LAT:
                        got |= self.inp[line] << pos(j)
                        j += 1
                        if j == n:
                            break
                    yield
                v = got
            self.tb = base + n * P
            mode = "do"
        else:
            _, acc_lo, acc_hi = self.acc
            a, ak, v, k = self.tb, 0, 0, 0
            fresh = True  # this cycle's edge has not been looked at yet
            while True:
                if fresh and self.edge(line):
                    # an edge seen before sample k sits at boundary k
                    e = self.now - EDGE_LAT
                    m = e - self.anchor
                    lo = acc_lo + k * p.min - QUANT
                    hi = min(BIG, acc_hi + (BIG if p.max == INF else k * p.max)) + QUANT
                    inside = lo <= m <= hi
                    if ins.flags & F_CHK and not inside:
                        self.log("EDGE", measured=m, lo=lo, hi=hi)
                        if ins.flags & F_ABORT:
                            self._abort()
                            yield
                            return
                    if ins.flags & F_RESYNC and inside:
                        a, ak = e, k
                fresh = False
                at = a + (k - ak) * P + P // 2 + EDGE_LAT
                if self.now >= at:
                    if self.now > at:
                        self.log("OVERRUN", measured=self.now - at)
                    self.samples.append(Sample(self.pc, line, self.now, P))
                    v |= self.inp[line] << pos(k)
                    k += 1
                    if k == n:
                        break
                # at most one sample per cycle: an overdue one waits for the next (isa §4.3)
                yield
                fresh = True
            self.tb = a + (n - ak) * P
            mode = "follow"
            if dst == Dst.FIFO:
                self._push_rx(v)
        if dst >= Dst.R0:
            self.regs[reg_of(dst)] = v & REG_MASK
        self._accumulate(p, n)
        self._record(ins, mode, v)
        self.pc += 1
        yield

    def _xfer_clocked(self, ins: Instr, p, lane, n: int, src: Src, dst: Dst, msb: bool) -> Gen:
        """Clocked XFER on one data line (isa §4.1, §4.3), with H = nom.

        The clock owner (the clk line's party, never MON) drives leading edge k (the clock goes to
        1 - cpol) at tb + (2k+1)H and trailing edge k (to cpol) at tb + (2k+2)H, then
        tb := tb + 2nH. Data is captured on the leading edges with cpha 0 and on the trailing ones
        with cpha 1, and launched on the others; with cpha 0, bit 0 goes out at the step start.
        Every other engine follows the clock, telling the edges apart by level."""
        if lane.data2 is not None or lane.stretch or self.lines[lane.data].kind == "od":
            raise NotImplementedError(f"duplex, stretch and OD data on a clocked lane land in V3b "
                                      f"part 2 (pc {self.pc})")
        self._step_start(ins)
        H, cpol, cpha, ld = p.nom, lane.cpol, lane.cpha, lane.ldelay
        data, clk, mask = lane.data, lane.clk, (1 << n) - 1
        own_clk = self.role != Role.MON and int(self.lines[clk].owner) == int(self.role)
        do = self.does(ins)

        def pos(k: int) -> int:  # value bit carried by bit slot k
            return n - 1 - k if msb else k

        v = 0
        if do:
            if src == Src.FIFO:
                if own_clk:
                    while not self.tx:  # checked every cycle; the step starts when it takes the word
                        self.idle = True
                        yield
                    v = self.tx.popleft()
                elif self.tx:
                    v = self.tx.popleft()
                else:
                    self.log("UNDERRUN")  # it can't hold the clock: send ONES, consume nothing
                    v = mask
            elif src == Src.ONES:
                v = mask
            elif src != Src.ZERO:
                v = self.regs[reg_of(src)]
            v &= mask
        start, got = self.now, 0

        if own_clk:
            # never overruns (isa §4.1): bit 0 (cpha 0) or the first edge (cpha 1) is never late
            self.tb = max(self.tb, start) if cpha == 0 else max(self.tb, start - H)
            base = self.tb
            edges = [(base + j * H, cpol ^ (j & 1)) for j in range(1, 2 * n + 1)]
            if do:
                launch = ([base] + [base + 2 * k * H + ld for k in range(1, n)] if cpha == 0
                          else [base + (2 * k + 1) * H + ld for k in range(n)])
                samp: list[int] = []
            else:
                launch = []
                samp = [base + (2 * k + 1 + cpha) * H + EDGE_LAT for k in range(n)]
            end = max([base + 2 * n * H] + samp)  # the later of the last edge and the last sample
            ei = li = si = 0
            while True:
                while ei < len(edges) and edges[ei][0] <= self.now:
                    self.drive(clk, edges[ei][1])
                    ei += 1
                while li < len(launch) and launch[li] <= self.now:
                    self.drive(data, v >> pos(li) & 1)
                    li += 1
                if si < len(samp) and self.now >= samp[si]:
                    self.samples.append(Sample(self.pc, data, self.now, H))
                    got |= self.inp[data] << pos(si)
                    si += 1
                if self.now >= end:
                    break
                yield
            self.tb = base + 2 * n * H
        else:
            # cpha 0: bit 0 at max(start, tb); edge launches at max(detect, e + ldelay)
            pending = [(max(start, self.tb), 0)] if do and cpha == 0 else []
            nlead = ntrail = kc = 0
            last: int | None = None     # detect cycle of this step's last edge
            ref = start                 # the timeout counts from the start, then from each edge
            while True:
                if self.edge(clk):
                    if (last is not None and ins.flags & F_CHK and self.now - last < p.min - QUANT):
                        self.log("CLK_PHASE", measured=self.now - last, lo=p.min, hi=p.max)
                        if ins.flags & F_ABORT:
                            self._abort()
                            yield
                            return
                    last = ref = self.now
                    e = self.now - EDGE_LAT
                    leading = self.inp[clk] != cpol
                    if leading == (cpha == 0) and not do and kc < n:  # a capture edge
                        self.samples.append(Sample(self.pc, data, self.now, H))
                        got |= self.inp[data] << pos(kc)
                        kc += 1
                    if leading:
                        if do and cpha == 1 and nlead < n:
                            pending.append((max(self.now, e + ld), nlead))
                        nlead += 1
                    else:
                        ntrail += 1
                        if do and cpha == 0 and ntrail < n:
                            pending.append((max(self.now, e + ld), ntrail))
                elif p.max != INF and self.now - ref > p.max + QUANT:  # an edge wins (isa §4.1)
                    self.log("TIMEOUT", measured=self.now - ref, lo=p.min, hi=p.max)
                    if ins.flags & F_ABORT:
                        self._abort()
                        yield
                        return
                    # no dst, no push; the step ends here
                    self.to = True
                    self.tb = self.now
                    self._reanchor(self.now)
                    self._record(ins, "do" if do else "follow")
                    self.pc += 1
                    yield
                    return
                while pending and pending[0][0] <= self.now:
                    self.drive(data, v >> pos(pending[0][1]) & 1)
                    pending.pop(0)
                if ntrail == n:
                    break
                yield
            self.tb = self.now - EDGE_LAT
        if not do:
            v = got
            if dst == Dst.FIFO:
                self._push_rx(v)
        if dst >= Dst.R0:
            self.regs[reg_of(dst)] = v & REG_MASK
        self._reanchor(self.tb)  # a clocked XFER is an anchor (isa §4.3, §6)
        self._record(ins, "do" if do else "follow", v)
        self.pc += 1
        yield
