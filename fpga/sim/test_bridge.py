"""The Basys 3 harness in simulation: the PC's UART frames reach the chip's host SPI port.

Each test drives RsRx bit by bit at 1 Mbaud (40 clk per bit at 40 MHz) and decodes RsTx, the way
the PC's USB-UART does on the board. The frame format is uart_spi_bridge.v's: 0xA5, a 16-bit
length LSB first, then the SPI bytes; one byte comes back per byte sent."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer

BIT = 40                     # clk per UART bit
ID = 0x5E                    # architecture §4: global register 0x00
SCRATCH = 0x0002


async def start(dut, sw=0):
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())   # 40 MHz
    dut.RsRx.value = 1
    dut.sw.value = sw
    dut.btnC.value = 1
    await ClockCycles(dut.clk, 10)
    dut.btnC.value = 0
    await ClockCycles(dut.clk, 20)


async def uart_send(dut, byte):
    for bit in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
        dut.RsRx.value = bit
        await ClockCycles(dut.clk, BIT)


async def uart_recv(dut, timeout=20000):
    """One byte from RsTx: wait for the start bit, sample mid-bit."""
    for _ in range(timeout):
        await ClockCycles(dut.clk, 1)
        if int(dut.RsTx.value) == 0:
            break
    else:
        raise AssertionError("no byte on RsTx")
    await ClockCycles(dut.clk, BIT // 2)
    v = 0
    for i in range(8):
        await ClockCycles(dut.clk, BIT)
        v |= int(dut.RsTx.value) << i
    await ClockCycles(dut.clk, BIT)
    assert int(dut.RsTx.value) == 1, "no stop bit"
    return v


async def spi(dut, data):
    """One bridge frame: returns the byte read back for each byte sent."""
    got = []

    async def reader():
        for _ in data:
            got.append(await uart_recv(dut))
    r = cocotb.start_soon(reader())
    for b in [0xA5, len(data) & 0xFF, len(data) >> 8] + list(data):
        await uart_send(dut, b)
    await r
    return got


async def read(dut, addr, n):
    got = await spi(dut, [0x80, addr >> 8, addr & 0xFF] + [0] * (n + 1))
    return got[4:]           # CMD, ADDR_H, ADDR_L, then the dummy byte


async def write(dut, addr, data):
    await spi(dut, [0x00, addr >> 8, addr & 0xFF] + list(data))


@cocotb.test()
async def test_id_through_the_bridge(dut):
    """Bring-up layer 2 (architecture §5): the ID register reads 0x5E through the bridge."""
    await start(dut)
    assert await read(dut, 0x0000, 1) == [ID]


@cocotb.test()
async def test_scratch_write_then_read(dut):
    """SCRATCH holds what the PC wrote, so writes reach the chip, not only reads."""
    await start(dut)
    for v in (0xA5, 0x3C):
        await write(dut, SCRATCH, [v])
        assert await read(dut, SCRATCH, 1) == [v]


@cocotb.test()
async def test_bytes_before_a_header_are_ignored(dut):
    """Noise on the line before 0xA5 doesn't start a frame or toggle CS."""
    await start(dut)
    for b in (0x00, 0xFF, 0x13):
        await uart_send(dut, b)
    assert int(dut.u_bridge.cs_n.value) == 1
    assert await read(dut, 0x0000, 1) == [ID]


@cocotb.test()
async def test_testmode_echoes_the_switches_on_the_protocol_pins(dut):
    """Bring-up layer 1: with the TESTMODE strap (sw[15]) up at reset and sw[14] = 1, uio
    outputs the registered ui_in, so the Pmod JB pins follow sw[5:0] and the strap bits."""
    sw = (1 << 15) | (1 << 14) | 0b101101
    await start(dut, sw)
    await ClockCycles(dut.clk, 10)
    assert int(dut.JB.value) == ((sw >> 8) & 0xC0) | (sw & 0x3F)


# ---- the later bring-up layers, as host/bringup.py runs them ----
PIN_CTRL, PINS, PIN_OUT, PIN_OE_SET, RUN = 0x0B, 0x0C, 0x0E, 0x0F, 0x04


@cocotb.test()
async def test_gpio_override_drives_and_reads_the_protocol_pins(dut):
    """Layer 3: PIN_CTRL.gpio_override with PIN_OE_SET and PIN_OUT drives Pmod JB, PINS reads it."""
    await start(dut)
    await write(dut, PIN_CTRL, [0b01])
    await write(dut, PIN_OE_SET, [0xFF])
    for v in (0x55, 0xA3):
        await write(dut, PIN_OUT, [v])
        await ClockCycles(dut.clk, 20)
        assert int(dut.JB.value) == v
        assert await read(dut, PINS, 1) == [v]


@cocotb.test()
async def test_program_sram_reads_back_what_was_written(dut):
    """Layer 4: the program SRAM (fpga/sram_fpga.v here) holds a pattern written over the bridge."""
    await start(dut)
    await write(dut, RUN, [0])
    data = [(37 * i + 11) & 0xFF for i in range(32)]
    await write(dut, 0x1000, data)
    assert await read(dut, 0x1000, len(data)) == data


@cocotb.test()
async def test_one_engine_runs_a_tiny_program(dut):
    """Layer 5: load an image, RUN engine 0 as P0, read r0-r2 back through REG_SEL/REG_DATA."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "toolchain"))
    from emu.compiler import compile_desc
    from emu.desc import Description
    d = Description("hello", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.li("r0", 0x1234)
    d.li("r1", 0x0FF0)
    d.alu("xor", "r2", "r0", "r1")
    d.halt()
    await start(dut)
    for addr, blob in compile_desc(d, check_timing=False).host_writes():
        await write(dut, addr, list(blob))
    await write(dut, 0x2000, [0])            # ROLE := P0
    await write(dut, RUN, [0b01])
    await ClockCycles(dut.clk, 200)
    (st,) = await read(dut, 0x2002, 1)
    assert st & 0b10, "engine 0 halted"
    regs = []
    for i in range(3):
        await write(dut, 0x2012, [i])
        lo, hi = await read(dut, 0x2014, 2)
        regs.append(lo | hi << 8)
    assert regs == [0x1234, 0x0FF0, 0x1234 ^ 0x0FF0]
