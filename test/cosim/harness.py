"""Runs one model replay against the RTL and fails on the first differing pulse (README.md)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                  # devOps's chip.py (imported, not edited)
sys.path.insert(0, str(HERE.parents[1] / "toolchain"))

import cocotb  # noqa: E402
from cocotb.triggers import RisingEdge  # noqa: E402

from chip import E_ROLE, ENG0, RUN, Host, reset  # noqa: E402
from emu.cosim import Mismatch, Replay, compare  # noqa: E402
from emu.isa import Role  # noqa: E402

from monitor import Monitor  # noqa: E402

LOAD_CAP = 20_000  # clk allowed beyond the model's end before the run is called hung


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
        raise AssertionError(report(m, rep))
    dut._log.info(f"co-sim: {len(rep.expected)} pulses match, model end at tick {rep.trace.ticks}")
