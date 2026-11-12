"""Samples the chip every clk from just before the RUN write, and drives uio_in (README.md §1)."""
import cocotb
from cocotb.triggers import RisingEdge

from emu.cosim import MASK24, Kind, Pulse, pulse

ACT = 1 << 7  # uo_out[7] (architecture §8)


class Monitor:
    """Edge k (counted from 1, just before the RUN write) ends the cycle with model tick
    k - run_s0. A read at a rising edge returns the values of the cycle that edge ends.

    Every edge records the pins and ACT. On RTL it also records the trace-port pulses, the first
    edge with the engine's `run` input high (it ends s0), and the first running cycle whose
    `tr_now` isn't its tick. The gate-level netlist has no trace port: `gates` skips all three."""

    def __init__(self, dut, stim: list[int], run_s0: int, gates: bool, eng: int = 0,
                 sample2: bool = False):
        self.dut, self.stim, self.run_s0, self.gates = dut, stim, run_s0, gates
        self.eng = None if gates else getattr(dut.user_project, f"u_eng{eng}")
        # a duplex MON's data2 sample (architecture §5): opt-in while tests in test/ still drop
        # those pulses from the expected trace (README §2)
        self.sample2 = sample2 and self.eng is not None
        if self.sample2 and not hasattr(self.eng, "tr_sample2"):
            raise RuntimeError("sample2: this engine has no tr_sample2 (architecture §5)")
        self.clk = 0                                  # rising edges seen
        self.pins: list[tuple[int, int]] = []         # (uio_oe, uio_out) per edge
        self.act = bytearray()                        # ACT per edge
        self.pulses: list[Pulse] = []
        self.run_edge: int | None = None
        self.bad_now: tuple[int, int] | None = None   # (tick, tr_now)
        self.halted = False
        cocotb.start_soon(self._run())

    @property
    def tick(self) -> int:
        """The model tick of the last cycle seen."""
        return self.clk - self.run_s0

    async def _run(self):
        d, last, seen = self.dut, len(self.stim) - 1, {}
        while True:
            await RisingEdge(d.clk)
            self.clk += 1
            tick = self.tick
            oe, out = int(d.uio_oe.value), int(d.uio_out.value)
            self.pins.append(seen.setdefault((oe, out), (oe, out)))
            self.act.append(int(d.uo_out.value) & ACT and 1)
            if not self.gates:
                self._trace(tick)
            if tick >= 0:  # on the wire from the next cycle
                d.uio_in.value = self.stim[min(tick + 1, last)]

    def _trace(self, tick: int):
        e, add = self.eng, self.pulses.append
        now, pc, halted = int(e.tr_now.value), int(e.tr_pc.value), int(e.tr_halted.value)
        if int(e.run.value):
            if self.run_edge is None:
                self.run_edge = self.clk
            # time freezes at a halt (architecture §5)
            if not halted and self.bad_now is None and now != tick & MASK24:
                self.bad_now = (tick, now)
        if int(e.tr_issue.value):
            add(pulse(now, Kind.ISSUE, pc))
        if int(e.tr_step.value):
            add(pulse(now, Kind.STEP, pc, int(e.tr_op.value), int(e.tr_do.value),
                      int(e.tr_value.value), int(e.tr_tb.value)))
        evt = int(e.tr_evt.value)
        for code in range(11):
            if evt >> code & 1:
                add(pulse(now, Kind.EVT, pc, code))
        if int(e.tr_mark.value):
            add(pulse(now, Kind.MARK, pc, int(e.tr_value.value)))
        if int(e.tr_br.value):
            add(pulse(now, Kind.BR, pc, int(e.tr_taken.value)))
        if int(e.tr_sample.value):
            add(pulse(now, Kind.SAMPLE, pc, int(e.tr_line.value)))
        if self.sample2 and int(e.tr_sample2.value):
            add(pulse(now, Kind.SAMPLE, pc, int(e.tr_line2.value)))
        if halted and not self.halted:
            self.halted = True
            add(pulse(now, Kind.HALT, pc))
