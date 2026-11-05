import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLKS_PER_BIT = 434  # must match project.v


def tx(dut):
    return int(dut.uo_out.value) & 1


def busy(dut):
    return (int(dut.uo_out.value) >> 1) & 1


async def reset(dut):
    # 50 MHz clock
    clock = Clock(dut.clk, 20, unit="ns")
    cocotb.start_soon(clock.start())

    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)


async def send(dut, byte):
    dut.ui_in.value = byte
    dut.uio_in.value = 1
    await ClockCycles(dut.clk, 5)
    dut.uio_in.value = 0


async def receive_byte(dut):
    """Act like the receiving end: watch the TX pin and decode one byte."""
    # wait for the start bit (line goes low)
    for _ in range(CLKS_PER_BIT * 20):
        await RisingEdge(dut.clk)
        if tx(dut) == 0:
            break
    else:
        assert False, "never saw a start bit"

    # go to the middle of the start bit and make sure it's still low
    await ClockCycles(dut.clk, CLKS_PER_BIT // 2)
    assert tx(dut) == 0, "start bit glitch"

    # sample each data bit in the middle, LSB first
    byte = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT)
        byte |= tx(dut) << i

    await ClockCycles(dut.clk, CLKS_PER_BIT)
    assert tx(dut) == 1, "stop bit should be high (framing error)"
    return byte


@cocotb.test()
async def test_idle_high(dut):
    await reset(dut)
    assert tx(dut) == 1, "TX should idle high"
    assert busy(dut) == 0


@cocotb.test()
async def test_send_one_byte(dut):
    await reset(dut)
    await send(dut, 0x41)  # 'A'
    got = await receive_byte(dut)
    assert got == 0x41, f"sent 0x41, got {got:#04x}"


@cocotb.test()
async def test_send_several_bytes(dut):
    await reset(dut)
    for b in [0x00, 0xFF, 0x55, 0xA5, ord("h"), ord("i")]:
        await send(dut, b)
        got = await receive_byte(dut)
        assert got == b, f"sent {b:#04x}, got {got:#04x}"
        # wait for the stop bit to finish before sending again
        while busy(dut):
            await RisingEdge(dut.clk)


# ---------------------------------------------------------------- SRAM trial harness
# uio_in[1] ADDR, [2] DATA, [3] WRITE, [4] READ: rising-edge strobes, ui_in holds the byte.
# ADDR shifts a byte into the address, so send the high byte first. uio_in[5] CHASE
# (level): read mem[rdata[AW-1:0]] every cycle. uio_in[6] VIEW (level): uo_out shows byte
# ui_in[1:0] of the last read word (ui_in[2] = 1: the flop memory, if built). uio_in[7] SEL
# (level): 0 = program memory u_mem, 1 = fuzzer memory u_fmem.
ADDR, DATA, WRITE, READ, CHASE, VIEW, SEL = (1 << i for i in range(1, 8))
AW = 10  # must match project.v
DEPTH = 1 << AW


async def strobe(dut, bit, byte=0, sel=0):
    dut.ui_in.value = byte
    dut.uio_in.value = bit | (SEL if sel else 0)
    await ClockCycles(dut.clk, 4)
    dut.uio_in.value = SEL if sel else 0
    await ClockCycles(dut.clk, 4)


async def set_addr(dut, addr, sel):
    await strobe(dut, ADDR, (addr >> 8) & 0xFF, sel)
    await strobe(dut, ADDR, addr & 0xFF, sel)


async def mem_write(dut, addr, word, sel=0):
    await set_addr(dut, addr, sel)
    for k in (3, 2, 1, 0):
        await strobe(dut, DATA, (word >> (8 * k)) & 0xFF, sel)
    await strobe(dut, WRITE, 0, sel)


async def view_word(dut, sel=0, flop=False):
    word = 0
    for k in range(4):
        dut.ui_in.value = k | (4 if flop else 0)
        dut.uio_in.value = VIEW | (SEL if sel else 0)
        await ClockCycles(dut.clk, 4)
        word |= int(dut.uo_out.value) << (8 * k)
    dut.uio_in.value = SEL if sel else 0
    await ClockCycles(dut.clk, 4)
    return word


async def mem_read(dut, addr, sel=0, flop=False):
    await set_addr(dut, addr, sel)
    await strobe(dut, READ, 0, sel)
    return await view_word(dut, sel, flop)


def flop_built(dut):
    try:
        dut.user_project.rdata_f  # only exists in RTL simulation with FLOP128
        return hasattr(dut.user_project, "fmem")
    except AttributeError:
        return False


@cocotb.test()
async def test_mem_word_roundtrip(dut):
    """Every data bit and every address bit, in both macros, keeps its own data."""
    await reset(dut)
    words = {0x000: 0x12345678, 0x001: 0xFFFF0000, 0x080: 0x0000FFFF, 0x100: 0xA5A55A5A,
             DEPTH - 1: 0xDEADBEEF, DEPTH // 2 - 1: 0x0F0F0F0F}
    for sel in (0, 1):
        for a, w in words.items():
            await mem_write(dut, a, w ^ (0xFFFFFFFF * sel), sel)
    for sel in (0, 1):
        for a, w in words.items():
            want = w ^ (0xFFFFFFFF * sel)
            got = await mem_read(dut, a, sel)
            assert got == want, f"mem{sel}[{a:#05x}] = {got:#010x}, wrote {want:#010x}"


@cocotb.test()
async def test_mem_chase_follows_pointers(dut):
    """CHASE feeds the macro output straight back into its address: the engine's fetch-ahead path."""
    await reset(dut)
    chain = {0x010: 0x120, 0x120: 0x030, 0x030: DEPTH - 2, DEPTH - 2: DEPTH - 2}  # ends in a self-loop
    for sel in (0, 1):
        for a, nxt in chain.items():
            await mem_write(dut, a, 0xC0000000 | (a << 16) | nxt, sel)
        await mem_read(dut, 0x010, sel)  # prime: rdata = mem[0x010]
        dut.uio_in.value = CHASE | (SEL if sel else 0)
        await ClockCycles(dut.clk, 12)  # synchronizer + 3 hops, then it sits on the self-loop
        dut.uio_in.value = SEL if sel else 0
        await ClockCycles(dut.clk, 4)
        got = await view_word(dut, sel)
        want = 0xC0000000 | ((DEPTH - 2) << 16) | (DEPTH - 2)
        assert got == want, f"mem{sel}: chase ended on {got:#010x}, expected {want:#010x}"


@cocotb.test()
async def test_flop_memory_if_built(dut):
    """FLOP128 build only: the 128-word flop memory written and read alongside u_fmem."""
    await reset(dut)
    if not flop_built(dut):
        return
    for a, w in {0: 0x01234567, 127: 0x89ABCDEF, 64: 0x5A5A5A5A}.items():
        await mem_write(dut, a, w, sel=1)
        assert await mem_read(dut, a, sel=1, flop=True) == w


@cocotb.test()
async def test_uart_still_works_with_mem_view_off(dut):
    await reset(dut)
    await mem_write(dut, 0x00, 0xFFFFFFFF)
    await send(dut, 0x5A)
    assert await receive_byte(dut) == 0x5A
