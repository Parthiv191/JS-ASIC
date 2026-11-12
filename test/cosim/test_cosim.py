"""Co-sim tests: each runs one image on the model and the RTL (README.md §4).

Each test checks, on the model and before it simulates, that its scenario does what the test
names (an event happens, a value changes, a flag changes what is compared), so no case can pass
without testing it."""
import os
from dataclasses import replace

import cocotb

from harness import GATES, CosimMismatch, cosim

from emu.compiler import compile_desc
from emu.cosim import Kind, pulse, replay
from emu.desc import Description
from emu.isa import INF, Instr, Op, Role, encode
from emu.model.system import EngineSpec, Glitch
from emu.protocols import get, slack

ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr", "eq", "ne", "lt")
P = 20        # bit cycles: UART at 2% needs P >= 19 (isa §6)
DATA = 0xA4   # reads 0x25 reversed, so the bit order shows on the wire (0xA5 and 0x5A read the same)


def model(img, role, peer_clock=(1, 1), peer_phase=0, peers=True, glitches=(), host=None):
    """The RTL plays `role`; the model also runs every other role (unless `peers` is False).
    `host` maps a party role to its host's TX words (the RTL's are written before RUN)."""
    specs = [EngineSpec(img, role)]
    if peers:
        specs += [EngineSpec(img, r, clock=peer_clock, phase=peer_phase) for r in Role if r != role]
    tx = {i: (host or {}).get(s.role, []) for i, s in enumerate(specs) if s.role != Role.MON}
    return replay(specs, 0, glitches=glitches, tx=tx)


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


def meas(timeout_only=False):
    """HOLD 1 · COND fall (unbounded), MEAS r2 · HOLD 0 · COND rise (bounded), MEAS r3 · HALT:
    tb moves before each COND. `timeout_only`: COND fall (bounded), MEAS r2 · HALT."""
    d = Description("meas", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=16, min=14, max=18)
    win = d.profile("W", nom=20, min=0, max=40)
    if timeout_only:
        d.cond("fall", ln, owner="a", prof=win, meas="r2")
    else:
        d.hold(ln, 1, owner="a", prof=bit)
        d.cond("fall", ln, owner="a", prof=d.profile("U", nom=20, min=0, max=INF), meas="r2")
        d.hold(ln, 0, owner="a", prof=bit)
        d.cond("rise", ln, owner="a", prof=win, meas="r3")
    d.halt()
    return compile_desc(d)


def cond_values(rep):
    return [r.value for r in rep.trace.engines[0].records if r.op == "COND"]


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1, Role.MON], peer_clock=[(1, 1), (49, 50)])
async def test_cond_meas(dut, role, peer_clock):
    """A COND's trace value is what MEAS writes (architecture §5), and the registers match: the
    model had none for every COND, and no co-sim test had MEAS, so that gap passed (#50)."""
    rep = model(meas(), role, peer_clock=peer_clock)
    e = rep.trace.engines[0]
    conds = [r for r in e.records if r.op == "COND"]
    assert cond_values(rep) == [e.regs[2], e.regs[3]] and all(r.value != r.tb for r in conds)
    await cosim(dut, rep, role)


@cocotb.test()
async def test_cond_meas_timeout(dut):
    """No owner: the COND times out (no ABORT), so its value and register are 0xFFFF."""
    rep = model(meas(timeout_only=True), Role.P1, peers=False)
    assert "TIMEOUT" in kinds(rep) and cond_values(rep) == [0xFFFF] == [rep.trace.engines[0].regs[2]]
    await cosim(dut, rep, Role.P1)


def frame(msb=False, check=True, resync=False, kind="pp"):
    """A UART frame with register src/dst: LI r0 · COND fall · HOLD 0 · XFER 8 · HOLD 1 · MARK · HALT."""
    d = Description("frame", parties=("a", "b"))
    ln = d.line("l", kind=kind, owner="a", idle=1)
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
    """Unclocked XFER follower with each combination of MSB, CHK and RESYNC (isa §4.3). As the
    owner, the RTL's bit order shows only on its pins: `uart frame` in role tx checks it, LSB
    first only."""
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


UART_WORDS = [0xA4, 0x3C, 0x00, 0xFF]  # 4: the TX FIFO's depth, all written before RUN


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1, Role.MON], peer_clock=[(1, 1), (49, 50)])
async def test_uart_protocol(dut, role, peer_clock):
    """The library UART (emu/protocols/uart.py) from the FIFOs, as tx, rx and mon, against peers up
    to 2% slow: tx takes its host's words by POP (the RTL from TXDATA), rx and mon push what they
    receive into RX (the RTL's read back over RXDATA). Plan step 4's M1 target."""
    rep = model(compile_desc(get("uart").build(bit_cycles=P)), role, peer_clock, host={Role.P0: UART_WORDS})
    et = rep.trace.engines[0]
    assert et.rx == ([] if role == Role.P0 else UART_WORDS) and et.tx_left == 0 and not et.events
    assert rep.tx == (UART_WORDS if role == Role.P0 else [])
    await cosim(dut, rep, role)


SPI_CTL = [0, 0xA4, 0x3C]  # the frame token, then the frame's two bytes
SPI_PER = [0x5A, 0xC3]


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1, Role.MON], mode=[(0, 0), (0, 1), (1, 0), (1, 1)])
async def test_spi_protocol(dut, role, mode):
    """The library SPI (emu/protocols/spi.py), one 2-byte frame in each mode, as ctl, per and mon
    against peers 2% slow: the SELECT bus, the gated MISO and duplex XFERs from and to the FIFOs.
    Every role pushes (MISO << 8) | MOSI. MON samples both data lines, the second on tr_sample2."""
    img = compile_desc(get("spi").build(cpol=mode[0], cpha=mode[1], words=2))
    rep = model(img, role, (49, 50), host={Role.P0: SPI_CTL, Role.P1: SPI_PER})
    et = rep.trace.engines[0]
    assert et.rx == [0x5AA4, 0xC33C] and et.tx_left == 0 and not et.events
    names = [ln.name for ln in img.lines]
    mosi, miso = names.index("mosi"), names.index("miso")
    sampled = {p.fields[0] for p in rep.expected if p.kind == Kind.SAMPLE}
    assert sampled == {Role.P0: {miso}, Role.P1: {mosi}, Role.MON: {mosi, miso}}[role]
    await cosim(dut, rep, role, sample2=True)


I2C_HOST = {  # the controller's host words: the address byte, then the transaction's two data bytes
    "ack": [0x5A << 1, 0xA4, 0x3C],
    "nack": [0x50 << 1, 0xA4, 0x3C],   # not the target's address
}
I2C_RX = {  # what each role pushes into RX, and the events it logs
    "ack": {Role.P0: ([], set()), Role.P1: ([0xA4, 0x3C], set()), Role.MON: ([0xA4, 0x3C], set())},
    "nack": {Role.P0: ([1], set()), Role.P1: ([], set()), Role.MON: ([], {"BUS"})},
}


def i2c(role, case="ack", peer_clock=(49, 50), ldelay=12):
    img = compile_desc(get("i2c").build(data_bytes=2, ldelay=ldelay))
    return model(img, role, peer_clock, host={Role.P0: I2C_HOST[case]})


@cocotb.test()
@cocotb.parametrize(case=list(I2C_HOST), role=[Role.P0, Role.P1, Role.MON], peer_clock=[(1, 1), (49, 50)])
async def test_i2c_protocol(dut, case, role, peer_clock):
    """The library I2C write (emu/protocols/i2c.py) with two data bytes, as ctl, tgt and mon,
    against peers up to 2% slow: COND START and STOP, OD lanes and their lets-go, the lead blocks.
    ack: the target ACKs its address and the data, and it and MON push the bytes. nack: the
    controller ends the transaction and pushes 1; MON follows the data byte speculatively, and the
    STOP makes that a bus exit."""
    rep = i2c(role, case, peer_clock)
    et = rep.trace.engines[0]
    rx, events = I2C_RX[case][role]
    assert et.rx == rx and et.tx_left == 0 and {ev.kind for ev in et.events} == events
    await cosim(dut, rep, role)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1], ldelay=[1, 4])
async def test_i2c_short_ldelay(dut, role, ldelay):
    """I2C with SDA moving 1 or 4 cycles after SCL falls (isa §4.3): the let-go's release counts
    ldelay from the owner's edge, or ldelay - EDGE_LAT from a detected one, down to 0."""
    rep = i2c(role, ldelay=ldelay)
    et = rep.trace.engines[0]
    assert et.rx == I2C_RX["ack"][role][0] and not et.events
    assert et.drives != i2c(role).trace.engines[0].drives, "ldelay changes this role's drives"
    await cosim(dut, rep, role)


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


# ---------------------------------------------------------------- evt, OD readback, the clamp
@cocotb.test()
async def test_raw_evt(dut):
    """isa §4.1: TIMEOUT sets evt bit 2 and LEVEL bit 3, and RAW EVT reads evt, then clears the
    masked bits. A follower with no owner: a stuck-low line gives the fall, the bounded rise times
    out (no ABORT), and the HOLD 1 reads 0."""
    d = Description("evt", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=16, min=14, max=18)
    d.cond("fall", ln, owner="a")
    d.cond("rise", ln, owner="a", prof=bit)
    d.hold(ln, 1, owner="a", prof=bit)
    d.raw_evt("r0", 0b0100)
    d.raw_evt("r1", 0)
    d.halt()
    rep = model(compile_desc(d, check_timing=False), Role.P1, peers=False, glitches=[Glitch(0, 10, 400, 0)])
    assert {"TIMEOUT", "LEVEL"} <= kinds(rep) and rep.trace.engines[0].regs[:2] == [0b1100, 0b1000]
    await cosim(dut, rep, Role.P1)


@cocotb.test()
@cocotb.parametrize(role=[Role.P0, Role.P1])
async def test_od_readback(dut, role):
    """isa §4.3: on an OD line the owner reads its bits back where a follower samples them, so a
    glitch over data bit 2 (a released 1 of DATA) gives 0xA0 in every role, and the owner
    completes on its last readback."""
    rep = model(frame(kind="od"), role, glitches=[Glitch(0, 74, 4, 0)])
    assert [et.regs[1] for et in rep.trace.engines] == [0xA0] * 3
    await cosim(dut, rep, role)


@cocotb.test()
@cocotb.parametrize(case=["halt", "abort"])
async def test_od_release(dut, case):
    """isa §4.1: HALT and an abort release the OD lines and keep the PP drives. P1 pulls OD line
    `o` low and drives PP line `q` 0, then follows P0's fall with ABORT: P0 makes it (HALT) or no
    one does (the COND times out and aborts). The lines sit on pins 5, 2 and 6, so the line-to-pin
    map shows too. Only the pins show any of this."""
    d = Description("release", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    o = d.line("o", "od", owner="b", idle=1)
    q = d.line("q", owner="b", idle=1)
    h = d.profile("H", nom=8)
    win = d.profile("W", nom=20, min=16, max=24)
    d.abort_to("end")
    d.hold(o, 0, owner="b", prof=h)
    d.hold(q, 0, owner="b", prof=h)
    d.cond("fall", ln, owner="a", prof=win, abort=True)
    d.mark(7)
    d.label("end")
    d.halt()
    img = compile_desc(d, check_timing=False)
    img = replace(img, lines=[replace(x, pin=p) for x, p in zip(img.lines, (5, 2, 6), strict=True)])
    rep = model(img, Role.P1, peers=case == "halt")
    et = rep.trace.engines[0]
    drv, h = et.drives, et.halt_cycle
    # the release lands the cycle after the HALT starts, or after the abort and before the HALT
    end = h if case == "halt" else next(ev.cycle for ev in et.events if ev.kind == "ABORT")
    assert ("ABORT" in kinds(rep)) == (case == "abort") == (end < h)
    assert drv[end] == (None, 0, 0) and drv[end + 1] == (None, None, 0) and drv[-1] == (None, None, 0)
    await cosim(dut, rep, Role.P1)


CLAMP = 1 << 22


@cocotb.test(skip=os.environ.get("COSIM_SLOW") != "1")
async def test_time_clamp(dut):
    """isa §4.1: tb never trails now by more than 2^22. After a gap of about 4.2 M cycles the owned
    HOLD is planned 2^22 back (OVERRUN), and its step's tb shows the clamp. Slow: COSIM_SLOW=1."""
    d = Description("clamp", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=10, min=8, max=12)
    d.li("r0", 1050)  # x (RAW DELAY 4000 + SUB + BR) > 2^22
    d.label("loop")
    d.raw_delay(4000)
    d.alu("sub", "r0", "r0", 1)
    d.br("loop", cond="nz", reg="r0")
    d.hold(ln, 0, owner="a", prof=bit)
    d.halt()
    rep = replay([EngineSpec(compile_desc(d, check_timing=False), Role.P0)], 0, max_ticks=5_000_000)
    et = rep.trace.engines[0]
    (hold,) = et.records
    start = next(now for pc, now in et.starts if pc == hold.pc)
    assert start > CLAMP and hold.tb == start - CLAMP + 10
    await cosim(dut, rep, Role.P0)


@cocotb.test()
async def test_illegal(dut):
    """isa §3: a reserved ALU sub-code halts as ILLEGAL (EVT pulse, then the engine stops)."""
    img = replace(frame(), words=[encode(Instr(Op.ALU, sub=10)), encode(Instr(Op.HALT))], debug={})
    rep = model(img, Role.P0, peers=False)
    assert pulse(2, Kind.EVT, 0, 10) in rep.expected
    await cosim(dut, rep, Role.P0)


@cocotb.test()
async def test_role_branches(dut):
    """architecture §5: tr_br pulses for ROLE_IF and RAW JPIN, not only for BR."""
    d = Description("roles", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.raw_jpin(ln, 1, "x")
        d.nop()
    d.label("x")
    d.halt()
    rep = model(compile_desc(d), Role.P0, peers=False)
    assert [p.pc for p in rep.expected if p.kind == Kind.BR] == [0, 1]   # ROLE_IF, then RAW JPIN
    await cosim(dut, rep, Role.P0)


# ---------------------------------------------------------------- known gaps (README §3): none open
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
