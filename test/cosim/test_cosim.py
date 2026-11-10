"""Co-sim tests: each runs one image on the model and the RTL (README.md §4)."""
from dataclasses import replace

import cocotb

from harness import cosim

from emu.compiler import compile_desc
from emu.cosim import replay
from emu.desc import Description
from emu.isa import Instr, Op, Role, encode
from emu.model.system import EngineSpec, Glitch
from emu.protocols import slack

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


# ---------------------------------------------------------------- timed steps
# Programs avoid ROLE_IF and RAW JPIN: devOps's tr_br doesn't pulse for them yet (test_role_branches).
P = 20  # bit cycles: UART at 2% needs P >= 19 (isa §6)


async def run(dut, img, role, peer_clock=(1, 1), peer_phase=0, peers=True, glitches=()):
    """The RTL plays `role`; the model also runs every other role (unless `peers` is False)."""
    specs = [EngineSpec(img, role)]
    if peers:
        specs += [EngineSpec(img, r, clock=peer_clock, phase=peer_phase) for r in Role if r != role]
    rep = replay(specs, 0, glitches=glitches)
    await cosim(dut, rep, role)
    return rep


def window():
    """COND fall · HOLD 0 · COND rise in a window, with ABORT · MARK · HALT (abort_pc)."""
    d = Description("window", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=16, min=14, max=18)
    win = d.profile("W", nom=20, min=16, max=24)
    d.abort_to("end")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.cond("rise", ln, owner="a", prof=win, abort=True)
    d.mark(7)
    d.label("end")
    d.halt()
    return compile_desc(d)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1])
async def test_hold_cond(dut, role):
    """HOLD and COND, done by P0 and followed by P1."""
    await run(dut, window(), role)


@cocotb.test()
async def test_cond_timeout_aborts(dut):
    """No owner: a stuck-low line gives the fall, then the windowed rise never comes, so the
    COND times out and aborts into abort_pc (isa §4.1)."""
    rep = await run(dut, window(), Role.P1, peers=False, glitches=[Glitch(0, 30, 400, 0)])
    assert {"TIMEOUT", "ABORT"} <= {ev.kind for ev in rep.trace.engines[0].events}


@cocotb.test()
async def test_cond_early(dut):
    """A glitch raises the line 13 cycles before the window opens: EARLY (isa §4.2)."""
    rep = await run(dut, window(), Role.P1, glitches=[Glitch(0, 25, 2, 1)])
    assert "EARLY" in {ev.kind for ev in rep.trace.engines[0].events}


def frame(msb=False, check=True, resync=False, mark=True):
    """A UART frame with register src/dst: LI r0 · COND fall · HOLD 0 · XFER 8 · HOLD 1 · HALT."""
    d = Description("frame", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    s = slack(P, 20000)  # the window the compiler's T2 needs at 2% (as emu.protocols.uart)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r0", 0xA5)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(8, ln, owner="a", prof=bit, src="r0", dst="r1", msb_first=msb, check=check, resync=resync)
    d.hold(ln, 1, owner="a", prof=bit)
    if mark:
        d.mark(9)
    d.halt()
    return compile_desc(d)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1], msb=[False, True], check=[False, True], resync=[False, True])
async def test_xfer(dut, role, msb, check, resync):
    """Unclocked XFER, owner and follower, with each of MSB, CHK and RESYNC (isa §4.3)."""
    await run(dut, frame(msb, check, resync, mark=False), role)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1, Role.MON], peer_clock=[(1, 1), (49, 50)], peer_phase=[0, 3])
async def test_uart_frame(dut, role, peer_clock, peer_phase):
    """A UART frame as tx (P0), rx (P1) and mon, against peers up to 2% slow and out of phase."""
    await run(dut, frame(), role, peer_clock, peer_phase)


@cocotb.test()
async def test_glitch_in_a_data_bit(dut):
    """A 4-cycle low glitch across data bit 2's sample point, on the receiver (isa §4.3)."""
    t = 2 + P + 2 * P + P // 2  # the fall at 2, the start bit, two bits, then mid-bit
    rep = await run(dut, frame(), Role.P1, glitches=[Glitch(0, t - 1, 4, 0)])
    assert rep.trace.engines[0].records[2].value != 0xA5 or rep.trace.engines[0].events


# ---------------------------------------------------------------- known gaps (README §3)
@cocotb.test(expect_fail=True)
async def test_illegal(dut):
    """isa §3: a reserved ALU sub-code halts as ILLEGAL. Gap: devOps's #13 runs it as a NOP."""
    img = replace(frame(), words=[encode(Instr(Op.ALU, sub=10)), encode(Instr(Op.HALT))])
    await run(dut, img, Role.P0, peers=False)


@cocotb.test(expect_fail=True)
async def test_role_branches(dut):
    """architecture §5: tr_br pulses for ROLE_IF and RAW JPIN. Gap: devOps's #13 pulses it only
    for BR."""
    d = Description("roles", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.raw_jpin(ln, 1, "x")
        d.nop()
    d.label("x")
    d.halt()
    await run(dut, compile_desc(d), Role.P0, peers=False)
