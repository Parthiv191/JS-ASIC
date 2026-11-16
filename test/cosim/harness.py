"""Runs one model replay against the chip and fails on the first difference (README.md).

RTL: the trace-port pulses, the pins every cycle, then r0-r7 and the FIFOs. Gate level
(COSIM_GATES=1, set by `make GATES=yes`): synthesis removes the trace port, so the pins, r0-r7 and
the FIFOs."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                  # devOps's chip.py (imported, not edited)
sys.path.insert(0, str(HERE.parents[1] / "toolchain"))

from cocotb.triggers import RisingEdge  # noqa: E402

from chip import E_FIFO_COUNTS, E_ROLE, E_RXDATA, E_TXDATA, ENG0, RUN, Host, eng_read, get_reg, reset  # noqa: E402
from emu.cosim import ACT_FROM, Mismatch, PinMismatch, Replay, act, compare, compare_pins, pins  # noqa: E402
from emu.isa import NUM_REGS, Role  # noqa: E402

from monitor import Monitor  # noqa: E402

GATES = os.environ.get("COSIM_GATES") == "1"

# Rising edges from the Monitor's start (just before the RUN write) to the edge that ends s0 (the
# cycle with now = 0). It depends on chip.py's SPI timing and the RTL's RUN path; every RTL run
# checks it against the engine's `run` input and tr_now, every run against ACT (README §1).
RUN_S0 = 263


class CosimMismatch(AssertionError):
    """A co-sim failure. `mismatch` is the first differing pulse, `pin` the first differing pin
    cycle; both are None when the alignment or the registers read back after the run differ."""

    def __init__(self, text: str, mismatch: Mismatch | None = None, pin: PinMismatch | None = None):
        super().__init__(text)
        self.mismatch, self.pin = mismatch, pin


def _source(rep: Replay, pc_set) -> list[str]:
    return [f"  pc {pc}: {rep.image.debug.get(pc, '?')}" for pc in sorted(pc_set)]


def report(m: Mismatch, rep: Replay) -> str:
    pcs = {p.pc for p in (m.expected, m.observed) if p is not None}
    return "\n".join([str(m), *_source(rep, pcs), "  waveform: test/cosim/tb.fst"])


def pin_report(m: PinMismatch, rep: Replay) -> str:
    """A drive on the wire in tick t was made in cycle t - 1: name the instruction running then."""
    starts = [pc for pc, now in rep.trace.engines[rep.engine].starts if now <= m.tick - 1]
    made = [f"  made in cycle {m.tick - 1}, by the instruction at", *_source(rep, starts[-1:])] if starts else []
    return "\n".join([str(m), *made, "  waveform: test/cosim/tb.fst"])


def _check_alignment(mon: Monitor) -> None:
    errs = []
    if not GATES:
        if mon.run_edge != RUN_S0:
            errs.append(f"s0 ended at edge {mon.run_edge}, not RUN_S0 = {RUN_S0}")
        elif mon.bad_now is not None:
            errs.append("tr_now is not the tick in a running cycle: (tick, tr_now) = %s" % (mon.bad_now,))
    k = RUN_S0 + ACT_FROM  # the edge that ends the cycle with now = ACT_FROM
    first = mon.act.find(1) + 1
    if first != k:
        errs.append(f"ACT first read 1 at edge {first or None}, not {k} (now = {ACT_FROM})")
    if errs:
        raise CosimMismatch("co-sim alignment (README §1):\n  " + "\n  ".join(errs))


def _check_build(dut) -> None:
    """COSIM_GATES must match the build: a stale COSIM_GATES=1 on RTL would skip the trace."""
    rtl = hasattr(dut.user_project, "u_eng0")
    if rtl == GATES:
        raise RuntimeError(f"COSIM_GATES={int(GATES)}, but the design {'has a' if rtl else 'has no'} "
                           "trace port: run make, or make GATES=yes for the netlist")


async def cosim(dut, rep: Replay, role: Role, margin: int = 64, sample2: bool = False) -> None:
    """`sample2` also compares a duplex MON's data2 samples (tr_sample2, architecture §5)."""
    _check_build(dut)
    await reset(dut)
    dut.uio_in.value = rep.stimulus[0]
    host = Host(dut)
    for addr, data in rep.image.host_writes():
        await host.write(addr, data)
    await host.write(ENG0 + E_ROLE, [int(role)])
    if rep.tx:  # the model pushes them at tick 0; RUN keeps the TX FIFO (architecture §4)
        await host.write(ENG0 + E_TXDATA, [b for w in rep.tx for b in (w & 0xFF, w >> 8)], pair=True)
    mon = Monitor(dut, rep.stimulus, RUN_S0, GATES, sample2=sample2)
    await host.write(RUN, [1])
    while mon.tick <= rep.trace.ticks + margin:
        await RisingEdge(dut.clk)
    _check_alignment(mon)
    if not GATES:
        m = compare(rep.expected, mon.pulses)
        if m is not None:
            raise CosimMismatch(report(m, rep), m)
    first = 1 - RUN_S0  # edge 1's tick
    want = pins(rep.trace, rep.engine, range(first, first + len(mon.pins)))
    pm = compare_pins(want, mon.pins, first)
    if pm is not None:
        raise CosimMismatch(pin_report(pm, rep), pin=pm)
    # ACT every cycle: at gate level the only view of when the engine halts
    want_act = act(rep.trace, rep.engine, range(first, first + len(mon.act)))
    bad = next((k for k, (w, g) in enumerate(zip(want_act, mon.act)) if w != g), None)
    if bad is not None:
        raise CosimMismatch(f"ACT differs at tick {first + bad}: expected {want_act[bad]}, observed "
                            f"{mon.act[bad]} (model HALT at {rep.trace.engines[rep.engine].halt_cycle})")
    # the trace port has no register writes: read r0-r7 back over the host port
    want = rep.trace.engines[rep.engine].regs
    got = [await get_reg(host, i) for i in range(NUM_REGS)]
    if got != want:
        diff = [f"  r{i}: expected {w:#06x}, observed {g:#06x}" for i, (w, g) in enumerate(zip(want, got)) if w != g]
        raise CosimMismatch("registers after the run differ:\n" + "\n".join(diff))
    # the FIFOs: the words left in TX, and RX read out over RXDATA (architecture §4)
    et = rep.trace.engines[rep.engine]
    counts = await eng_read(host, E_FIFO_COUNTS)
    tx_left, rx_n = counts & 0x7, counts >> 4
    if (tx_left, rx_n) != (et.tx_left, len(et.rx)):
        raise CosimMismatch(f"FIFO counts after the run: TX {tx_left}, RX {rx_n}; the model has TX "
                            f"{et.tx_left}, RX {len(et.rx)}")
    if rx_n:
        b = await host.read(ENG0 + E_RXDATA, 2 * rx_n, pair=True)
        got = [b[2 * i] | b[2 * i + 1] << 8 for i in range(rx_n)]
        if got != et.rx:
            raise CosimMismatch(f"RX FIFO after the run: expected {[hex(w) for w in et.rx]}, observed "
                                f"{[hex(w) for w in got]}")
    what = "pins, ACT" if GATES else f"{len(rep.expected)} pulses, pins, ACT"
    dut._log.info(f"co-sim: {what}, r0-r7 and the FIFOs match ({len(et.rx)} RX words), {mon.clk} clk, "
                  f"model end at tick {rep.trace.ticks}")
