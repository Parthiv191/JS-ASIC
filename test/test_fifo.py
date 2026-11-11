"""FIFOs (isa §1, §4.9; architecture §4): PUSH and POP, XFER src/dst = FIFO, the host's TXDATA
and RXDATA, OVERFLOW, the POP stall and its re-base. Timing claims are derived from isa §4.0-§4.3
in each test; programs come from the v1 encoder."""

import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolchain"))

from emu.isa import Dst, Instr, Op, Owner, Src, encode, li, xfer_operand
from emu.isa import CondKind, cond_operand

from chip import (E_EVT, E_FIFO_COUNTS, E_HALTED, E_NOW, E_RUNNING, E_RX_EMPTY, E_RX_FULL, E_RXDATA,
                  E_STATUS, E_TB, E_TX_EMPTY, E_TX_FULL, E_TXDATA, E_VIOL, ENG0, SOFT_RESET,
                  STATUS_HOST_ERR, Bench, Host, eng_read, get_reg, line_byte, reset, run_program,
                  wait_halted, write_tables)

HALT = Instr(Op.HALT)
OVERRUN, OVERFLOW = 0, 7
UNUSED = [line_byte(i, od=True, owner=2, idle=1) for i in range(8)]
PP0 = line_byte(0, od=False, owner=0, idle=1)


def push(ra):
    return Instr(Op.PUSH, ra=ra)


def pop(rd):
    return Instr(Op.POP, rd=rd)


def words(prog):
    return [i if isinstance(i, int) else encode(i) for i in prog]


def lines_with(**cfg):
    t = list(UNUSED)
    for k, v in cfg.items():
        t[int(k[1:])] = v
    return t


async def tx_write(host, values):
    """TXDATA in one pair-mode frame: each word pushes on its high byte."""
    data = []
    for v in values:
        data += [v & 0xFF, v >> 8]
    await host.write(ENG0 + E_TXDATA, data, pair=True)


async def rx_read(host, n):
    """n words of RXDATA in one pair-mode frame; each pops once its high byte has shifted out."""
    b = await host.read(ENG0 + E_RXDATA, 2 * n, pair=True)
    return [b[2 * i] | b[2 * i + 1] << 8 for i in range(n)]


async def counts(host):
    c = await eng_read(host, E_FIFO_COUNTS)
    return c & 0x0F, c >> 4   # (tx, rx)


@cocotb.test()
async def test_push_rxdata(dut):
    """PUSH writes the RX FIFO in order; RXDATA streams it out (the low byte's read-ahead takes the
    next entry while the previous high byte is still shifting); empty reads 0 and pops nothing."""
    vals = [0x1234, 0xBEEF, 0x0001, 0x8000, 0x5A5A]
    prog = []
    for v in vals:
        prog += [li(1, v), push(1)]
    prog.append(HALT)
    await reset(dut)
    host = Host(dut)
    await run_program(host, words(prog))
    await wait_halted(host)
    assert await counts(host) == (0, 5)
    assert await eng_read(host, E_STATUS) == E_HALTED | E_TX_EMPTY
    assert await rx_read(host, 5) == vals
    assert await counts(host) == (0, 0)
    assert await eng_read(host, E_STATUS) == E_HALTED | E_TX_EMPTY | E_RX_EMPTY
    assert await rx_read(host, 1) == [0]


@cocotb.test()
async def test_rx_partial_read_does_not_pop(dut):
    """Only a fully shifted-out high byte pops: the low byte alone, or the high byte alone, doesn't."""
    await reset(dut)
    host = Host(dut)
    await run_program(host, words([li(1, 0xCAFE), push(1), HALT]))
    await wait_halted(host)
    assert await host.read(ENG0 + E_RXDATA, 1) == [0xFE]
    assert await counts(host) == (0, 1)
    assert await host.read(ENG0 + E_RXDATA, 2) == [0xFE, 0xCA]   # low, then high: pops
    assert await counts(host) == (0, 0)


@cocotb.test()
async def test_rx_overflow(dut):
    """The 9th PUSH into the 8-deep RX FIFO is dropped with OVERFLOW (VIOL bit 7, evt bit 4); the
    engine doesn't stall (isa §4.9)."""
    prog = []
    for v in range(1, 10):
        prog += [li(1, v), push(1)]
    prog.append(HALT)
    await reset(dut)
    host = Host(dut)
    await run_program(host, words(prog))
    await wait_halted(host)
    assert await counts(host) == (0, 8)
    assert await eng_read(host, E_STATUS) & E_RX_FULL
    assert await eng_read(host, E_VIOL, 2) == 1 << OVERFLOW
    assert await eng_read(host, E_EVT) & 1 << 4
    assert await eng_read(host, E_NOW, 3) == 2 + 18 * 2   # 18 instructions before HALT, U each
    assert await rx_read(host, 8) == list(range(1, 9))


@cocotb.test()
async def test_txdata_pop(dut):
    """TXDATA before RUN fills the TX FIFO (RUN keeps it); POP takes words in order at U each.
    A 5th TXDATA word while full is dropped and sets STATUS.host_err."""
    await reset(dut)
    host = Host(dut)
    await tx_write(host, [0x1111, 0x2222, 0x3333, 0x4444])
    assert await counts(host) == (4, 0)
    assert await eng_read(host, E_STATUS) & E_TX_FULL
    await tx_write(host, [0x5555])
    assert (await host.read(0x0003, 1))[0] & STATUS_HOST_ERR
    assert await counts(host) == (4, 0)
    await run_program(host, words([pop(1), pop(2), pop(3), pop(4), HALT]))
    await wait_halted(host)
    assert [await get_reg(host, i) for i in (1, 2, 3, 4)] == [0x1111, 0x2222, 0x3333, 0x4444]
    assert await eng_read(host, E_NOW, 3) == 2 + 4 * 2      # no stall: U each
    assert await counts(host) == (0, 0)


@cocotb.test()
async def test_pop_stall_rebase(dut):
    """POP on an empty TX FIFO stalls, retrying on own cycles, and completes on the first one with
    data. The owned HOLD after it re-bases, tb := max(tb, start) (isa §4.1): it drives at its start
    with no OVERRUN, so HALT starts 2 later and tb = HOLD start + nom = NOW + nom - 2."""
    prog = [pop(1), Instr(Op.HOLD, owner=Owner.P0, line=0, prof=1, operand=0), HALT]
    await reset(dut)
    host = Host(dut)
    await write_tables(host, profs={1: (90, 100, 110)})
    await run_program(host, words(prog), lines=lines_with(L0=PP0))
    await ClockCycles(dut.clk, 500)
    assert await eng_read(host, E_STATUS) & E_RUNNING
    await tx_write(host, [0xABCD])
    await wait_halted(host)
    assert await get_reg(host, 1) == 0xABCD
    now = await eng_read(host, E_NOW, 3)
    assert now > 500 and now % 2 == 0
    assert await eng_read(host, E_TB, 3) == now + 100 - 2
    assert await eng_read(host, E_VIOL, 2) == 0, "the re-based HOLD overran"
    assert int(dut.uio_out.value) & 1 == 0


@cocotb.test()
async def test_xfer_src_fifo_waits(dut):
    """An owned src = FIFO XFER (PP, P = 20, 8 bits, LSB first) with an empty TX FIFO waits,
    checking every cycle; when the word arrives at cycle w it re-bases, so bit k goes out at
    w + kP with no OVERRUN, and tb ends at w + 8P (HALT at w + 7P + 2)."""
    prog = [Instr(Op.XFER, owner=Owner.P0, line=0, prof=1, operand=xfer_operand(8, Src.FIFO, Dst.R2)), HALT]
    await reset(dut)
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, profs={1: (19, 20, 21)}, lanes={0: 0})
    await run_program(host, words(prog), lines=lines_with(L0=PP0))
    await ClockCycles(dut.clk, 300)
    v = 0x5A                                   # bit 0 = 0: the first bit makes an edge
    await tx_write(host, [v])
    await wait_halted(host)
    assert await eng_read(host, E_VIOL, 2) == 0
    assert await get_reg(host, 2) == v
    # the first fall on the wire is at cycle w + 1 (a drive in w is on the wire from w + 1)
    k0 = next(k for k in range(bench.k_act, len(bench.samples)) if not bench.samples[k][1] & 1)
    w = bench.now_of(k0) - 1
    for k in range(8):
        for t in range(w + 1 + 20 * k, w + 1 + 20 * (k + 1)):
            out, oe = bench.pin(t, 0)
            assert oe and out == (v >> k) & 1, f"bit {k} at now {t}"
    assert await eng_read(host, E_TB, 3) == w + 8 * 20
    assert await eng_read(host, E_NOW, 3) == w + 7 * 20 + 2 + ((w + 7 * 20) & 1)


@cocotb.test()
async def test_xfer_src_fifo_ready(dut):
    """With the word already in TX at its start there is no wait and no re-base: the XFER at
    now 2 (tb 0) sends bit 0 in its issue slot at 2 and bit k at 20k; tb ends at 160."""
    prog = [Instr(Op.XFER, owner=Owner.P0, line=0, prof=1, operand=xfer_operand(8, Src.FIFO, Dst.R2)), HALT]
    await reset(dut)
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, profs={1: (19, 20, 21)}, lanes={0: 0})
    await tx_write(host, [0xA6])
    await run_program(host, words(prog), lines=lines_with(L0=PP0))
    await wait_halted(host)
    assert await get_reg(host, 2) == 0xA6
    assert await eng_read(host, E_TB, 3) == 160
    for k in range(8):
        lo = 3 if k == 0 else 20 * k + 1
        for t in range(lo, 20 * (k + 1) + 1):
            out, _ = bench.pin(t, 0)
            assert out == (0xA6 >> k) & 1, f"bit {k} at now {t}"


@cocotb.test()
async def test_dst_fifo_follower(dut):
    """A followed UART frame with dst = FIFO pushes the received byte; two frames, two words."""
    rx_bytes = [0x3C, 0xC3]
    frame = [Instr(Op.COND, owner=Owner.P0, line=0, prof=0, operand=cond_operand(CondKind.FALL)),
             Instr(Op.HOLD, owner=Owner.P0, line=0, prof=1, operand=0),
             Instr(Op.XFER, owner=Owner.P0, line=0, prof=1, operand=xfer_operand(8, Src.ZERO, Dst.FIFO)),
             Instr(Op.HOLD, owner=Owner.P0, line=0, prof=1, operand=1)]
    await reset(dut)
    dut.uio_in.value = 0x01
    host = Host(dut)
    bench = Bench(dut)
    await write_tables(host, profs={1: (18, 20, 22)}, lanes={0: 0})
    await run_program(host, words(frame + frame + [HALT]), role=1, lines=lines_with(L0=PP0))
    t = 40
    for byte in rx_bytes:
        bench.at[t] = 0
        for k in range(8):
            bench.at[t + 20 * (k + 1)] = (byte >> k) & 1
        bench.at[t + 180] = 1
        t += 300
    await wait_halted(host)
    assert await eng_read(host, E_VIOL, 2) == 0
    assert await rx_read(host, 2) == rx_bytes


@cocotb.test()
async def test_soft_reset_empties_fifos(dut):
    await reset(dut)
    host = Host(dut)
    await tx_write(host, [1, 2])
    await run_program(host, words([li(1, 7), push(1), Instr(Op.BR, sub=0, operand=2)]))  # loops
    await ClockCycles(dut.clk, 50)
    assert await counts(host) == (2, 1)
    await host.write(SOFT_RESET, [1])
    assert await counts(host) == (0, 0)


@cocotb.test(skip=__import__("os").environ.get("GATES") == "yes")
async def test_pop_retries_on_own_cycles(dut):
    """RTL trace: a stalled POP sees the word in the first cycle w where TX holds it, completes on
    the first own (even) cycle >= w, and its successor starts 2 later (isa §4.0). Pushed at two
    phases so w is odd once and even once."""
    from cocotb.triggers import RisingEdge
    e = dut.user_project.u_eng0
    for extra in (0, 1):
        await reset(dut)
        host = Host(dut)
        await run_program(host, words([pop(1), HALT]))
        await ClockCycles(dut.clk, 200 + extra)
        seen = {}

        async def watch():
            while "issue" not in seen:
                await RisingEdge(dut.clk)
                now = int(e.tr_now.value)
                if "w" not in seen and int(e.tx_n.value):
                    seen["w"] = now
                if "w" in seen and int(e.tr_issue.value) and int(e.tr_pc.value) == 1:
                    seen["issue"] = now

        mon = cocotb.start_soon(watch())
        await tx_write(host, [0x77])
        await wait_halted(host)
        await mon
        w = seen["w"]
        assert seen["issue"] == w + (w & 1) + 2, f"word visible at {w}, HALT issued at {seen['issue']}"
