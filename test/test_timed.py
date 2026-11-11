"""Timed steps (isa §4.1-4.3): COND FALL/RISE, HOLD and unclocked XFER, owner and follower,
with OVERRUN, EARLY, TIMEOUT, LEVEL, EDGE and ABORT.

Every expected cycle is derived from isa.md in the test, never read off the RTL:
- instruction 0 starts at now = 2; a successor starts on the first own (even) cycle
  >= max(completion + 1, start + 2) (§4.0);
- a drive in cycle t is on the wire from t + 1; an input on the wire from cycle w is detected
  at w + 2, so e = detect - EDGE_LAT = w - 1, and a sample at cycle s reads the wire at s - 2
  (§1: SYNC_LAT 2, EDGE_LAT 3);
- an abort decided at t starts abort_pc at (first even >= t + 1) + 2 (§4.0).
The bench (chip.Bench) maps every pin sample to `now` through the ACT pin, so these tests run
at gate level too, except test_timed_trace (the RTL trace port)."""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolchain"))

from emu.isa import (BrCond, CondKind, Dst, F_ABORT, F_CHK, F_MSB, F_RESYNC, Instr, Op, Owner, RawOp,
                     Role, Src, cond_operand, encode, li, xfer_operand)

from chip import (E_EVT, E_FV_KIND, E_NOW, E_TB, E_VIOL, Bench, Host, eng_read, get_reg, line_byte,
                  reset, run_program, wait_halted, write_tables)

GL = os.environ.get("GATES") == "yes"
EDGE_LAT = 3
OVERRUN, EARLY, TIMEOUT, LEVEL, EDGE, ABORT = 0, 1, 2, 3, 4, 6
INF = 0xFFFF
HALT = Instr(Op.HALT)
UNUSED = [line_byte(i, od=True, owner=2, idle=1) for i in range(8)]


def even_up(t):
    return t + (t & 1)


def nxt(start, done):
    """isa §4.0: the successor's start."""
    return even_up(max(done + 1, start + 2))


def after_abort(t):
    return even_up(t + 1) + 2


def cond(kind, line, owner, prof, meas=None, flags=0):
    return Instr(Op.COND, owner=owner, line=line, prof=prof, operand=cond_operand(kind, meas), flags=flags)


def hold(line, level, owner, prof, flags=0):
    return Instr(Op.HOLD, owner=owner, line=line, prof=prof, operand=level, flags=flags)


def xfer(n, lane, owner, src, dst, prof, flags=0):
    return Instr(Op.XFER, owner=owner, line=lane, prof=prof, operand=xfer_operand(n, src, dst), flags=flags)


def lines_with(**cfg):
    t = list(UNUSED)
    for k, v in cfg.items():
        t[int(k[1:])] = v
    return t


async def setup(dut, prog, role=0, lines=None, profs=None, lanes=None, abort_pc=None, uio=0):
    await reset(dut)
    dut.uio_in.value = uio
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, profs, lanes, abort_pc)
    await run_program(host, [encode(i) for i in prog], role, lines)
    return host, bench


async def viol(host):
    return await eng_read(host, E_VIOL, 2)


def wire_from(drives, t, idle):
    """The level on the wire during cycle t for drives [(cycle, level)] (on the wire from cycle + 1)."""
    v = idle
    for c, lv in drives:
        if c < t:
            v = lv
    return v


def check_wave(bench, pin, drives, idle, t0, t1):
    for t in range(t0, t1):
        out, oe = bench.pin(t, pin)
        want = wire_from(drives, t, idle)
        assert oe == 1 and out == want, f"pin {pin} at now {t}: out {out} oe {oe}, want {want}"


PP0 = line_byte(0, od=False, owner=0, idle=1)   # pin 0, PP, owned by P0, idle 1


# ---------------------------------------------------------------- owner: HOLD, COND, XFER

@cocotb.test()
async def test_hold_do(dut):
    """HOLD do acts at max(now, tb) and moves tb by nom; the first one uses the issue slot.

    HOLD 0 starts at 2 (planned 0, 2 late: inside ISSUE), tb = 100; HOLD 1 starts at 4, acts
    at 100; HOLD 2 starts at 102, acts at 200; HALT at 202, tb = 300."""
    prog = [hold(0, 0, Owner.P0, 1), hold(0, 1, Owner.P0, 1), hold(0, 0, Owner.P0, 1), HALT]
    host, bench = await setup(dut, prog, lines=lines_with(L0=PP0), profs={1: (90, 100, 110)})
    await wait_halted(host)
    assert await viol(host) == 0
    assert await eng_read(host, E_NOW, 3) == 202
    assert await eng_read(host, E_TB, 3) == 300
    check_wave(bench, 0, [(2, 0), (100, 1), (200, 0)], 1, 3, 230)


@cocotb.test()
async def test_cond_do_meas(dut):
    """COND do acts at max(now, tb + nom) and sets tb to the action cycle; MEAS = action - tb.

    COND FALL at 2 waits to 50 (r1 = 50); COND RISE starts at 52, acts at 100 (r2 = 50);
    HALT at 102, tb = 100. Then nom = 0 and late: 2 late is in the issue slot (no OVERRUN,
    MEAS 2); after a DELAY it overruns."""
    prog = [cond(CondKind.FALL, 0, Owner.P0, 1, meas=1), cond(CondKind.RISE, 0, Owner.P0, 1, meas=2), HALT]
    host, bench = await setup(dut, prog, lines=lines_with(L0=PP0), profs={1: (40, 50, 60)})
    await wait_halted(host)
    assert await viol(host) == 0
    assert [await get_reg(host, 1), await get_reg(host, 2)] == [50, 50]
    assert await eng_read(host, E_NOW, 3) == 102
    assert await eng_read(host, E_TB, 3) == 100
    check_wave(bench, 0, [(50, 0), (100, 1)], 1, 3, 120)

    prog = [cond(CondKind.FALL, 0, Owner.P0, 2, meas=1), Instr(Op.RAW, sub=int(RawOp.DELAY), operand=20),
            cond(CondKind.RISE, 0, Owner.P0, 2, meas=2), HALT]
    await write_tables(host, profs={2: (0, 0, 10)})
    await run_program(host, [encode(i) for i in prog], 0, lines_with(L0=PP0))
    await wait_halted(host)
    # COND 0 at 2: planned 0, 2 late, allowed (nom = 0). DELAY at 4 (20), COND at 24: 22 late.
    assert await get_reg(host, 1) == 2
    assert await get_reg(host, 2) == 22
    assert await viol(host) == 1 << OVERRUN
    assert await eng_read(host, E_FV_KIND) == OVERRUN


@cocotb.test()
async def test_hold_overrun(dut):
    """An owned HOLD more than ISSUE late logs OVERRUN and still drives; an IDLE one never does."""
    delay = Instr(Op.RAW, sub=int(RawOp.DELAY), operand=10)
    host, _ = await setup(dut, [delay, hold(0, 0, Owner.P0, 0), HALT], lines=lines_with(L0=PP0))
    await wait_halted(host)
    assert await viol(host) == 0, "IDLE HOLD overran"
    await write_tables(host, profs={1: (5, 10, 15)})
    await run_program(host, [encode(i) for i in (delay, hold(0, 0, Owner.P0, 1), HALT)], 0, lines_with(L0=PP0))
    await wait_halted(host)
    assert await viol(host) == 1 << OVERRUN
    assert int(dut.uio_out.value) & 1 == 0 and int(dut.uio_oe.value) & 1


@cocotb.test()
async def test_xfer_do(dut):
    """A UART frame: HOLD start bit, XFER 8 bits, HOLD stop bit, P = 20, LSB first then MSB first.

    HOLD at 2 (tb 20); LI at 4; XFER at 6, bit k at 20 + 20k, completes at 160 (a PP owner
    completes on its last bit); HOLD stop starts at 162, acts at 180; HALT at 182, tb 200.
    dst = the wire value = src for a PP owner."""
    for msb in (False, True):
        v = 0xA5 if not msb else 0x2C
        prog = [hold(0, 0, Owner.P0, 1), li(1, v),
                xfer(8, 0, Owner.P0, Src.R1, Dst.R2, 1, flags=F_MSB if msb else 0),
                hold(0, 1, Owner.P0, 1), HALT]
        host, bench = await setup(dut, prog, lines=lines_with(L0=PP0), profs={1: (19, 20, 21)}, lanes={0: 0})
        await wait_halted(host)
        assert await viol(host) == 0
        assert await get_reg(host, 2) == v
        assert await eng_read(host, E_NOW, 3) == 182
        assert await eng_read(host, E_TB, 3) == 200
        bits = [(v >> (7 - k if msb else k)) & 1 for k in range(8)]
        drives = [(2, 0)] + [(20 + 20 * k, b) for k, b in enumerate(bits)] + [(180, 1)]
        check_wave(bench, 0, drives, 1, 3, 200)


@cocotb.test()
async def test_xfer_od_readback(dut):
    """On an OD lane the owner reads its bits back: someone else's 0 shows up in dst, and it
    completes at the follower's offset. P = 20, XFER at 2 (tb 0): the last sample is at
    7P + P/2 + E = 153, so HALT is at 154. A PP owner completes on bit 7 at 140: HALT 142."""
    od = line_byte(1, od=True, owner=0, idle=1)
    prog = [xfer(8, 0, Owner.P0, Src.ONES, Dst.R3, 1), HALT]
    host, _ = await setup(dut, prog, lines=lines_with(L1=od), profs={1: (19, 20, 21)}, lanes={0: 1},
                          uio=0x00)  # the peer holds pin 1 low
    await wait_halted(host)
    assert await get_reg(host, 3) == 0x00
    assert await eng_read(host, E_NOW, 3) == 154
    pp = line_byte(1, od=False, owner=0, idle=1)
    await run_program(host, [encode(i) for i in prog], 0, lines_with(L1=pp))
    await wait_halted(host)
    assert await get_reg(host, 3) == 0xFF
    assert await eng_read(host, E_NOW, 3) == 142


# ---------------------------------------------------------------- follower: COND, HOLD, XFER

@cocotb.test()
async def test_cond_follow(dut):
    """A followed FALL on the wire from w is detected at w + 2: e = w - 1, MEAS = e - tb, tb := e.
    An edge before min - QUANT logs EARLY and still completes."""
    prog = [cond(CondKind.FALL, 0, Owner.P0, 1, meas=1), HALT]
    # min 60: EARLY iff m = w - 1 < min - QUANT = 59, so the boundary is w = 59 / 60
    for w, early in ((101, False), (61, False), (60, False), (59, True), (40, True)):
        host, bench = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs={1: (60, 100, 140)},
                                  uio=0x01)
        bench.at[w] = 0x00
        await wait_halted(host)
        e = w - 1
        assert await get_reg(host, 1) == e, f"w={w}"
        assert await viol(host) == (1 << EARLY if early else 0), f"w={w}"
        assert await eng_read(host, E_TB, 3) == e
        assert await eng_read(host, E_NOW, 3) == nxt(2, w + 2)


@cocotb.test()
async def test_cond_timeout(dut):
    """No edge: TIMEOUT once now - E > tb + max + QUANT, i.e. at now = max + 5 (tb 0).
    Without ABORT: `to` is set, tb := tb + nom, MEAS 0xFFFF, BR TO is taken.
    With ABORT: VIOL TIMEOUT + ABORT, tb := now, abort_pc starts at even(t + 1) + 2."""
    prof = {1: (80, 100, 120)}
    t = 120 + 5
    prog = [cond(CondKind.FALL, 0, Owner.P0, 1, meas=1), Instr(Op.BR, sub=int(BrCond.TO), operand=4),
            li(2, 0xBAD), HALT, li(2, 0x600D), HALT]
    host, _ = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs=prof, uio=0x01)
    await wait_halted(host)
    assert await get_reg(host, 1) == 0xFFFF
    assert await get_reg(host, 2) == 0x600D
    assert await viol(host) == 1 << TIMEOUT
    assert await eng_read(host, E_EVT) & 1 << 2
    br_at = nxt(2, t)
    assert await eng_read(host, E_NOW, 3) == br_at + 4   # BR (taken), LI, then HALT

    prog = [cond(CondKind.FALL, 0, Owner.P0, 1, flags=F_ABORT), HALT, HALT, HALT, li(3, 0xAB), HALT]
    host, _ = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs=prof, abort_pc=4, uio=0x01)
    await wait_halted(host)
    assert await get_reg(host, 3) == 0xAB
    assert await viol(host) == (1 << TIMEOUT) | (1 << ABORT)
    assert await eng_read(host, E_FV_KIND) == TIMEOUT
    assert await eng_read(host, E_NOW, 3) == after_abort(t) + 2
    assert await eng_read(host, E_TB, 3) == t


@cocotb.test()
async def test_hold_follow(dut):
    """A followed HOLD samples at tb + nom//2 + E = 53 (nom 100), which reads the wire at 51.
    A level change on the wire from 51 is seen, from 52 is not. LEVEL logs and continues
    (tb := 100); with ABORT it goes to abort_pc at even(53 + 1) + 2 = 56."""
    prof = {1: (90, 100, 110)}
    prog = [hold(0, 0, Owner.P0, 1), HALT]
    for w, ok in ((51, True), (52, False)):
        host, bench = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs=prof, uio=0x01)
        bench.at[w] = 0x00
        await wait_halted(host)
        assert await viol(host) == (0 if ok else 1 << LEVEL), f"w={w}"
        assert await eng_read(host, E_TB, 3) == 100
        assert await eng_read(host, E_NOW, 3) == nxt(2, 53)
    prog = [hold(0, 0, Owner.P0, 1, flags=F_ABORT), HALT, li(3, 0xAB), HALT]
    host, _ = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs=prof, abort_pc=2, uio=0x01)
    await wait_halted(host)
    assert await viol(host) == (1 << LEVEL) | (1 << ABORT)
    assert await eng_read(host, E_EVT) & 1 << 3
    assert await get_reg(host, 3) == 0xAB
    assert await eng_read(host, E_NOW, 3) == after_abort(53) + 2


def uart_wave(bench, byte, start, period, idle=1):
    """Schedule a UART frame on pin 0: start bit from `start`, LSB first, then the stop bit."""
    bench.at[start] = 0
    for k in range(8):
        bench.at[start + period * (k + 1)] = (byte >> k) & 1
    bench.at[start + period * 9] = idle


UART_RX = [cond(CondKind.FALL, 0, Owner.P0, 0),                   # wait for the start bit
           hold(0, 0, Owner.P0, 1),                               # the start bit's middle
           xfer(8, 0, Owner.P0, Src.ZERO, Dst.R2, 1, flags=F_CHK | F_RESYNC),
           hold(0, 1, Owner.P0, 1),                               # stop bit
           HALT]


@cocotb.test()
async def test_xfer_follow(dut):
    """UART receive at P = 20 for random bytes: samples at a + (k - ak)P + P//2 + E."""
    rng = random.Random(7)
    for byte in [0x00, 0xFF, 0xA5] + [rng.randrange(256) for _ in range(3)]:
        host, bench = await setup(dut, UART_RX, role=1, lines=lines_with(L0=PP0), profs={1: (18, 20, 22)},
                                  lanes={0: 0}, uio=0x01)
        uart_wave(bench, byte, 40, 20)
        await wait_halted(host)
        assert await get_reg(host, 2) == byte, f"{byte:#04x}"
        assert await viol(host) == 0, f"{byte:#04x}: VIOL {await viol(host):#x}"
        # start edge on the wire from 40: e = 39 = tb; the stop HOLD ends at 39 + 10P
        assert await eng_read(host, E_TB, 3) == 39 + 10 * 20


@cocotb.test()
async def test_xfer_resync_and_edge(dut):
    """A sender 10% slow (22 for 20): without RESYNC the drift reaches a wrong bit, with RESYNC
    every in-window edge re-anchors. A sender far out of window (30) logs EDGE with CHK."""
    byte = 0x55  # an edge at every bit boundary
    prof = {1: (18, 20, 22)}
    plain = list(UART_RX)
    plain[2] = xfer(8, 0, Owner.P0, Src.ZERO, Dst.R2, 1)
    got = {}
    for name, prog in (("plain", plain), ("resync", UART_RX)):
        host, bench = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs=prof, lanes={0: 0},
                                  uio=0x01)
        uart_wave(bench, byte, 40, 22)
        await wait_halted(host)
        got[name] = (await get_reg(host, 2), await viol(host))
    assert got["resync"] == (byte, 0), got
    assert got["plain"][0] != byte, got

    host, bench = await setup(dut, UART_RX, role=1, lines=lines_with(L0=PP0), profs=prof, lanes={0: 0},
                              uio=0x01)
    uart_wave(bench, byte, 40, 30)
    await wait_halted(host)
    assert await viol(host) & 1 << EDGE


# ---------------------------------------------------------------- trace port (RTL only)

@cocotb.test(skip=GL)
async def test_timed_trace(dut):
    """tr_step for each HOLD of test_hold_do, and the bench's now mapping against tr_now."""
    prog = [hold(0, 0, Owner.P0, 1), hold(0, 1, Owner.P0, 1), hold(0, 0, Owner.P0, 1), HALT]
    eng = dut.user_project.u_eng0
    steps, nows = [], []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            if int(eng.tr_step.value):
                steps.append((int(eng.tr_pc.value), int(eng.tr_op.value), int(eng.tr_do.value),
                              int(eng.tr_value.value), int(eng.tr_now.value), int(eng.tr_tb.value)))
            if int(eng.tr_issue.value):
                nows.append(int(eng.tr_now.value))

    await reset(dut)
    host = Host(dut)
    bench = Bench(dut, probe=lambda: (int(eng.tr_now.value), int(eng.running.value)))
    cocotb.start_soon(watch())
    await write_tables(host, {1: (90, 100, 110)})
    await run_program(host, [encode(i) for i in prog], 0, lines_with(L0=PP0))
    await wait_halted(host)
    assert steps == [(0, 2, 1, 0, 2, 100), (1, 2, 1, 1, 100, 200), (2, 2, 1, 0, 200, 300)], steps
    assert nows == [2, 4, 102, 202], nows
    # the bench's mapping against tr_now, for every cycle the engine ran
    ran = [k for k, (_, run) in enumerate(bench.probed) if run]
    assert len(ran) > 200
    for k in ran[1:]:
        assert bench.probed[k][0] == bench.now_of(k), f"sample {k}: tr_now {bench.probed[k][0]}"


@cocotb.test()
async def test_xfer_edge_window(dut):
    """CHK window at its bounds: the anchor is the start edge (e = 39), the start-bit HOLD adds
    (min, max) = (18, 22), so boundary 0's edge must have m = e - anchor in [18 - 1, 22 + 1].
    A start bit of L cycles puts that edge at m = L; the byte is 0xFF, so it is the only edge."""
    prog = list(UART_RX)
    prog[2] = xfer(8, 0, Owner.P0, Src.ZERO, Dst.R2, 1, flags=F_CHK)
    for length, ok in ((16, False), (17, True), (23, True), (24, False)):
        host, bench = await setup(dut, prog, role=1, lines=lines_with(L0=PP0), profs={1: (18, 20, 22)},
                                  lanes={0: 0}, uio=0x01)
        bench.at[40] = 0
        bench.at[40 + length] = 1
        await wait_halted(host)
        assert await get_reg(host, 2) == 0xFF, f"L={length}"
        got = await viol(host)
        assert got == (0 if ok else 1 << EDGE), f"start bit {length}: VIOL {got:#x}"


@cocotb.test()
async def test_xfer_od_late_single_bit(dut):
    """An OD owner's 1-bit XFER that starts late enough to drive and read back in its start cycle
    still ends at tb + P. DELAY 38 at 2, so the XFER starts at 40 with tb 0 (OVERRUN); its sample
    is due at P//2 + E = 13 <= 40, so it completes at 40 with tb = 20; HALT at 42."""
    od = line_byte(1, od=True, owner=0, idle=1)
    prog = [Instr(Op.RAW, sub=int(RawOp.DELAY), operand=38), xfer(1, 0, Owner.P0, Src.ONES, Dst.R3, 1), HALT]
    host, _ = await setup(dut, prog, lines=lines_with(L1=od), profs={1: (19, 20, 21)}, lanes={0: 1}, uio=0x02)
    await wait_halted(host)
    assert await eng_read(host, E_NOW, 3) == 42
    assert await eng_read(host, E_TB, 3) == 20
    assert await get_reg(host, 3) == 1        # released, and the pad reads 1
    assert await viol(host) == 1 << OVERRUN
