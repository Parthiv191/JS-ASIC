"""Clocked XFER (isa §4.3), one data line: the clock owner, clock followers, data owners without
the clock, CPOL/CPHA, ldelay, CLK_PHASE, TIMEOUT and UNDERRUN.

Every expected cycle is derived from isa.md in the test, never read off the RTL (the rules are in
test_timed's docstring). For the clocked lane, with H = nom:
- the clock owner sets base = max(tb, start) (cpha 0) or max(tb, start - H) (cpha 1) and drives
  edge j (1..2n) at base + jH, to 1 - cpol when j is odd (leading), else to cpol;
- a launch goes out at its launching edge + ldelay (the clock owner) or at
  max(detect, e + ldelay) (anyone else); with cpha 0 bit 0 goes out at max(start, tb);
- the clock owner samples at capture + EDGE_LAT, anyone else when it detects the capture edge;
- tb := base + 2nH (the clock owner) or detect(last trailing edge) - EDGE_LAT (others)."""

import os
import random
import sys
from pathlib import Path

import cocotb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolchain"))

from emu.isa import (Dst, F_ABORT, F_CHK, F_MSB, Instr, Op, Owner, RawOp, Src, encode, li, xfer_operand)

from chip import (E_EVT, E_FV_KIND, E_NOW, E_TB, E_VIOL, Bench, Host, clocked_lane, eng_read, get_reg,
                  line_byte, reset, run_program, wait_halted, write_tables)

GL = os.environ.get("GATES") == "yes"
EDGE_LAT = 3
TIMEOUT, CLK_PHASE, ABORT, UNDERRUN = 2, 5, 6, 8
HALT = Instr(Op.HALT)
UNUSED = [line_byte(i, od=True, owner=2, idle=1) for i in range(8)]
CLK, DAT = 0, 1   # lines and pins


def even_up(t):
    return t + (t & 1)


def nxt(start, done):
    """isa §4.0: the successor's start."""
    return even_up(max(done + 1, start + 2))


def after_abort(t):
    return even_up(t + 1) + 2


def xfer(n, owner, src, dst, prof=1, flags=0, lane=0):
    return Instr(Op.XFER, owner=owner, line=lane, prof=prof, operand=xfer_operand(n, src, dst), flags=flags)


def lines(cpol, clk_owner=0, dat_owner=0, dat_od=False):
    t = list(UNUSED)
    t[CLK] = line_byte(CLK, od=False, owner=clk_owner, idle=cpol)
    t[DAT] = line_byte(DAT, od=dat_od, owner=dat_owner, idle=1)
    return t


async def setup(dut, prog, role, cpol, cpha, ld=0, prof=(5, 5, 5), uio=None, abort_pc=None, **lk):
    await reset(dut)
    dut.uio_in.value = (cpol | 1 << DAT) if uio is None else uio
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, {1: prof}, {0: clocked_lane(DAT, CLK, cpol, cpha, ld)}, abort_pc)
    await run_program(host, [encode(i) for i in prog], role, lines(cpol, **lk))
    return host, bench


def wire_from(drives, t, idle):
    v = idle
    for c, lv in sorted(drives):
        if c < t:
            v = lv
    return v


def check_wave(bench, pin, drives, idle, t0, t1, what):
    for t in range(t0, t1):
        out, oe = bench.pin(t, pin)
        want = wire_from(drives, t, idle)
        assert oe == 1 and out == want, f"{what}: pin {pin} at now {t}: out {out} oe {oe}, want {want}"


def owner_plan(n, H, cpol, cpha, ld, v, start=2, tb=0):
    """isa §4.3 for the clock owner that also owns the data: (clock drives, data drives, tb after)."""
    base = max(tb, start) if cpha == 0 else max(tb, start - H)
    clk = [(base + j * H, cpol ^ (j & 1)) for j in range(1, 2 * n + 1)]
    if cpha == 0:
        dat = [(max(start, tb), v & 1)] + [(base + 2 * k * H + ld, v >> k & 1) for k in range(1, n)]
    else:
        dat = [(base + (2 * k + 1) * H + ld, v >> k & 1) for k in range(n)]
    return clk, dat, base + 2 * n * H


def master_wave(bench, n, H, cpol, cpha, v, b0):
    """The bench as clock and data owner: edge j at b0 + jH; data bit k set at its launching
    edge (cpha 0: bit 0 at b0 - H). Returns the cycle (on the wire) of every edge."""
    level = {CLK: cpol, DAT: 1}
    sched = {}
    edges = [b0 + j * H for j in range(1, 2 * n + 1)]
    for j, w in enumerate(edges, 1):
        sched.setdefault(w, {})[CLK] = cpol ^ (j & 1)
    for k in range(n):
        w = (b0 - H if k == 0 else b0 + 2 * k * H) if cpha == 0 else b0 + (2 * k + 1) * H
        sched.setdefault(w, {})[DAT] = v >> k & 1
    for w in sorted(sched):
        level.update(sched[w])
        bench.at[w] = level[CLK] << CLK | level[DAT] << DAT
    return edges


@cocotb.test()
async def test_clock_owner(dut):
    """The clock and data owner (P0) in all four modes, with ldelay 0 and 3: every edge and launch
    on its cycle, dst = src, and tb := base + 2nH. H = 5, so cpha 1 has base = max(0, 2 - 5) = 0."""
    H, n, v = 5, 8, 0xB4
    for cpol in (0, 1):
        for cpha in (0, 1):
            for ld in (0, 3):
                prog = [xfer(n, Owner.P0, Src.R1, Dst.R2), HALT]
                host, bench = await setup(dut, [li(1, v)] + prog, 0, cpol, cpha, ld, prof=(H, H, H))
                await wait_halted(host)
                what = f"cpol {cpol} cpha {cpha} ld {ld}"
                clk, dat, tb = owner_plan(n, H, cpol, cpha, ld, v, start=4)   # LI at 2, XFER at 4
                check_wave(bench, CLK, clk, cpol, 1, tb + 4, what)
                check_wave(bench, DAT, dat, 1, 1, tb + 4, what)
                assert await get_reg(host, 2) == v, what
                assert await eng_read(host, E_TB, 3) == tb, what
                assert await eng_read(host, E_NOW, 3) == nxt(4, tb), what
                assert await eng_read(host, E_VIOL, 2) == 0, what


@cocotb.test()
async def test_clock_owner_reads(dut):
    """The clock owner follows the data (the bench owns it, P1): it samples at capture + E, so
    with cpha 1 it completes E after its last trailing edge. The bench sets each bit at the
    clock owner's launching edge as it sees it on the wire."""
    H, n = 6, 8
    for cpha in (0, 1):
        v = random.randrange(256)
        # DELAY 40 first, so the bench can act on every edge (setup returns a few cycles after RUN)
        prog = [Instr(Op.RAW, sub=int(RawOp.DELAY), operand=40), xfer(n, Owner.P1, Src.ZERO, Dst.R2), HALT]
        host, bench = await setup(dut, prog, 0, 0, cpha, prof=(H, H, 0xFFFF), dat_owner=1,
                                  uio=(v & 1 if cpha == 0 else 1) << DAT)
        start = 42
        clk, _, tb = owner_plan(n, H, 0, cpha, 0, 0, start=start)
        # each data bit on the wire from the drive of its launching edge (cpha 0: bit 0 from RUN)
        for k in range(1 if cpha == 0 else 0, n):
            w = clk[2 * k - 1][0] if cpha == 0 else clk[2 * k][0]
            bench.at[w + 1] = (v >> k & 1) << DAT
        await wait_halted(host)
        samp_last = clk[2 * n - 1 - (1 - cpha)][0] + EDGE_LAT
        done = max(tb, samp_last)
        assert await get_reg(host, 2) == v, f"cpha {cpha}"
        assert await eng_read(host, E_TB, 3) == tb
        assert await eng_read(host, E_NOW, 3) == nxt(start, done), f"cpha {cpha}"


@cocotb.test()
async def test_clock_follower(dut):
    """A clock and data follower (P1) in all four modes: it samples when it detects each capture
    edge (the wire at the edge's cycle), and tb := detect(last trailing) - E = that edge - 1."""
    H, n, b0 = 6, 8, 20
    for cpol in (0, 1):
        for cpha in (0, 1):
            v = random.randrange(256)
            prog = [xfer(n, Owner.P0, Src.ZERO, Dst.R2), HALT]
            host, bench = await setup(dut, prog, 1, cpol, cpha, prof=(H, H, 0xFFFF), clk_owner=0)
            edges = master_wave(bench, n, H, cpol, cpha, v, b0)
            await wait_halted(host)
            what = f"cpol {cpol} cpha {cpha}"
            assert await get_reg(host, 2) == v, what
            assert await eng_read(host, E_TB, 3) == edges[-1] - 1, what
            assert await eng_read(host, E_NOW, 3) == nxt(2, edges[-1] + 2), what


@cocotb.test()
async def test_data_owner_follows_clock(dut):
    """A data owner without the clock (P1 owns the data, P0 the clock): with cpha 0 bit 0 goes
    out at max(start, tb) = 2, and each later bit at max(detect, e + ldelay) of its launching
    edge, detect = wire + 2 and e = detect - E."""
    H, n, b0 = 8, 8, 20
    for cpha in (0, 1):
        for ld in (0, 6):
            v = random.randrange(256)
            prog = [li(1, v), xfer(n, Owner.P1, Src.R1, Dst.R2), HALT]
            host, bench = await setup(dut, prog, 1, 0, cpha, ld, prof=(H, H, 0xFFFF), clk_owner=0, dat_owner=1)
            edges = master_wave(bench, n, H, 0, cpha, 0, b0)
            for w in list(bench.at):
                bench.at[w] &= 1 << CLK          # the bench drives only the clock
            await wait_halted(host)
            launch = [edges[2 * k - 1] if cpha == 0 else edges[2 * k] for k in range(n)]
            dat = [(max(w + 2, w + 2 - EDGE_LAT + ld), v >> k & 1) for k, w in enumerate(launch)]
            if cpha == 0:
                dat[0] = (4, v & 1)                # XFER starts at 4 (after the LI), tb = 0
            what = f"cpha {cpha} ld {ld}"
            check_wave(bench, DAT, dat, 1, 1, edges[-1] + 3, what)
            assert await get_reg(host, 2) == v, what
            assert await eng_read(host, E_TB, 3) == edges[-1] - 1, what


@cocotb.test()
async def test_clock_timeout(dut):
    """A bounded clock follower: TIMEOUT once now - ref > max + QUANT, ref the start and then
    each detected edge. Without ABORT: to set, tb := now, no dst; with ABORT: abort_pc."""
    H, n, b0, mx = 6, 8, 20, 30   # the first edge (26) comes 24 after the start (4)
    for abort in (False, True):
        prog = [li(2, 0x77), xfer(n, Owner.P0, Src.ZERO, Dst.R2, flags=F_ABORT if abort else 0), HALT,
                li(3, 0xAB), HALT]
        host, bench = await setup(dut, prog, 1, 0, 0, prof=(4, H, mx), clk_owner=0, abort_pc=3)
        edges = master_wave(bench, n, H, 0, 0, 0xFF, b0)
        for w in list(bench.at):
            if w > edges[4]:                       # the clock stops after its fifth edge
                del bench.at[w]
        await wait_halted(host)
        t = edges[4] + 2 + mx + 2                  # now - detect > max + 1
        assert await eng_read(host, E_VIOL, 2) == (1 << TIMEOUT) | ((1 << ABORT) if abort else 0)
        assert await eng_read(host, E_EVT) & 1 << 2
        if abort:
            assert await get_reg(host, 3) == 0xAB
            assert await eng_read(host, E_NOW, 3) == after_abort(t) + 2
        else:
            assert await get_reg(host, 2) == 0x77, "a timed-out XFER writes no dst"
            assert await eng_read(host, E_TB, 3) == t
            assert await eng_read(host, E_NOW, 3) == nxt(4, t)


@cocotb.test()
async def test_clock_phase(dut):
    """CHK: CLK_PHASE when two detected edges are less than min - QUANT apart (the first edge
    has no phase). min 5: a 4-cycle half period passes, a 3-cycle one fails."""
    n = 2
    for half, bad in ((4, False), (3, True)):
        prog = [xfer(n, Owner.P0, Src.ZERO, Dst.R2, flags=F_CHK), HALT]
        host, bench = await setup(dut, prog, 1, 0, 0, prof=(5, 6, 0xFFFF), clk_owner=0)
        w = 20
        for j in range(1, 2 * n + 1):
            bench.at[w] = (j & 1) << CLK | 1 << DAT
            w += 6 if j != 2 else half             # edges 2 and 3 are `half` apart
        await wait_halted(host)
        got = await eng_read(host, E_VIOL, 2)
        assert got == ((1 << CLK_PHASE) if bad else 0), f"half {half}: VIOL {got:#x}"
        if bad:
            assert await eng_read(host, E_EVT) & 1 << 3
            assert await eng_read(host, E_FV_KIND) == CLK_PHASE


@cocotb.test()
async def test_underrun(dut):
    """A data owner without the clock and an empty TX FIFO: src = FIFO sends ONES, logs UNDERRUN
    (evt bit 5), consumes nothing and doesn't wait (isa §4.3)."""
    H, n, b0 = 6, 4, 20
    prog = [xfer(n, Owner.P1, Src.FIFO, Dst.R2), HALT]
    host, bench = await setup(dut, prog, 1, 0, 1, prof=(H, H, 0xFFFF), clk_owner=0, dat_owner=1)
    edges = master_wave(bench, n, H, 0, 1, 0, b0)
    for w in list(bench.at):
        bench.at[w] &= 1 << CLK
    await wait_halted(host)
    assert await get_reg(host, 2) == 0xF
    assert await eng_read(host, E_VIOL, 2) == 1 << UNDERRUN
    assert await eng_read(host, E_EVT) & 1 << 5
    for t in range(1, edges[-1] + 3):
        assert bench.pin(t, DAT) == (1, 1), f"now {t}: ONES"


# ---------------------------------------------------------------- co-sim against the model
# The model (toolchain/emu/model) is the reference: each case runs one description on the model in
# every party and on the RTL in one role, through verification's harness (test/cosim, imported).
# It compares the trace port and r0-r7, not the RTL's own pin drives (the hand tests above check
# those), so it is RTL only.
if not GL:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "cosim"))
    from harness import cosim
    from emu.compiler import compile_desc
    from emu.cosim import replay
    from emu.desc import Description
    from emu.isa import Role
    from emu.model.system import EngineSpec, Glitch

SPI = {   # name: lane mode, data owner and kind, H
    "mode0": dict(),
    "mode1": dict(cpha=1),
    "mode2": dict(cpol=1),
    "mode3_ld": dict(cpol=1, cpha=1, ld=2, H=10),
    "target": dict(dat_owner="b"),
    "target_cpha1_ld": dict(dat_owner="b", cpha=1, ld=5, H=10),
    "od_target": dict(kind="od", dat_owner="b"),
    "i2c_like": dict(kind="od", sck_kind="od", cpol=1, cpha=1, ld=3, H=12),
    "msb16": dict(n=16, msb=True),
}


def spi(cpol=0, cpha=0, ld=0, dat_owner="a", kind="pp", n=8, H=6, sck_kind="pp", msb=False):
    """One clocked XFER of 0xA4 (0xB4A4 for n = 16) from r0 into r1, a MARK, then a second one
    from r1 into r2, so a step follows a clocked one. A target with cpha 0 gets no second one:
    its bit 0 can't be valid by the master's first capture edge (T3)."""
    d = Description("spi", parties=("a", "b"))
    sck = d.line("sck", kind=sck_kind, owner="a", idle=cpol)
    dat = d.line("dat", kind=kind, owner=dat_owner, idle=1)
    lane = d.lane("ln", dat, clk=sck, cpol=cpol, cpha=cpha, ldelay=ld)
    bit = d.profile("H", nom=H, min=H, max=0xFFFF)
    d.li("r0", 0xB4A4 if n == 16 else 0xA4)
    d.xfer(n, lane, owner=dat_owner, prof=bit, src="r0", dst="r1", msb_first=msb)
    d.mark(9)
    if dat_owner == "a" or cpha == 1:
        d.xfer(n, lane, owner=dat_owner, prof=bit, src="r1", dst="r2", msb_first=msb)
    else:
        d.li("r2", 0xA4)
    d.halt()
    return compile_desc(d)


def model(img, role, glitches=()):
    specs = [EngineSpec(img, role)] + [EngineSpec(img, r) for r in Role if r != role]
    return replay(specs, 0, glitches=glitches)


@cocotb.test(skip=GL)
@cocotb.parametrize(case=list(SPI), role=["P0", "P1", "MON"])
async def test_cosim_clocked(dut, case, role):
    """Every lane mode, a target sending, OD data (an owner reads its bits back) and an OD clock,
    16 bits MSB first: in every role, against the model."""
    r = Role[role]
    rep = model(spi(**SPI[case]), r)
    want = 0xB4A4 if SPI[case].get("n") == 16 else 0xA4
    assert [et.regs[2] for et in rep.trace.engines] == [want] * 3, "the scenario moves no data"
    await cosim(dut, rep, r)


@cocotb.test(skip=GL)
@cocotb.parametrize(role=["P1", "MON"])
async def test_cosim_clocked_glitch(dut, role):
    """A follower's samples come from the wire: a glitch over data bit 2's capture edge flips that
    bit (0xA4 -> 0xA0) for every reader."""
    r = Role[role]
    clean = model(spi(), r)
    rep = model(spi(), r, glitches=[Glitch(1, 30, 6, 0)])   # data bit 2's capture edge is at 34
    assert rep.trace.engines[0].regs[1] != clean.trace.engines[0].regs[1], "the glitch changes nothing"
    await cosim(dut, rep, r)


@cocotb.test()
async def test_clock_owner_lane3(dut):
    """The clock owner on lane 3: its LANE bytes are at 0x0444-0x0446, so every LANE byte position
    is written and read (the gds flow once built LANE byte k at bit k, which lane 0's first byte
    alone can't show). Mode 3 with ldelay 3."""
    H, n, v, cpol, cpha, ld = 5, 8, 0x5C, 1, 1, 3
    prog = [li(1, v), xfer(n, Owner.P0, Src.R1, Dst.R2, lane=3), HALT]
    await reset(dut)
    dut.uio_in.value = cpol | 1 << DAT
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, {1: (H, H, H)}, {3: clocked_lane(DAT, CLK, cpol, cpha, ld)})
    await run_program(host, [encode(i) for i in prog], 0, lines(cpol))
    await wait_halted(host)
    clk, dat, tb = owner_plan(n, H, cpol, cpha, ld, v, start=4)
    check_wave(bench, CLK, clk, cpol, 1, tb + 4, "lane 3")
    check_wave(bench, DAT, dat, 1, 1, tb + 4, "lane 3")
    assert await get_reg(host, 2) == v
