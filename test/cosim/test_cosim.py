"""Co-sim tests: each runs one image on the model and the RTL (README.md §4).

Each test checks, on the model and before it simulates, that its scenario does what the test
names (an event happens, a value changes, a flag changes what is compared), so no case can pass
without testing it."""
from dataclasses import replace

import cocotb

from harness import CosimMismatch, cosim

from emu.compiler import compile_desc
from emu.cosim import Kind, pulse, replay
from emu.desc import Description
from emu.isa import Instr, Op, Role, encode
from emu.model.system import EngineSpec, Glitch
from emu.protocols import slack

ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr", "eq", "ne", "lt")
P = 20        # bit cycles: UART at 2% needs P >= 19 (isa §6)
DATA = 0xA4   # reads 0x25 reversed, so the bit order shows on the wire (0xA5 and 0x5A read the same)
# Programs avoid ROLE_IF and RAW JPIN: devOps's tr_br doesn't pulse for them yet (test_role_branches).


def model(img, role, peer_clock=(1, 1), peer_phase=0, peers=True, glitches=()):
    """The RTL plays `role`; the model also runs every other role (unless `peers` is False)."""
    specs = [EngineSpec(img, role)]
    if peers:
        specs += [EngineSpec(img, r, clock=peer_clock, phase=peer_phase) for r in Role if r != role]
    return replay(specs, 0, glitches=glitches)


def kinds(rep):
    return {ev.kind for ev in rep.trace.engines[0].events}


@cocotb.test()
async def test_untimed(dut):
    """LI, every ALU op (each result folded into r2 as r2 := 3·r2 ^ result, so all of them reach
    the register readback), a BR NZ loop of 3, MARK, RAW DELAY and GET, NOP, HALT on P0 alone."""
    d = Description("untimed", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.li("r5", 0xBEEF)
    d.li("r0", 3)
    d.label("loop")
    for op in ALU_OPS:
        d.alu(op, "r1", "r0", 5)
        d.alu("shl", "r3", "r2", 1)
        d.alu("add", "r2", "r2", "r3")
        d.alu("xor", "r2", "r2", "r1")
    d.alu("sub", "r0", "r0", 1)
    d.br("loop", cond="nz", reg="r0")
    d.mark(5)
    d.raw_delay(5)
    d.raw_get("r4", ln)
    d.nop()
    d.halt()
    await cosim(dut, model(compile_desc(d), Role.P0, peers=False), Role.P0)


@cocotb.test()
async def test_input_at_now_1(dut):
    """uio_in during now = 1 is stimulus[1] (README §1). A one-cycle low at tick 1 is a fall for
    the COND at pc 0; a driver one cycle late would miss it, and the RTL would never halt."""
    d = Description("now1", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.mark(3)
    d.halt()
    rep = model(compile_desc(d), Role.P1, peers=False, glitches=[Glitch(0, 1, 1, 0)])
    assert rep.stimulus[:3] == [0xFF, 0xFE, 0xFF] and rep.trace.engines[0].halt_cycle is not None
    await cosim(dut, rep, Role.P1)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1])
async def test_idle_0_start(dut, role):
    """isa §2: the owner's idle drive is made in s0 and lands at now = 1, so the owner and the
    follower both read the pull-up's 1 at now = 2 and the idle 0 from now = 3."""
    d = Description("idle0", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=0)
    d.raw_get("r0", ln)  # now 2
    d.raw_get("r1", ln)  # now 4
    d.halt()
    rep = model(compile_desc(d), role)
    assert rep.trace.engines[0].regs[:2] == [1, 0]
    await cosim(dut, rep, role)


# ---------------------------------------------------------------- timed steps
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
    await cosim(dut, model(window(), role), role)


@cocotb.test()
async def test_cond_timeout_aborts(dut):
    """No owner: a stuck-low line gives the fall, then the windowed rise never comes, so the
    COND times out and aborts into abort_pc (isa §4.1)."""
    rep = model(window(), Role.P1, peers=False, glitches=[Glitch(0, 30, 400, 0)])
    assert {"TIMEOUT", "ABORT"} <= kinds(rep)
    await cosim(dut, rep, Role.P1)


@cocotb.test()
async def test_cond_early(dut):
    """A glitch raises the line 13 cycles before the window opens: EARLY (isa §4.2)."""
    rep = model(window(), Role.P1, glitches=[Glitch(0, 25, 2, 1)])
    assert "EARLY" in kinds(rep)
    await cosim(dut, rep, Role.P1)


def frame(msb=False, check=True, resync=False):
    """A UART frame with register src/dst: LI r0 · COND fall · HOLD 0 · XFER 8 · HOLD 1 · MARK · HALT."""
    d = Description("frame", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    s = slack(P, 20000)  # the window the compiler's T2 needs at 2% (as emu.protocols.uart)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r0", DATA)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(8, ln, owner="a", prof=bit, src="r0", dst="r1", msb_first=msb, check=check, resync=resync)
    d.hold(ln, 1, owner="a", prof=bit)
    d.mark(9)
    d.halt()
    return compile_desc(d)


FLAGS = ("msb", "check", "resync")


def follower(**flags):
    """The receiver against a 2% slow sender, with a 3-cycle low in data bit 2: the scenario found
    where each of MSB, CHK and RESYNC changes what the RTL sees or must produce."""
    return model(frame(**flags), Role.P1, peer_clock=(49, 50), glitches=[Glitch(0, 72, 3, 0)])


@cocotb.test()
@cocotb.parametrize(msb=[False, True], check=[False, True], resync=[False, True])
async def test_xfer_follower(dut, msb, check, resync):
    """Unclocked XFER follower with each combination of MSB, CHK and RESYNC (isa §4.3). The
    owner's bit order shows only on its pins, which co-sim doesn't compare yet (README §4)."""
    flags = dict(msb=msb, check=check, resync=resync)
    rep = follower(**flags)
    for f in FLAGS:
        flip = follower(**{**flags, f: not flags[f]})
        assert (flip.expected, flip.stimulus) != (rep.expected, rep.stimulus), f"{f} changes nothing here"
    await cosim(dut, rep, Role.P1)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1, Role.MON], peer_clock=[(1, 1), (49, 50)], peer_phase=[0, 3])
async def test_uart_frame(dut, role, peer_clock, peer_phase):
    """A UART frame as tx (P0), rx (P1) and mon, against peers up to 2% slow and out of phase."""
    await cosim(dut, model(frame(), role, peer_clock, peer_phase), role)


GLITCHES = {  # on the receiver, perfect clocks; tick 72 is the middle of data bit 2 on the wire
    "edge": (Glitch(0, 71, 4, 0), lambda rep: "EDGE" in kinds(rep) and rep.trace.engines[0].regs[1] == DATA),
    "data": (Glitch(0, 74, 4, 0), lambda rep: rep.trace.engines[0].regs[1] == 0xA0),
    "start": (Glitch(0, 14, 3, 1), lambda rep: "LEVEL" in kinds(rep)),
}


@cocotb.test()
@cocotb.parametrize(case=list(GLITCHES))
async def test_glitch_on_the_receiver(dut, case):
    """An out-of-window edge (EDGE, data kept), a flipped sample (data 0xA0) and a glitch in the
    start bit (LEVEL): isa §4.2-4.3."""
    glitch, effect = GLITCHES[case]
    rep = model(frame(), Role.P1, glitches=[glitch])
    assert effect(rep), f"the {case} glitch doesn't do what this case names"
    await cosim(dut, rep, Role.P1)


# ---------------------------------------------------------------- known gaps (README §3)
async def expect_gap(dut, rep, role, gap):
    """Fail on exactly the known gap. Any other failure raises RuntimeError, which `expect_fail`
    doesn't absorb, so a half fix or an unrelated mismatch still shows. When the gap is fixed the
    test passes, cocotb reports that as a failure, and the mark comes off."""
    try:
        await cosim(dut, rep, role)
    except CosimMismatch as e:
        if e.mismatch is None or not gap(e.mismatch):
            raise RuntimeError(f"failed for another reason than its gap:\n{e}") from e
        raise


@cocotb.test(expect_fail=True)
async def test_illegal(dut):
    """isa §3: a reserved ALU sub-code halts as ILLEGAL. Gap: devOps's #13 runs it as a NOP."""
    img = replace(frame(), words=[encode(Instr(Op.ALU, sub=10)), encode(Instr(Op.HALT))], debug={})
    await expect_gap(dut, model(img, Role.P0, peers=False), Role.P0,
                     lambda m: m.expected == pulse(2, Kind.EVT, 0, 10) and m.observed == pulse(4, Kind.ISSUE, 1))


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
    await expect_gap(dut, model(compile_desc(d), Role.P0, peers=False), Role.P0,
                     lambda m: m.expected is not None and m.expected.kind == Kind.BR and m.expected.pc == 0)
