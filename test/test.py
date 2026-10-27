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
