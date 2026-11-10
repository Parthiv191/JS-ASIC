"""Co-sim tests: each runs one image on the model and the RTL (README.md §4)."""
import cocotb

from harness import cosim

from emu.compiler import compile_desc
from emu.cosim import replay
from emu.desc import Description
from emu.isa import Role
from emu.model.system import EngineSpec

ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr", "eq", "ne", "lt")


@cocotb.test()
async def test_untimed(dut):
    """LI, every ALU op, a BR NZ loop of 3, MARK, RAW DELAY and GET, NOP, HALT on P0 alone."""
    d = Description("untimed", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.li("r0", 3)
    d.label("loop")
    for op in ALU_OPS:
        d.alu(op, "r1", "r0", 5)
    d.alu("sub", "r0", "r0", 1)
    d.br("loop", cond="nz", reg="r0")
    d.mark(5)
    d.raw_delay(5)
    d.raw_get("r2", ln)
    d.nop()
    d.halt()
    img = compile_desc(d)
    await cosim(dut, replay([EngineSpec(img, Role.P0)], 0), Role.P0)
