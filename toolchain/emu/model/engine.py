"""Cycle-level engine interpreter (docs/design/isa-v0.md §4).

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
    F_REG, REG_MASK, SYNC_LAT, AluOp, BrCond, Instr, Op, Owner, RawOp, Role, unpack_role_if,
)

VIOLATIONS = frozenset({"OVERRUN", "EARLY", "TIMEOUT", "LEVEL", "EDGE", "CLK_PHASE"})


@dataclass(frozen=True)
class StepRecord:
    pc: int
    op: str
    mode: str          # "do" | "follow"
    value: int | None
    cycle: int


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
    pc: int
    taken: bool


Gen = Iterator[None]


class Engine:
    def __init__(self, image: Image, role: Role, name: str = ""):
        self.image = image
        self.role = Role(role)
        self.name = name or self.role.name
        self.instrs: list[Instr] = image.instrs()
        self.lines = image.lines
        self.pc = 0
        self.regs = [0, 0, 0, 0]
        self.tb = 0
        self.now = -1
        self.drv: list[int | None] = [
            ln.idle if ln.kind == "pp" and self.role != Role.MON and int(ln.owner) == int(self.role) else None
            for ln in self.lines
        ]
        self.inp: list[int] | None = None
        self.prev_inp: list[int] | None = None
        self._pipe: list[list[int]] | None = None
        self.tx: deque[int] = deque()
        self.rx: list[int] = []
        self.records: list[StepRecord] = []
        self.events: list[Event] = []
        self.samples: list[Sample] = []
        self.branches: list[Branch] = []
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
        if self._pipe is None:  # preload: as if the wire had always looked like this
            self._pipe = [w] * SYNC_LAT
            self.inp = w
        self.now += 1
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

    def log(self, kind: str, measured=None, lo=None, hi=None) -> None:
        self.events.append(Event(kind, self.pc, self.now, measured, lo, hi))

    def wait_until(self, t: int) -> Gen:
        while self.now < t:
            yield

    # ------------------------------------------------------------------ interpreter
    def _run(self) -> Gen:
        while True:
            if self.pc >= len(self.instrs):
                yield from self._halt()
            ins = self.instrs[self.pc]
            handler = getattr(self, f"_op_{ins.op.name.lower()}", None)
            if handler is None:
                raise NotImplementedError(f"{ins.op.name} is reserved in ISA v0 (pc {self.pc})")
            yield from handler(ins)

    def _halt(self) -> Gen:
        self.halted = True
        self.halt_cycle = self.now
        while True:
            self.idle = True
            yield

    def _op_halt(self, ins: Instr) -> Gen:
        yield from self._halt()

    def _op_nop(self, ins: Instr) -> Gen:
        self.pc += 1
        yield

    def _op_li(self, ins: Instr) -> Gen:
        self.regs[ins.pin & 3] = ins.operand
        self.pc += 1
        yield

    def _op_alu(self, ins: Instr) -> Gen:
        a = self.regs[ins.pin >> 2 & 3]
        b = self.regs[ins.operand & 3] if ins.flags & F_REG else ins.operand
        op = AluOp(ins.prof)
        r = {
            AluOp.ADD: lambda: a + b, AluOp.SUB: lambda: a - b, AluOp.AND: lambda: a & b,
            AluOp.OR: lambda: a | b, AluOp.XOR: lambda: a ^ b, AluOp.SHL: lambda: a << (b & 15),
            AluOp.SHR: lambda: a >> (b & 15), AluOp.EQ: lambda: int(a == b),
            AluOp.NE: lambda: int(a != b), AluOp.LT: lambda: int(a < b),
        }[op]()
        self.regs[ins.pin & 3] = r & REG_MASK
        self.pc += 1
        yield

    def _op_br(self, ins: Instr) -> Gen:
        cond = BrCond(ins.prof & 3)
        if cond == BrCond.TO:
            raise NotImplementedError(f"BR TO needs the timeout flag from Task 7 (pc {self.pc})")
        if cond == BrCond.ALWAYS:
            taken = True
        else:
            zero = self.regs[ins.pin & 3] == 0
            taken = zero if cond == BrCond.Z else not zero
            self.branches.append(Branch(self.pc, taken))
        self.pc = ins.operand if taken else self.pc + 1
        yield

    def _op_role_if(self, ins: Instr) -> Gen:
        mask, count = unpack_role_if(ins.operand)
        self.pc += 1 if mask >> int(self.role) & 1 else 1 + count
        yield

    def _op_push(self, ins: Instr) -> Gen:
        self.rx.append(self.regs[ins.pin & 3])
        self.pc += 1
        yield

    def _op_pop(self, ins: Instr) -> Gen:
        while not self.tx:
            self.idle = True
            yield
        self.regs[ins.pin & 3] = self.tx.popleft() & REG_MASK
        self.pc += 1
        yield

    def _op_mark(self, ins: Instr) -> Gen:
        self.log("MARK", measured=ins.operand)
        self.pc += 1
        yield

    def _op_raw(self, ins: Instr) -> Gen:
        op = RawOp(ins.prof)
        nxt = self.pc + 1
        if op == RawOp.SET:
            self.drive(ins.pin, ins.operand & 1)
        elif op == RawOp.REL:
            self.drv[ins.pin] = None
        elif op == RawOp.GET:
            self.regs[ins.operand & 3] = self.inp[ins.pin]
        elif op == RawOp.JPIN:
            if self.inp[ins.pin] == ins.operand >> 11 & 1:
                nxt = ins.operand & 0x7FF
        elif op == RawOp.DELAY:
            self.pc = nxt
            for _ in range(max(1, ins.operand)):
                yield
            return
        self.pc = nxt
        yield
