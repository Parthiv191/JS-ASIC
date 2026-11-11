"""Co-simulation's model side (test/cosim/README.md): one model engine's records as trace-port
pulses (architecture §5), the comparator that finds the first difference, and the replay of the
other engines' wire into the RTL's uio_in."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .image import Image
from .model.bus import resolve
from .model.system import EngineSpec, Glitch, System, Trace

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


def stimulus(trace: Trace, e: int) -> list[int]:
    """Per tick, the `uio_in` byte the RTL must see while it plays engine `e`: the wire of every
    other engine (undriven reads 1), with the run's glitches, line i on pin LINE[i].pin."""
    others = [et.drives for i, et in enumerate(trace.engines) if i != e]
    pins = _pin_map(trace)
    out = []
    for t in range(trace.ticks):
        wire, _ = resolve(trace.lines, [d[t] for d in others])
        for g in trace.glitches:
            if g.start <= t < g.start + g.length:
                wire[g.wire] = g.value
        byte = 0xFF  # pins with no line read 1
        for i, v in enumerate(wire):
            if not v:
                byte &= ~(1 << pins[i])
        out.append(byte)
    return out


def _pin_map(trace: Trace) -> list[int]:
    return [i if ln.pin is None else ln.pin for i, ln in enumerate(trace.lines)]


def pins(trace: Trace, e: int, ticks: range) -> list[tuple[int, int]]:
    """Per tick, the (uio_oe, uio_out) bytes model engine `e` puts on the pins: a driven line
    sets its pin's oe bit and its out bit to the level; out is 0 where oe is 0 (it never reaches
    the wire). Before RUN (tick < 0) every pin is an input; past the run the last drive holds.
    Equal entries share one tuple, so a run of millions of ticks stays small."""
    drives, pin = trace.engines[e].drives, _pin_map(trace)
    seen: dict[tuple[int | None, ...], tuple[int, int]] = {}

    def byte(d: tuple[int | None, ...]) -> tuple[int, int]:
        if d not in seen:
            oe = lvl = 0
            for i, v in enumerate(d):
                if v is not None:
                    oe |= 1 << pin[i]
                    lvl |= (v & 1) << pin[i]
            seen[d] = (oe, lvl)
        return seen[d]

    return [(0, 0) if t < 0 else byte(drives[min(t, len(drives) - 1)]) for t in ticks]


ACT_FROM = 2  # ACT first reads 1 in the cycle with now = 2 (architecture §8)


def act(trace: Trace, e: int, ticks: range) -> list[int]:
    """Per tick, the ACT pin (uo_out[7]) for model engine `e`: its running state through one
    flop (architecture §8). It runs from now = 1 through its HALT's start cycle, so ACT reads 1
    from now = 2 through that cycle + 1, and stays 1 if it never halts."""
    h = trace.engines[e].halt_cycle
    last = None if h is None else h + 1
    return [int(t >= ACT_FROM and (last is None or t <= last)) for t in ticks]


def _pin_state(oe: int, out: int, p: int) -> str:
    return f"driven {out >> p & 1}" if oe >> p & 1 else "released"


@dataclass
class PinMismatch:
    tick: int
    pins: list[int]               # the pins that differ
    expected: tuple[int, int]     # (uio_oe, uio_out)
    observed: tuple[int, int]

    def __str__(self) -> str:
        lines = [f"pin mismatch at tick {self.tick}:"]
        lines += [f"  pin {p}: expected {_pin_state(*self.expected, p)}, observed {_pin_state(*self.observed, p)}"
                  for p in self.pins]
        return "\n".join(lines)


def compare_pins(expected: list[tuple[int, int]], observed: list[tuple[int, int]],
                 first: int) -> PinMismatch | None:
    """The first tick where the pins differ, or None. Entry k of both lists is tick `first` + k.
    A pin differs when its oe differs, or when both drive it to different levels."""
    for k, ((eo, ev), (oo, ov)) in enumerate(zip(expected, observed, strict=True)):
        bad = (eo ^ oo) | (eo & oo & (ev ^ ov))
        if bad:
            return PinMismatch(first + k, [p for p in range(8) if bad >> p & 1], (eo, ev), (oo, ov))
    return None


@dataclass
class Replay:
    """One model run, ready to replay into the RTL playing engine `engine`."""
    image: Image
    trace: Trace
    engine: int
    expected: list[Pulse]
    stimulus: list[int]  # uio_in per tick


def replay(specs: list[EngineSpec], engine: int, glitches: list[Glitch] | tuple = (),
           tx: dict[int, list[int]] | None = None, max_ticks: int = 50_000) -> Replay:
    """Run every party on the model; the RTL will play `specs[engine]`. Raises ValueError when
    the run can't be replayed exactly (test/cosim/README.md §1)."""
    me = specs[engine]
    if me.clock != (1, 1) or me.phase != 0:
        raise ValueError(f"the RTL's engine must run at clock (1, 1) and phase 0, not {me.clock}, {me.phase}")
    if any(s.image != me.image for s in specs):
        raise ValueError("every engine must run the same image")
    if (tx or {}).get(engine):
        raise ValueError("no host words for the RTL's engine until the RTL has FIFOs")
    s = System(list(specs), glitches=glitches, host_reads_rx=False)  # the harness reads RX after the run
    for i, words in (tx or {}).items():
        s.host_push(i, words)
    trace = s.run(max_ticks)
    if not trace.quiescent:
        raise ValueError(f"the model run didn't settle within {max_ticks} ticks")
    if trace.contention:
        raise ValueError(f"push-pull contention in the model run: (tick, line) {trace.contention[:4]}")
    own = trace.engines[engine].drives
    for g in glitches:
        od_pull = trace.lines[g.wire].kind == "od" and g.value == 0
        for t in range(g.start, min(g.start + g.length, trace.ticks)):
            if own[t][g.wire] is not None and not od_pull:
                raise ValueError(f"glitch on line {g.wire} at tick {t} is over the RTL's own drive")
    return Replay(me.image, trace, engine, expected(trace, engine), stimulus(trace, engine))
