"""Runs one model replay against the RTL and fails on the first differing pulse (README.md)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                  # devOps's chip.py (imported, not edited)
sys.path.insert(0, str(HERE.parents[1] / "toolchain"))

import cocotb  # noqa: E402
from cocotb.triggers import RisingEdge  # noqa: E402

from chip import E_ROLE, ENG0, RUN, Host, get_reg, reset  # noqa: E402
from emu.cosim import Mismatch, Replay, compare  # noqa: E402
from emu.isa import NUM_REGS, Role  # noqa: E402

from monitor import Monitor  # noqa: E402

LOAD_CAP = 20_000  # clk allowed beyond the model's end before the run is called hung


class CosimMismatch(AssertionError):
    """A co-sim failure. `mismatch` is the first differing pulse, or None when the pulses agree
    and the registers read back after the run don't."""

    def __init__(self, text: str, mismatch: Mismatch | None = None):
        super().__init__(text)
        self.mismatch = mismatch


def report(m: Mismatch, rep: Replay) -> str:
    pcs = {p.pc for p in (m.expected, m.observed) if p is not None}
    src = [f"  pc {pc}: {rep.image.debug.get(pc, '?')}" for pc in sorted(pcs)]
    return "\n".join([str(m), *src, "  waveform: test/cosim/tb.fst"])


async def _drive(dut, eng, stim: list[int]):
    """uio_in during the cycle with now = w is stim[w]. `running` is 0 in the start cycle s0, so
    this keys on the engine's `run` input instead."""
    last = len(stim) - 1
    while True:
        await RisingEdge(dut.clk)
        if int(eng.run.value):
            dut.uio_in.value = stim[min(int(eng.tr_now.value) + 1, last)]


async def cosim(dut, rep: Replay, role: Role, margin: int = 64) -> None:
    await reset(dut)
    eng = dut.user_project.u_eng0
    dut.uio_in.value = rep.stimulus[0]
    host = Host(dut)
    for addr, data in rep.image.host_writes():
        await host.write(addr, data)
    await host.write(ENG0 + E_ROLE, [int(role)])
    mon = Monitor(dut)
    cocotb.start_soon(_drive(dut, eng, rep.stimulus))
    await host.write(RUN, [1])
    end, cap = rep.trace.ticks + margin, mon.clk + rep.trace.ticks + margin + LOAD_CAP
    while mon.now <= end and mon.clk <= cap:
        if mon.halted_clk is not None and mon.clk - mon.halted_clk > margin:
            break
        await RisingEdge(dut.clk)
    m = compare(rep.expected, mon.pulses)
    if m is not None:
        raise CosimMismatch(report(m, rep), m)
    # the trace port has no register writes: read r0-r7 back over the host port
    want = rep.trace.engines[rep.engine].regs
    got = [await get_reg(host, i) for i in range(NUM_REGS)]
    if got != want:
        diff = [f"  r{i}: expected {w:#06x}, observed {g:#06x}" for i, (w, g) in enumerate(zip(want, got)) if w != g]
        raise CosimMismatch("registers after the run differ:\n" + "\n".join(diff))
    dut._log.info(f"co-sim: {len(rep.expected)} pulses and r0-r7 match, model end at tick {rep.trace.ticks}")
