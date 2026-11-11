"""Samples an engine's RTL trace port (architecture §5) every clk into co-sim pulses."""
import cocotb
from cocotb.triggers import RisingEdge

from emu.cosim import Kind, Pulse, pulse


class Monitor:
    """A read at a rising edge returns the values of the cycle that edge ends, so every pulse
    gets that cycle's `tr_now` and `tr_pc`."""

    def __init__(self, dut, eng: int = 0):
        self.dut = dut
        self.eng = getattr(dut.user_project, f"u_eng{eng}")
        self.pulses: list[Pulse] = []
        self.now = 0                      # the last tr_now seen
        self.clk = 0                      # rising edges seen
        self.halted_clk: int | None = None
        cocotb.start_soon(self._run())

    async def _run(self):
        e, add = self.eng, self.pulses.append
        while True:
            await RisingEdge(self.dut.clk)
            self.clk += 1
            now, pc = int(e.tr_now.value), int(e.tr_pc.value)
            self.now = now
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
            if self.halted_clk is None and int(e.tr_halted.value):
                self.halted_clk = self.clk
                add(pulse(now, Kind.HALT, pc))
