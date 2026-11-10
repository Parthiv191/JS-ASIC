"""Multi-engine system: engines on a shared bus, per-engine clock enables, host FIFOs.

One global tick: resolve the wires from every engine's registered drives (glitches
override), then run one cycle of every engine whose clock enable is high. A drive
made in tick t is therefore on the wire from tick t+1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..image import Image, LineCfg
from ..isa import SYNC_LAT, Role
from .bus import resolve
from .engine import Branch, Engine, Event, Sample, StepRecord


@dataclass(frozen=True)
class EngineSpec:
    image: Image
    role: Role
    clock: tuple[int, int] = (1, 1)   # engine cycles per tick = num/den, num <= den
    phase: int = 0                    # ticks before the engine's first cycle


@dataclass(frozen=True)
class Glitch:
    wire: int
    start: int
    length: int
    value: int


@dataclass
class EngineTrace:
    name: str
    role: Role
    records: list[StepRecord]
    events: list[Event]
    rx: list[int]
    samples: list[Sample]
    branches: list[Branch]
    halted: bool
    idle: bool
    tx_left: int
    cycle_tick: list[int]  # tick of every clock enable, also after HALT (the clock keeps running);
    #                         engine cycle c (1-based) ran in tick cycle_tick[c - 1]
    starts: list[tuple[int, int]] = field(default_factory=list)  # (pc, now) per instruction start
    halt_cycle: int | None = None  # the HALT's (or ILLEGAL's) start cycle
    drives: list[tuple[int | None, ...]] = field(default_factory=list)  # per tick, the drive that
    #                                                                     tick's wire was resolved from

    @property
    def violations(self) -> list[Event]:
        return [e for e in self.events if e.violation]


@dataclass
class Trace:
    ticks: int
    quiescent: bool
    lines: list[LineCfg]
    engines: list[EngineTrace]
    wire_edges: list[list[int]]
    contention: list[tuple[int, int]] = field(default_factory=list)
    glitches: list[Glitch] = field(default_factory=list)


def enabled(spec: EngineSpec, tick: int) -> bool:
    """Clock enable: ceil(u*num/den) engine cycles after u ticks; the first is at `phase`."""
    u = tick - spec.phase
    if u < 0:
        return False
    num, den = spec.clock
    return -(-(u + 1) * num // den) > -(-u * num // den)


class System:
    def __init__(self, engines: list[EngineSpec], glitches: list[Glitch] | tuple = ()):
        if not engines:
            raise ValueError("need at least one engine")
        for s in engines:
            num, den = s.clock
            if not 0 < num <= den:
                raise ValueError(f"clock ratio {num}/{den} must be in (0, 1]")
        self.specs = list(engines)
        self.lines = engines[0].image.lines
        self.engines = [Engine(s.image, s.role, name=f"E{i}") for i, s in enumerate(engines)]
        self.glitches = list(glitches)
        self._pushes: list[tuple[int, int, list[int]]] = []
        self._cycle_tick: list[list[int]] = [[] for _ in engines]

    def host_push(self, idx: int, words: list[int], at_tick: int = 0) -> None:
        self._pushes.append((at_tick, idx, list(words)))

    def run(self, max_ticks: int, stop_when_quiescent: bool = True) -> Trace:
        n = len(self.lines)
        edges: list[list[int]] = [[] for _ in range(n)]
        contention: list[tuple[int, int]] = []
        prev: list[int] | None = None
        started = [False] * len(self.engines)
        quiescent = False
        last_glitch = max((g.start + g.length for g in self.glitches), default=0)
        # An idle follower may still have an edge crossing its synchronizer: only stop once
        # every engine has had SYNC_LAT + 1 cycles since the last wire edge.
        gap = max(-(-s.clock[1] // s.clock[0]) for s in self.specs)
        settle = (SYNC_LAT + 1) * gap
        last_edge = -settle - 1
        drives: list[list[tuple[int | None, ...]]] = [[] for _ in self.engines]
        tick = 0
        while tick < max_ticks:
            if self._pushes:
                due = [p for p in self._pushes if p[0] <= tick]
                for p in due:
                    self.engines[p[1]].tx.extend(p[2])
                    self._pushes.remove(p)
            for log, e in zip(drives, self.engines):
                log.append(tuple(e.drv))
            wire, cont = resolve(self.lines, [e.drv for e in self.engines])
            for g in self.glitches:
                if g.start <= tick < g.start + g.length:
                    wire[g.wire] = g.value
            for w in cont:
                contention.append((tick, w))
            if tick == 0:
                for e in self.engines:
                    e.preload(wire)
            if prev is not None:
                for i in range(n):
                    if wire[i] != prev[i]:
                        edges[i].append(tick)
                        last_edge = tick
            prev = wire
            for i, (e, s) in enumerate(zip(self.engines, self.specs)):
                if enabled(s, tick):
                    e.cycle(wire)
                    self._cycle_tick[i].append(tick)
                    started[i] = True
            tick += 1
            if (stop_when_quiescent and all(started) and not self._pushes and tick >= last_glitch
                    and tick - last_edge > settle and all(e.idle for e in self.engines)):
                quiescent = True
                break
        return Trace(
            ticks=tick,
            quiescent=quiescent,
            lines=list(self.lines),
            engines=[
                EngineTrace(e.name, e.role, e.records, e.events, e.rx, e.samples, e.branches,
                            e.halted, e.idle, len(e.tx), self._cycle_tick[i], e.starts, e.halt_cycle,
                            drives[i])
                for i, e in enumerate(self.engines)
            ],
            wire_edges=edges,
            contention=contention,
            glitches=list(self.glitches),
        )
