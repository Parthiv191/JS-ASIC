"""Bring-up layers (architecture §5), through the pins only so the gate-level run uses
the same tests: 1 TESTMODE and pads, 2 host SPI with ID/SCRATCH, 4 SRAM through the host."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

SCK, MOSI, CS_N = 1 << 0, 1 << 1, 1 << 2
TM_DIR, TM_STRAP = 1 << 6, 1 << 7

ID, VERSION = 0x5E, 0x01
STATUS_BOOT_DONE, STATUS_TESTMODE = 1 << 0, 1 << 4
UO_RESET = 0x50  # flash CS_N and REPORT high, the rest low (architecture §2)

COL1, COL2 = 0x1000, 0x3000
WORDS = 512


class Host:
    """Bit-banged SPI host, mode 0. `half` is the SCK half period in clk (>= 4: SCK <= clk/8)."""

    def __init__(self, dut, half=4):
        self.dut, self.half = dut, half
        self.ui = CS_N

    def _set(self, mask, on):
        self.ui = (self.ui | mask) if on else (self.ui & ~mask)
        self.dut.ui_in.value = self.ui

    async def frame(self, out, n_in=0):
        """Send `out`, then clock `n_in` more bytes. Returns every byte read back."""
        clk, h = self.dut.clk, self.half
        got = []
        self._set(CS_N, 0)
        await ClockCycles(clk, h)
        for b in list(out) + [0] * n_in:
            r = 0
            for i in range(7, -1, -1):
                self._set(MOSI, (b >> i) & 1)
                await ClockCycles(clk, h)
                r = (r << 1) | (int(self.dut.uo_out.value) & 1)  # sample MISO at the rise
                self._set(SCK, 1)
                await ClockCycles(clk, h)
                self._set(SCK, 0)
            got.append(r)
        await ClockCycles(clk, h)
        self._set(CS_N, 1)
        await ClockCycles(clk, 6)  # CS high >= 4 clk between frames
        return got

    async def write(self, addr, data, pair=False):
        await self.frame([0x40 if pair else 0x00, addr >> 8, addr & 0xFF] + list(data))

    async def read(self, addr, n, pair=False):
        got = await self.frame([0xC0 if pair else 0x80, addr >> 8, addr & 0xFF], n + 1)
        return got[4:]  # drop CMD, ADDR_H, ADDR_L and the dummy byte

    async def write_words(self, addr, words):
        await self.write(addr, b"".join(w.to_bytes(4, "little") for w in words))

    async def read_words(self, addr, n):
        b = bytes(await self.read(addr, 4 * n))
        return [int.from_bytes(b[4 * i:4 * i + 4], "little") for i in range(n)]


async def reset(dut, ui=CS_N):
    """Reset with `ui` on ui_in (the straps are sampled as reset releases)."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())  # 50 MHz
    dut.ena.value = 1
    dut.ui_in.value = ui
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 10)


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
