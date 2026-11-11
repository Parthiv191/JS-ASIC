"""Devices under fuzz: small state machines with planted hidden behaviour (bus-fuzzer.md §6
stage 1), each compiled to one description and run on the engine model.

The bus is SPI with a ready line: CS framing, then per message byte the controller (engine 0,
the fuzzer, party `ctrl`) clocks the byte out, the device (engine 1, party `dev`) computes and
toggles RDY, and the controller clocks the reply in. RDY is a bounded followed COND with ABORT
on the controller, so a device that stops answering is a fatal abort: a hang.

Every iteration runs on a fresh `System`: that is the reset prefix (§4), so the device is
stateless across iterations. The controller's TX FIFO stands in for `XFER src=FZ` (the model
has no FZ ops yet), and the fingerprint tokens are derived from engine 0's trace exactly as the
auto-tokens of §3 define them. This is the host-driven loop of stage 2, run on the model.

A device's observable behaviour depends only on its state path, so `Target` runs the model
once per distinct path and caches the observation; `simulate` always runs it.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from ..compiler import compile_desc
from ..desc import Description
from ..image import Image
from ..isa import INF, Op, Role, unpack_xfer
from ..model.system import EngineSpec, System, Trace
from ..protocols import slack
from .unit import bucket

H = 8                # SCK half period, cycles (T3-latency needs >= 7 at 2%)
RDY_MAX = 4          # the controller's RDY timeout, as a multiple of nom
LEAK_RDY_MAX = 1024  # the RDY timeout of a device with delays
ARM_MARK = 1         # the MARK that stands for fz.next: tokens count from here
MAX_TICKS = 40_000


@dataclass(frozen=True)
class Observation:
    tokens: tuple[int, ...]   # the data tokens, in step-completion order
    events: tuple[str, ...]   # engine 0's event kinds this iteration
    hang: bool                # a fatal abort on engine 0 (ABORT)


@dataclass(frozen=True)
class TableDevice:
    """A device as a state table. State 0 at CS fall; per received byte b in state s:
    `rules[s][b]` if present, else `default.get(s, 0)`. The reply to each byte is
    `reply[new state]` (0 if absent). Entering a state in `halt` makes the device stop
    (it never toggles RDY again). `delay[s]` cycles are spent before RDY in state s: a
    timing leak. Such a device's RDY is late by design, so it is compiled without the T rules
    (they would rightly flag its OVERRUN); the controller's side is unchanged."""
    name: str
    length: int
    rules: dict[int, dict[int, int]]
    default: dict[int, int] = field(default_factory=dict)
    reply: dict[int, int] = field(default_factory=dict)
    halt: frozenset[int] = frozenset()
    delay: dict[int, int] = field(default_factory=dict)
    planted: int = -1          # the state that is the planted behaviour
    doc: str = ""

    def states(self) -> list[int]:
        s = {0, *self.rules, *self.default.values(), *self.reply, *self.halt, *self.delay}
        for r in self.rules.values():
            s |= set(r.values())
        return sorted(s)

    def path(self, msg: bytes) -> tuple[int, ...]:
        """The state after each byte; ends early at a halt state."""
        s, out = 0, []
        for b in msg[:self.length]:
            s = self.rules.get(s, {}).get(b, self.default.get(s, 0))
            out.append(s)
            if s in self.halt:
                break
        return tuple(out)

    def reached(self, msg: bytes) -> bool:
        return self.planted in self.path(msg)

    # ---- the description -------------------------------------------------------------
    def build(self, ppm: int = 20000) -> Description:
        d = Description(self.name, parties=("ctrl", "dev"), tolerance_ppm=ppm)
        sck = d.line("sck", owner="ctrl", idle=0)
        mosi = d.line("mosi", owner="ctrl", idle=0)
        miso = d.line("miso", owner="dev", idle=0)
        cs = d.line("cs", owner="ctrl", idle=1)
        rdy = d.line("rdy", owner="dev", idle=0)
        half = d.profile("HALF", nom=H, min=max(1, H - slack(H, ppm)), max=INF)
        su = d.profile("CS_SU", nom=2 * H)
        go = d.profile("GO", nom=H, min=1, max=INF)
        if self.delay:   # a slow device: RDY leaves when the compute is done (the owner is late)
            rp = d.profile("RDY", nom=H, min=1, max=LEAK_RDY_MAX)
        else:
            nom = self._rdy_nom()
            rp = d.profile("RDY", nom=nom, min=1, max=RDY_MAX * nom)
        out_l = d.lane("out", data=mosi, clk=sck)
        in_l = d.lane("in", data=miso, clk=sck)
        d.abort_to("txn")
        d.label("txn")
        with d.only("dev"):
            d.li("r2", 0)
        d.cond("rise", rdy, owner="dev", prof=go)          # the device is ready: the reset prefix
        with d.only("ctrl"):
            d.mark(ARM_MARK)                                # where fz.next goes: capture is armed
        d.cond("fall", cs, owner="ctrl")
        d.hold(cs, 0, owner="ctrl", prof=su)
        for i in range(self.length):
            level = i % 2                                   # RDY falls, rises, falls, ...
            d.xfer(8, out_l, owner="ctrl", prof=half, src="fifo", dst="r1", msb_first=True)
            with d.only("dev"):
                self._logic(d, i)
            d.cond("rise" if level else "fall", rdy, owner="dev", prof=rp, abort=True)
            d.xfer(8, in_l, owner="dev", prof=half, src="r4", dst="fifo", msb_first=True)
        d.cond("rise", cs, owner="ctrl", prof=go)
        if self.length % 2 == 0:
            d.cond("fall", rdy, owner="dev", prof=go)       # RDY back to idle
        d.halt()
        return d

    def _rdy_nom(self) -> int:
        """RDY's nominal time covers the device's longest compute path (U = 2 per instruction)."""
        n_rules = sum(len(r) for r in self.rules.values())
        states = len(self.states())
        instrs = 4 * states + 2 * n_rules + 4 * states + 3 * len(self.halt) + 3 * len(self.delay) + 8
        return 2 * instrs + max(self.delay.values(), default=0) + 16

    def _logic(self, d: Description, i: int) -> None:
        """r1 = the byte, r2 = the state; leaves the new state in r2 and the reply in r4."""
        p = f"b{i}_"
        for s in self.states():
            rules = self.rules.get(s, {})
            dflt = self.default.get(s, 0)
            if not rules and dflt == s:
                continue
            d.alu("eq", "r3", "r2", s)
            d.br(f"{p}not{s}", "z", "r3")
            for b, t in sorted(rules.items()):
                d.alu("eq", "r3", "r1", b)
                d.br(f"{p}to{t}", "nz", "r3")
            d.li("r2", dflt)
            d.br(f"{p}next")
            d.label(f"{p}not{s}")
        d.br(f"{p}next")
        for t in sorted({t for r in self.rules.values() for t in r.values()}):
            d.label(f"{p}to{t}")
            d.li("r2", t)
            d.br(f"{p}next")
        d.label(f"{p}next")
        d.li("r4", self.reply.get(0, 0))
        for s, v in sorted(self.reply.items()):
            if s:
                d.alu("eq", "r3", "r2", s)
                d.br(f"{p}rep{s}", "z", "r3")
                d.li("r4", v)
                d.label(f"{p}rep{s}")
        for s in sorted(self.halt):
            d.alu("eq", "r3", "r2", s)
            d.br(f"{p}live{s}", "z", "r3")
            d.halt()
            d.label(f"{p}live{s}")
        for s, n in sorted(self.delay.items()):
            d.alu("eq", "r3", "r2", s)
            d.br(f"{p}fast{s}", "z", "r3")
            d.raw_delay(n)
            d.label(f"{p}fast{s}")


def tokens_of(image: Image, trace: Trace, engine: int = 0) -> Observation:
    """The auto-tokens of bus-fuzzer.md §3 from one engine's trace, from the arm MARK on: a
    followed XFER gives value[7:0] (and value[15:8] if n > 8 or the lane is duplex); a followed
    COND gives 0x40 + bucket(m), m being its response latency (0xFFFF on a timeout)."""
    et = trace.engines[engine]
    instrs = image.instrs()
    arm = next(e.cycle for e in et.events if e.kind == "MARK" and e.measured == ARM_MARK)
    timeouts = {(e.pc, e.cycle) for e in et.events if e.kind == "TIMEOUT"}
    toks: list[int] = []
    tb = 0
    for r in et.records:
        if r.mode == "follow" and r.cycle > arm:
            ins = instrs[r.pc]
            if ins.op == Op.XFER:
                n = unpack_xfer(ins.operand)[0]
                toks.append(r.value & 0xFF)
                if n > 8 or image.lanes[ins.line].data2 is not None:
                    toks.append(r.value >> 8 & 0xFF)
            elif ins.op == Op.COND:
                m = 0xFFFF if (r.pc, r.cycle) in timeouts else r.tb - tb
                toks.append(0x40 + bucket(m))
        tb = r.tb
    kinds = tuple(e.kind for e in et.events if e.kind != "MARK" and e.cycle >= arm)
    return Observation(tuple(toks), kinds, "ABORT" in kinds)


class Target:
    """A device on the model, with the observation cached per state path."""

    def __init__(self, dev: TableDevice, ppm: int = 20000):
        self.dev = dev
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.image = compile_desc(dev.build(ppm), check_timing=not dev.delay)
        self.cache: dict[tuple[int, ...], Observation] = {}
        self.runs = 0

    def simulate(self, msg: bytes, clocks: tuple = ((1, 1), (1, 1)), phases: tuple = (0, 0)) -> Observation:
        self.runs += 1
        specs = [EngineSpec(self.image, Role.P0, clock=clocks[0], phase=phases[0]),
                 EngineSpec(self.image, Role.P1, clock=clocks[1], phase=phases[1])]
        s = System(specs)
        s.host_push(0, list(msg))
        tr = s.run(MAX_TICKS)
        if not tr.quiescent:
            raise RuntimeError(f"{self.dev.name}: {bytes(msg).hex()} did not settle in {MAX_TICKS} ticks")
        return tokens_of(self.image, tr)

    def observe(self, msg: bytes) -> Observation:
        key = self.dev.path(msg)
        obs = self.cache.get(key)
        if obs is None:
            obs = self.cache[key] = self.simulate(msg)
        return obs


# ---- the devices -------------------------------------------------------------------------
def _seq(key: tuple[int, ...], done_reply: int, visible: bool) -> tuple[dict, dict]:
    """Rules for 'match key in order' with restart on key[0]; stays unlocked at the end."""
    n = len(key)
    rules = {s: {key[s]: s + 1} for s in range(n)}
    for s in range(1, n):
        rules[s].setdefault(key[0], 1)
    reply = {s: s for s in range(1, n)} if visible else {}
    reply[n] = done_reply
    return rules, reply


def unlock() -> TableDevice:
    key = (0xA5, 0x3C, 0x7E)
    rules, reply = _seq(key, 0xE7, visible=True)
    return TableDevice("unlock", 4, rules, default={3: 3}, reply=reply, planted=3,
                       doc="3-byte unlock A5 3C 7E; the reply shows the state, so progress is visible")


def leak() -> TableDevice:
    key = (0xA5, 0x3C, 0x7E)
    rules, reply = _seq(key, 0xE7, visible=False)
    return TableDevice("leak", 4, rules, default={3: 3}, reply=reply, planted=3,
                       delay={1: 64, 2: 192, 3: 448},
                       doc="the same unlock, but the reply is 0 until unlocked: only RDY latency leaks progress")


def hang() -> TableDevice:
    # 0x5A enters a debug state (reply 1); 0xC3 there makes it stop answering
    rules = {0: {0x5A: 1}, 1: {0xC3: 2, 0x5A: 1}}
    return TableDevice("hang", 4, rules, reply={1: 1}, halt=frozenset({2}), planted=2,
                       doc="5A then C3 hangs the device (no RDY: a timeout abort on the fuzzer)")


def magic() -> TableDevice:
    rules = {0: {0x4B: 1}, 1: {0x1D: 2}}
    return TableDevice("magic", 4, rules, default={2: 2}, reply={2: 0x99}, planted=2,
                       doc="16-bit magic 4B 1D with no partial feedback: the control")


DEVICES = {f.__name__: f for f in (unlock, leak, hang, magic)}


__all__ = ["TableDevice", "Target", "Observation", "tokens_of", "DEVICES", "unlock", "leak", "hang", "magic"]
