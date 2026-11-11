"""Multi-engine system: engines on a shared bus, per-engine clock enables, host FIFOs.

One global tick: the host writes TX words while there is room and (by default) reads
every RX word; resolve the wires from every engine's registered drives (glitches
override), then run one cycle of every engine whose clock enable is high. A drive
made in tick t is therefore on the wire from tick t+1.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..image import Image, LineCfg
from ..isa import SYNC_LAT, TX_DEPTH, Role
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
    rx: list[int]  # every word the host got from the RX FIFO, during or after the run
    samples: list[Sample]
    branches: list[Branch]
    halted: bool
    idle: bool
    tx_left: int   # words the engine never popped: in its TX FIFO or still with the host
    cycle_tick: list[int]  # tick of every clock enable, also after HALT (the clock keeps running);
    #                         engine cycle c (1-based) ran in tick cycle_tick[c - 1]
    starts: list[tuple[int, int]] = field(default_factory=list)  # (pc, now) per instruction start
    bus_events: list[tuple[int, str]] = field(default_factory=list)  # detector confirmations (isa §4.7)
    evt: int = 0  # the sticky event register at the end of the run (isa §4.1)

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


def enabled(spec: EngineSpec, tick: int) -> bool:
    """Clock enable: ceil(u*num/den) engine cycles after u ticks; the first is at `phase`."""
    u = tick - spec.phase
    if u < 0:
        return False
    num, den = spec.clock
    return -(-(u + 1) * num // den) > -(-u * num // den)


class System:
    def __init__(self, engines: list[EngineSpec], glitches: list[Glitch] | tuple = (),
                 host_reads_rx: bool = True):
        """`host_reads_rx`: the host empties every RX FIFO each tick (it keeps up), or with False
        only after the run, as co-sim's host does, so a 9th word overflows."""
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
        self._backlog: list[deque[int]] = [deque() for _ in engines]  # host words waiting for room
        self._got: list[list[int]] = [[] for _ in engines]            # RX words the host read
        self.host_reads_rx = host_reads_rx
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
        tick = 0
        while tick < max_ticks:
            if self._pushes:
                due = [p for p in self._pushes if p[0] <= tick]
                for p in due:
                    self._backlog[p[1]].extend(p[2])
                    self._pushes.remove(p)
            # the host writes TX only while there is room (it polls the FIFO counts)
            for e, b in zip(self.engines, self._backlog):
                while b and len(e.tx) < TX_DEPTH:
                    e.tx.append(b.popleft())
            if self.host_reads_rx:
                for e, got in zip(self.engines, self._got):
                    got.extend(e.rx)
                    e.rx.clear()
            wire, cont = resolve(self.lines, [e.drv for e in self.engines])
            for g in self.glitches:
                if g.start <= tick < g.start + g.length:
                    wire[g.wire] = g.value
            for w in cont:
                contention.append((tick, w))
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
                EngineTrace(e.name, e.role, e.records, e.events, self._got[i] + e.rx, e.samples, e.branches,
                            e.halted, e.idle,
                            len(e.tx) + len(self._backlog[i]) + sum(len(p[2]) for p in self._pushes if p[1] == i),
                            self._cycle_tick[i], e.starts, bus_events=e.bus_events, evt=e.evt)
                for i, e in enumerate(self.engines)
            ],
            wire_edges=edges,
            contention=contention,
        )
