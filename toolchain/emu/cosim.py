"""Co-simulation's model side (test/cosim/README.md): one model engine's records as trace-port
pulses (architecture §5), and the comparator that finds the first difference."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .model.system import Trace

MASK24 = (1 << 24) - 1  # times compare modulo 2^24 (architecture §5)

# isa §7 event kind codes, the bit numbers of tr_evt
EVENT_CODES = {
    "OVERRUN": 0, "EARLY": 1, "TIMEOUT": 2, "LEVEL": 3, "EDGE": 4, "CLK_PHASE": 5, "ABORT": 6,
    "OVERFLOW": 7, "UNDERRUN": 8, "BUS": 9, "ILLEGAL": 10,
}
STEP_OPS = {"COND": 1, "HOLD": 2, "XFER": 3}  # tr_op codes


class Kind(IntEnum):
    """Pulse kinds, in their sort order within one cycle."""
    ISSUE = 0
    STEP = 1
    EVT = 2
    MARK = 3
    BR = 4
    SAMPLE = 5
    HALT = 6


@dataclass(frozen=True, order=True)
class Pulse:
    """One trace-port pulse. Fields: STEP (op, do, value, tb), EVT (code,), MARK (id,),
    BR (taken,), SAMPLE (line,); ISSUE and HALT have none."""
    now: int
    kind: Kind
    pc: int
    fields: tuple[int, ...] = ()

    def __str__(self) -> str:
        return f"now {self.now:>6}  {self.kind.name:<6} pc {self.pc:>3}  {self.fields if self.fields else ''}"


def pulse(now: int, kind: Kind, pc: int, *fields: int) -> Pulse:
    return Pulse(now & MASK24, Kind(kind), pc, tuple(fields))


def expected(trace: Trace, e: int) -> list[Pulse]:
    """Model engine `e`'s records as the pulses its RTL trace port must show, sorted."""
    et = trace.engines[e]
    out = [pulse(now, Kind.ISSUE, pc) for pc, now in et.starts]
    for r in et.records:
        out.append(pulse(r.cycle, Kind.STEP, r.pc, STEP_OPS[r.op], int(r.mode == "do"),
                         0 if r.value is None else r.value, r.tb & MASK24))
    for ev in et.events:
        if ev.kind == "MARK":
            out.append(pulse(ev.cycle, Kind.MARK, ev.pc, ev.measured))
        elif ev.kind in EVENT_CODES:
            out.append(pulse(ev.cycle, Kind.EVT, ev.pc, EVENT_CODES[ev.kind]))
        else:
            raise ValueError(f"model event {ev.kind!r} has no trace-port code")
    out += [pulse(b.cycle, Kind.BR, b.pc, int(b.taken)) for b in et.branches]
    out += [pulse(s.cycle, Kind.SAMPLE, s.pc, s.line) for s in et.samples]
    if et.halt_cycle is not None:
        out.append(pulse(et.halt_cycle, Kind.HALT, et.starts[-1][0] if et.starts else 0))
    return sorted(out)


@dataclass
class Mismatch:
    index: int
    expected: Pulse | None
    observed: Pulse | None
    matched: list[Pulse] = field(default_factory=list)  # the last pulses that agreed

    def __str__(self) -> str:
        lines = [f"co-sim mismatch at pulse {self.index}:",
                 f"  expected  {self.expected if self.expected else '(nothing more)'}",
                 f"  observed  {self.observed if self.observed else '(nothing more)'}"]
        if self.matched:
            lines.append("  last matched:")
            lines += [f"            {p}" for p in self.matched]
        return "\n".join(lines)


def compare(expected: list[Pulse], observed: list[Pulse], context: int = 5) -> Mismatch | None:
    """The first pulse where the two runs differ, or None. Both are sorted first, so pulses in
    one cycle may come in any order."""
    exp, obs = sorted(expected), sorted(observed)
    for i in range(max(len(exp), len(obs))):
        a = exp[i] if i < len(exp) else None
        b = obs[i] if i < len(obs) else None
        if a != b:
            return Mismatch(i, a, b, exp[max(0, i - context):i])
    return None
