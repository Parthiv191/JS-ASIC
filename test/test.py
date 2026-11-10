"""Bring-up layers (architecture §5), through the pins only so the gate-level run uses
the same tests: 1 TESTMODE and pads, 2 host SPI with ID/SCRATCH, 3 GPIO override and
LOOPBACK, 4 SRAM through the host."""

import random

import cocotb
from cocotb.triggers import ClockCycles

from chip import (CS_N, ID, MOSI, PIN_CTRL, PIN_OE, PIN_OE_SET, PIN_OUT, PINS, SCK, STATUS_BOOT_DONE,
                  STATUS_LOOPBACK, STATUS_TESTMODE, TM_DIR, TM_STRAP, UO_RESET, VERSION, COL1, COL2, WORDS,
                  OVERRIDE, LOOPBACK, Host, reset)

# ---------------------------------------------------------------- layer 1: reset and TESTMODE

@cocotb.test()
async def test_reset_levels(dut):
    """Out of reset: uo_out at its idle levels, every uio an input."""
    await reset(dut)
    assert int(dut.uo_out.value) == UO_RESET, f"uo_out = {int(dut.uo_out.value):#04x}"
    assert int(dut.uio_oe.value) == 0
    dut.uio_in.value = 0xFF  # without the strap, uio_in never reaches uo_out
    await ClockCycles(dut.clk, 5)
    assert int(dut.uo_out.value) == UO_RESET


@cocotb.test()
async def test_testmode_drive(dut):
    """TESTMODE, ui_in[6] = 1: uio drives the registered ui_in (2 synchronizer flops)."""
    await reset(dut, TM_STRAP | TM_DIR | CS_N)
    assert int(dut.uio_oe.value) == 0xFF
    pats = [TM_DIR | (1 << i) for i in range(6)] + [0xFF, TM_DIR | 0x2A, TM_DIR | 0x15]
    for p in pats:
        dut.ui_in.value = p
        await ClockCycles(dut.clk, 2)  # cocotb reads pre-edge values: 2 flops show on the 3rd edge
        assert int(dut.uio_out.value) != p, "faster than 2 synchronizer flops"
        await ClockCycles(dut.clk, 1)
        assert int(dut.uio_out.value) == p, f"ui_in {p:#04x} -> uio_out {int(dut.uio_out.value):#04x}"


@cocotb.test()
async def test_testmode_show(dut):
    """TESTMODE, ui_in[6] = 0: uo_out shows the registered uio_in; uio stays an input."""
    await reset(dut, TM_STRAP | CS_N)
    for p in [1 << i for i in range(8)] + [0xFF, 0x00, 0xA5, 0x5A]:  # each differs from the last
        dut.uio_in.value = p
        await ClockCycles(dut.clk, 2)  # cocotb reads pre-edge values: 2 flops show on the 3rd edge
        assert int(dut.uo_out.value) != p, "faster than 2 synchronizer flops"
        await ClockCycles(dut.clk, 1)
        assert int(dut.uo_out.value) == p, f"uio_in {p:#04x} -> uo_out {int(dut.uo_out.value):#04x}"
        assert int(dut.uio_oe.value) == 0


@cocotb.test()
async def test_testmode_strap_sampled_at_reset(dut):
    """Raising ui_in[7] after reset doesn't enter TESTMODE."""
    await reset(dut)
    dut.ui_in.value = TM_STRAP | TM_DIR | CS_N
    await ClockCycles(dut.clk, 10)
    assert int(dut.uio_oe.value) == 0
    assert int(dut.uo_out.value) == UO_RESET


# ---------------------------------------------------------------- layer 2: host SPI

@cocotb.test()
async def test_spi_id(dut):
    """ID, VERSION, SCRATCH (reset 0) and STATUS in one auto-incrementing read."""
    await reset(dut)
    host = Host(dut)
    assert await host.read(0x0000, 4) == [ID, VERSION, 0x00, STATUS_BOOT_DONE]


@cocotb.test()
async def test_spi_scratch(dut):
    """SCRATCH holds every bit pattern; ID and VERSION ignore writes."""
    await reset(dut)
    host = Host(dut)
    for v in [0xFF, 0x00, 0xA5, 0x5A] + [1 << i for i in range(8)]:
        await host.write(0x0002, [v])
        assert await host.read(0x0002, 1) == [v], f"SCRATCH {v:#04x}"
    await host.write(0x0000, [0x00, 0x00, 0x3C])  # ID, VERSION, then SCRATCH
    assert await host.read(0x0000, 3) == [ID, VERSION, 0x3C]


@cocotb.test()
async def test_spi_status_testmode(dut):
    """STATUS.testmode follows the strap, and SPI still works in TESTMODE with ui_in[6] = 1."""
    await reset(dut, TM_STRAP | TM_DIR | CS_N)
    host = Host(dut)
    host.ui |= TM_STRAP | TM_DIR
    assert await host.read(0x0003, 1) == [STATUS_BOOT_DONE | STATUS_TESTMODE]


@cocotb.test()
async def test_spi_pair_mode(dut):
    """Pair mode toggles only address bit 0."""
    await reset(dut)
    host = Host(dut)
    assert await host.read(0x0000, 6, pair=True) == [ID, VERSION] * 3
    await host.write(0x0002, [0x11, 0x22, 0x33], pair=True)  # 02, 03 (STATUS, read-only), 02
    assert await host.read(0x0002, 1) == [0x33]


@cocotb.test()
async def test_spi_unmapped_reads_zero(dut):
    await reset(dut)
    host = Host(dut)
    assert await host.read(0x00FE, 4) == [0, 0, 0, 0]  # 0x00FE, 0x00FF, then 0x0100, 0x0101


@cocotb.test()
async def test_spi_cs_rise_resets_frame(dut):
    """A frame cut mid-byte leaves no state behind: the next frame decodes from its CMD."""
    await reset(dut)
    host = Host(dut)
    await host.write(0x0002, [0x77])
    # 3 bits of a CMD byte, then CS rises
    host._set(CS_N, 0)
    for bit in (1, 0, 1):
        host._set(MOSI, bit)
        await ClockCycles(dut.clk, 4)
        host._set(SCK, 1)
        await ClockCycles(dut.clk, 4)
        host._set(SCK, 0)
    host._set(CS_N, 1)
    await ClockCycles(dut.clk, 8)
    assert await host.read(0x0000, 3) == [ID, VERSION, 0x77]


@cocotb.test()
async def test_spi_speeds(dut):
    """SCRATCH round trips at SCK = clk/8 up to clk/16, random values."""
    await reset(dut)
    rng = random.Random(1)
    for half in (4, 5, 6, 8):
        host = Host(dut, half)
        for _ in range(4):
            v = rng.randrange(256)
            await host.write(0x0002, [v])
            assert await host.read(0x0000, 3) == [ID, VERSION, v], f"half={half} v={v:#04x}"


# ---------------------------------------------------------------- layer 3: GPIO override, LOOPBACK

async def set_pins(host, ctrl, out, oe):
    """PIN_CTRL, then PIN_OUT and PIN_OE_SET (PINS and PIN_OE are read-only and ignore the write)."""
    await host.write(PIN_CTRL, [ctrl, 0xFF, 0xFF, out, oe])
    await ClockCycles(host.dut.clk, 2)  # the drive flops follow one cycle after the registers


def check_pads(dut, out, oe):
    assert int(dut.uio_oe.value) == oe, f"uio_oe {int(dut.uio_oe.value):#04x}, want {oe:#04x}"
    got = int(dut.uio_out.value) & oe
    assert got == out & oe, f"uio_out {got:#04x} on driven pins, want {out & oe:#04x}"


@cocotb.test()
async def test_gpio_override_off(dut):
    """Without gpio_override, PIN_OUT and PIN_OE_SET drive nothing."""
    await reset(dut)
    host = Host(dut)
    await set_pins(host, 0, 0xA5, 0xFF)
    check_pads(dut, 0, 0)
    assert await host.read(PIN_CTRL, 3) == [0, 0, 0]  # PIN_CTRL, PINS (uio_in = 0), PIN_OE


@cocotb.test()
async def test_gpio_override_drives(dut):
    """Each pin as push-pull high, push-pull low and input; PIN_OE and PINS read back."""
    await reset(dut)
    host = Host(dut)
    rng = random.Random(3)
    cases = [(0xFF, 0xFF), (0x00, 0xFF), (0xA5, 0xF0), (0x5A, 0x0F)] + [(1 << i, 1 << i) for i in range(8)]
    cases += [(rng.randrange(256), rng.randrange(256)) for _ in range(6)]
    for out, oe in cases:
        ext = rng.randrange(256)
        dut.uio_in.value = (out & oe) | (ext & ~oe)  # the pad reads back its own drive
        await set_pins(host, OVERRIDE, out, oe)
        check_pads(dut, out, oe)
        pins = (out & oe) | (ext & ~oe)
        assert await host.read(PIN_CTRL, 3) == [OVERRIDE, pins, oe], f"out={out:#04x} oe={oe:#04x}"
    await set_pins(host, 0, 0xFF, 0xFF)  # clearing the override releases every pin
    check_pads(dut, 0, 0)


@cocotb.test()
async def test_gpio_drive_wins_over_pad(dut):
    """Inside the chip a driven pin reads its own drive, even if the pad disagrees."""
    await reset(dut)
    host = Host(dut)
    dut.uio_in.value = 0x0F
    await set_pins(host, OVERRIDE, 0xF0, 0xFF)
    assert await host.read(PINS, 1) == [0xF0]


@cocotb.test()
async def test_loopback(dut):
    """LOOPBACK: no pin is driven; driven lines read their drive, undriven lines read 1."""
    await reset(dut)
    host = Host(dut)
    dut.uio_in.value = 0x00  # ignored in LOOPBACK
    await set_pins(host, OVERRIDE | LOOPBACK, 0x30, 0x3C)
    check_pads(dut, 0, 0)
    pins = 0x30 | ~0x3C & 0xFF  # 0x30 driven high, 0x0C driven low, the rest released
    assert await host.read(PIN_CTRL, 3) == [OVERRIDE | LOOPBACK, pins, 0x3C]
    assert (await host.read(0x0003, 1))[0] & STATUS_LOOPBACK
    await set_pins(host, OVERRIDE, 0x30, 0x3C)  # leaving LOOPBACK drives the pads again
    check_pads(dut, 0x30, 0x3C)
    assert not (await host.read(0x0003, 1))[0] & STATUS_LOOPBACK


# ---------------------------------------------------------------- layer 4: SRAM through the host

@cocotb.test()
async def test_sram_roundtrip(dut):
    """Every data and address bit, in both columns, keeps its own data (little-endian words)."""
    await reset(dut)
    host = Host(dut)
    words = {0x000: 0x12345678, 0x001: 0xFFFF0000, 0x080: 0x0000FFFF, 0x100: 0xA5A55A5A,
             WORDS - 1: 0xDEADBEEF, WORDS // 2 - 1: 0x0F0F0F0F}
    words |= {1 << i: 0x01010101 << (i % 8) for i in range(9)}
    for col, base in enumerate((COL1, COL2)):
        for a, w in words.items():
            await host.write_words(base + 4 * a, [w ^ (0xFFFFFFFF * col)])
    for col, base in enumerate((COL1, COL2)):
        for a, w in words.items():
            want = w ^ (0xFFFFFFFF * col)
            got = (await host.read_words(base + 4 * a, 1))[0]
            assert got == want, f"col{col + 1}[{a:#05x}] = {got:#010x}, wrote {want:#010x}"
    assert await host.read(COL1, 4) == [0x78, 0x56, 0x34, 0x12]  # little-endian bytes


@cocotb.test()
async def test_sram_burst(dut):
    """One frame writes 16 consecutive words; one frame reads them back (read-ahead across words)."""
    await reset(dut)
    host = Host(dut)
    rng = random.Random(2)
    words = [rng.getrandbits(32) for _ in range(16)]
    await host.write_words(COL1 + 4 * 300, words)
    assert await host.read_words(COL1 + 4 * 300, 16) == words


@cocotb.test()
async def test_sram_partial_word_discarded(dut):
    """Fewer than 4 bytes of a word, then a CS rise: the word is unchanged."""
    await reset(dut)
    host = Host(dut)
    await host.write_words(COL1 + 4 * 5, [0x11223344])
    await host.write(COL1 + 4 * 5, [0xAA, 0xBB, 0xCC])
    await host.write(COL1 + 4 * 5 + 3, [0xDD])  # the 4th byte alone doesn't commit either
    assert await host.read_words(COL1 + 4 * 5, 1) == [0x11223344]
