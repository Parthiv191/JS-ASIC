"""The bus (isa §4.7): the detector (I2C, SELECT), COND START/STOP done and followed, bus exits
and gated lines (isa §2), and RAW EVT against a flag set in its own start cycle (isa §4.1).

The co-sim cases also compare the RTL's own pins, cycle by cycle, with the model's drives
(`EngineTrace.drives`): a gated line's release and an exit's releases show only on the pins."""

import os
import sys
from pathlib import Path

import cocotb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolchain"))

from emu.isa import Instr, Op, RawOp, encode

from chip import Bench, Host, get_reg, line_byte, reset, run_program

GL = os.environ.get("GATES") == "yes"
HALT = Instr(Op.HALT)
BUS_KIND, BUS_AB, BUS_CONFIRM = 0x0454, 0x0455, 0x0456

if not GL:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "cosim"))
    from harness import cosim
    from emu.compiler import compile_desc
    from emu.cosim import replay
    from emu.desc import Description
    from emu.isa import Role
    from emu.model.system import EngineSpec, Glitch


def model(img, role, glitches=()):
    specs = [EngineSpec(img, role)] + [EngineSpec(img, r) for r in Role if r != role]
    return replay(specs, 0, glitches=glitches)


def meas_values(rep):
    """architecture §5: a COND step's tr_value is its measurement, but the model records 0 for
    every COND (reported to verification): expect each MEAS COND's value from its register (each
    is written once here)."""
    from dataclasses import replace
    from emu.cosim import Kind
    from emu.isa import unpack_cond
    regs = rep.trace.engines[rep.engine].regs
    meas = {}
    for pc, w in enumerate(rep.image.words):
        ins = decode_or_none(w)
        if ins is not None and ins.op == Op.COND:
            _, m, rd = unpack_cond(ins.operand)
            if m:
                meas[pc] = regs[rd]
    exp = [replace(p, fields=(p.fields[0], p.fields[1], meas[p.pc], p.fields[3]))
           if p.kind == Kind.STEP and p.pc in meas else p for p in rep.expected]
    return replace(rep, expected=exp)


def decode_or_none(w):
    from emu.isa import decode
    try:
        return decode(w)
    except ValueError:
        return None


async def cosim_pins(dut, rep, role):
    """Co-sim (trace and registers), then every pin of every line against the model's drives:
    released <-> oe 0; a level <-> oe 1 and that level (an OD line drives only 0)."""
    await reset(dut)            # the bench samples from here: no X
    bench = Bench(dut)
    await cosim(dut, meas_values(rep), role)
    drives = rep.trace.engines[rep.engine].drives
    end = (rep.trace.engines[rep.engine].halt_cycle or len(drives)) - 1
    for t in range(1, min(end, len(drives))):
        for i, ln in enumerate(rep.image.lines):
            want = drives[t][i]
            out, oe = bench.pin(t, i if ln.pin is None else ln.pin)   # pin: default its index
            got = None if not oe else out
            assert got == want, f"line {i} at now {t}: RTL {got}, model {want}"


def i2c_write(confirm=None):
    """A master writes 0xA4 (MSB first) and the target ACKs, between a START and a STOP. The
    master holds sda low 40 cycles after START, so the targets have confirmed it (15 cycles)."""
    d = Description("i2cw", parties=("m", "t"))
    sda = d.line("sda", kind="od", owner="shared", idle=1)
    scl = d.line("scl", kind="od", owner="m", idle=1)
    d.bus("i2c", sda, scl, confirm=confirm)
    lane = d.lane("ln", sda, clk=scl, cpol=1, cpha=1, ldelay=3)
    bit = d.profile("H", nom=12, min=12, max=0xFFFF)
    cnd = d.profile("C", nom=12, min=4, max=0xFFFF)
    setup = d.profile("SU", nom=40, min=30, max=0xFFFF)
    d.li("r0", 0xA4)
    d.cond("start", owner="m", prof=cnd, meas="r5")
    d.hold(sda, 0, owner="m", prof=setup)
    d.xfer(8, lane, owner="m", prof=bit, src="r0", dst="r1", msb_first=True, abort=True)
    d.xfer(1, lane, owner="t", prof=bit, src="zero", dst="r2", abort=True)
    d.cond("fall", scl, owner="m", prof=cnd)
    d.hold(sda, 0, owner="m", prof=bit)
    d.hold(scl, 1, owner="m", prof=bit)
    d.cond("stop", owner="m", prof=cnd, meas="r6")
    d.mark(1)
    d.halt()
    d.label("exit")
    d.mark(2)
    d.halt()
    d.abort_to("exit")
    return compile_desc(d)


def spi_select():
    """SELECT (CS) with a gated MISO: duplex 0xA4 / 0x3C between CS falling and rising. The
    target drives MISO only from its first driving step until its detector confirms CS rising."""
    d = Description("sel", parties=("m", "t"))
    cs = d.line("cs", owner="m", idle=1)
    sck = d.line("sck", owner="m", idle=0)
    mosi = d.line("mosi", owner="m", idle=1)
    miso = d.line("miso", owner="t", idle=1, gated=True)
    d.bus("select", cs)
    lane = d.lane("ln", mosi, clk=sck, cpol=0, cpha=1, data2=miso)
    bit = d.profile("H", nom=12, min=12, max=0xFFFF)
    cnd = d.profile("C", nom=12, min=4, max=0xFFFF)
    setup = d.profile("SU", nom=20, min=10, max=0xFFFF)
    with d.only("m"):
        d.li("r0", 0xA4)
    with d.only("t"):
        d.li("r0", 0x3C)
    d.cond("start", owner="m", prof=cnd)
    d.hold(cs, 0, owner="m", prof=setup)
    d.xfer(8, lane, owner="m", prof=bit, src="r0", dst="r1", abort=True)
    d.cond("stop", owner="m", prof=cnd)
    d.mark(1)
    d.halt()
    d.label("exit")
    d.mark(2)
    d.halt()
    d.abort_to("exit")
    return compile_desc(d)


def kinds(rep):
    return [ev.kind for ev in rep.trace.engines[0].events]


@cocotb.test(skip=GL)
@cocotb.parametrize(role=["P0", "P1", "MON"])
async def test_i2c_write(dut, role):
    """START and STOP done by the master and followed by the others (MEAS from the flag's
    anchor), the address byte and the ACK, every pin as the model drives it."""
    r = Role[role]
    rep = model(i2c_write(), r)
    e = rep.trace.engines[0]
    assert e.regs[1] == 0xA4 and e.regs[2] == 0 and "MARK" in kinds(rep)
    if r != Role.P0:
        assert [k for _, k in e.bus_events] == ["START", "STOP"]
    await cosim_pins(dut, rep, r)


@cocotb.test(skip=GL)
@cocotb.parametrize(role=["P1", "MON"])
async def test_i2c_bus_exit(dut, role):
    """A START mid-byte (sda glitched low while scl is high, confirm 4): the followed XFER with
    ABORT makes a bus exit to abort_pc: a BUS record, no ABORT, no VIOL bit."""
    r = Role[role]
    rep = model(i2c_write(confirm=4), r, glitches=[Glitch(0, 80, 3, 0)])
    assert "BUS" in kinds(rep) and "ABORT" not in kinds(rep) and rep.trace.engines[0].regs[1] == 0
    await cosim_pins(dut, rep, r)


@cocotb.test(skip=GL)
@cocotb.parametrize(role=["P0", "P1", "MON"])
async def test_select_gated(dut, role):
    """SELECT with a gated MISO: the target drives it during the frame and releases it once its
    detector confirms CS rising; duplex dst = 0x3CA4 in every role."""
    r = Role[role]
    rep = model(spi_select(), r)
    assert rep.trace.engines[0].regs[1] == 0x3CA4
    if r == Role.MON:   # MON's MISO samples go on tr_sample2 (proposed in #50), not monitored yet
        from dataclasses import replace
        from emu.cosim import Kind
        rep = replace(rep, expected=[p for p in rep.expected if not (p.kind == Kind.SAMPLE and p.fields == (3,))])
    if r == Role.P1:
        miso = [d[3] for d in rep.trace.engines[0].drives]
        assert 1 in miso or 0 in miso, "the target never drives MISO"
        assert miso[-1] is None, "MISO is not released after CS rises"
    await cosim_pins(dut, rep, r)


@cocotb.test(skip=GL)
async def test_select_exit(dut):
    """CS glitched high mid-frame: the target's XFER with ABORT exits; its gated MISO is released."""
    rep = model(spi_select(), Role.P1, glitches=[Glitch(0, 40, 6, 1)])
    assert "BUS" in kinds(rep)
    await cosim_pins(dut, rep, Role.P1)


@cocotb.test()
async def test_raw_evt_same_cycle(dut):
    """RAW EVT reads evt, then clears the masked bits (isa §4.1): a START flag confirmed in the
    cycle a RAW EVT starts wasn't read, so it must survive that RAW EVT's clear. RAW EVTs into r1-r4
    clear START every own cycle from 22; r5 reads it last. The START (sda falls while scl is
    high, confirm 1) is confirmed at w + 2 + 1, odd for w = 20 and even (a RAW EVT's start) for
    w = 21: either way exactly one read sees it."""
    SDA, SCL = 0, 1
    evt = Instr(Op.RAW, sub=int(RawOp.EVT), operand=0x01)
    prog = [Instr(Op.RAW, sub=int(RawOp.DELAY), operand=20)]
    prog += [Instr(Op.RAW, sub=int(RawOp.EVT), rd=k, operand=0x01) for k in (1, 2, 3, 4)]
    prog += [Instr(Op.RAW, sub=int(RawOp.EVT), rd=5, operand=0), HALT]
    lines = [line_byte(i, od=True, owner=2, idle=1) for i in range(8)]
    lines[SDA] = line_byte(SDA, od=True, owner=2, idle=1)
    lines[SCL] = line_byte(SCL, od=True, owner=0, idle=1)   # P0 owns scl: P1 runs the detector
    for w in (20, 21):
        await reset(dut)
        dut.uio_in.value = 1 << SDA | 1 << SCL
        host = Host(dut)
        bench = Bench(dut)
        await host.write(BUS_KIND, [1])
        await host.write(BUS_AB, [SDA | SCL << 3])
        await host.write(BUS_CONFIRM, [1])
        bench.at[w] = 1 << SCL                              # START: sda falls, scl high
        bench.at[w + 30] = 1 << SDA | 1 << SCL              # (a STOP well after the reads)
        await run_program(host, [encode(i) for i in prog], 1, lines)
        from chip import wait_halted
        await wait_halted(host)
        reads = [await get_reg(host, k) & 1 for k in (1, 2, 3, 4)]
        last = await get_reg(host, 5) & 1
        assert sum(reads) + last == 1, f"w {w}: START read {reads} then {last}: lost or read twice"
        assert sum(reads) == 1, f"w {w}: the START (confirmed by 24) was read by none of r1-r4"
