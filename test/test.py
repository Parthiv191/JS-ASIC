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
# uio_in[5] CHASE (level): read mem[rdata[7:0]] every cycle. uio_in[6] VIEW (level):
# uo_out shows byte ui_in[1:0] of the last read word instead of TX/busy.
ADDR, DATA, WRITE, READ, CHASE, VIEW = (1 << i for i in range(1, 7))


async def strobe(dut, bit, byte=0):
    dut.ui_in.value = byte
    dut.uio_in.value = bit
    await ClockCycles(dut.clk, 4)
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 4)


async def mem_write(dut, addr, word):
    await strobe(dut, ADDR, addr)
    for k in (3, 2, 1, 0):
        await strobe(dut, DATA, (word >> (8 * k)) & 0xFF)
    await strobe(dut, WRITE)


async def view_word(dut):
    word = 0
    for k in range(4):
        dut.ui_in.value = k
        dut.uio_in.value = VIEW
        await ClockCycles(dut.clk, 4)
        word |= int(dut.uo_out.value) << (8 * k)
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 4)
    return word


async def mem_read(dut, addr):
    await strobe(dut, ADDR, addr)
    await strobe(dut, READ)
    return await view_word(dut)


@cocotb.test()
async def test_mem_word_roundtrip(dut):
    """Both 16-bit halves (one macro each) keep their own data at every address bit."""
    await reset(dut)
    words = {0x00: 0x12345678, 0x01: 0xFFFF0000, 0x80: 0x0000FFFF, 0xFF: 0xDEADBEEF, 0x55: 0xA5A55A5A}
    for a, w in words.items():
        await mem_write(dut, a, w)
    for a, w in words.items():
        got = await mem_read(dut, a)
        assert got == w, f"mem[{a:#04x}] = {got:#010x}, wrote {w:#010x}"


@cocotb.test()
async def test_mem_chase_follows_pointers(dut):
    """CHASE feeds the macro output straight back into its address: the engine's fetch-ahead path."""
    await reset(dut)
    chain = {0x10: 0x20, 0x20: 0x30, 0x30: 0xF0, 0xF0: 0xF0}  # ends in a self-loop at 0xF0
    for a, nxt in chain.items():
        await mem_write(dut, a, 0xC0DE0000 | (a << 8) | nxt)
    await mem_read(dut, 0x10)  # prime: rdata = mem[0x10]
    dut.uio_in.value = CHASE
    await ClockCycles(dut.clk, 10)  # synchronizer + 3 hops, then it sits on the self-loop
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 4)
    got = await view_word(dut)  # rdata holds the last word read
    assert got == 0xC0DEF0F0, f"chase ended on {got:#010x}, expected the self-loop word 0xc0def0f0"


@cocotb.test()
async def test_uart_still_works_with_mem_view_off(dut):
    await reset(dut)
    await mem_write(dut, 0x00, 0xFFFFFFFF)
    await send(dut, 0x5A)
    assert await receive_byte(dut) == 0x5A
