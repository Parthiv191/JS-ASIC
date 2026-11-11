"""Shared test helpers: pin constants, the SPI host, reset, and engine access."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

SCK, MOSI, CS_N = 1 << 0, 1 << 1, 1 << 2
TM_DIR, TM_STRAP = 1 << 6, 1 << 7

ID, VERSION = 0x5E, 0x01
STATUS_BOOT_DONE, STATUS_TESTMODE, STATUS_LOOPBACK = 1 << 0, 1 << 4, 1 << 5
UO_RESET = 0x50  # flash CS_N and REPORT high, the rest low (architecture §2)

PIN_CTRL, PINS, PIN_OE, PIN_OUT, PIN_OE_SET = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
OVERRIDE, LOOPBACK = 1 << 0, 1 << 1

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


# ---------------------------------------------------------------- engines
RUN, SOFT_RESET, IRQ, ERR_CLR = 0x0004, 0x0005, 0x0006, 0x0007
LINE = 0x0430
ENG0 = 0x2000
E_ROLE, E_STATUS, E_PC, E_VIOL, E_EVT, E_IRQ_ID = 0x00, 0x02, 0x04, 0x06, 0x08, 0x0A
E_FV_PC, E_FV_KIND, E_REG_SEL, E_REG_DATA, E_TB, E_NOW = 0x0C, 0x0E, 0x12, 0x14, 0x16, 0x1A
STATUS_HOST_ERR = 1 << 2
E_RUNNING, E_HALTED = 1 << 0, 1 << 1


def line_byte(pin, od=False, owner=2, idle=1, gated=False):
    """LINE[i]: pin[2:0] od[3] owner[5:4] idle[6] gated[7] (architecture §4)."""
    return pin | int(od) << 3 | owner << 4 | idle << 6 | int(gated) << 7


async def eng_read(host, off, n=1, eng=0):
    """An engine register, little-endian."""
    return int.from_bytes(bytes(await host.read(ENG0 + 0x100 * eng + off, n)), "little")


async def get_reg(host, i, eng=0):
    await host.write(ENG0 + 0x100 * eng + E_REG_SEL, [i])
    return await eng_read(host, E_REG_DATA, 2, eng)


async def set_reg(host, i, v, eng=0):
    await host.write(ENG0 + 0x100 * eng + E_REG_SEL, [i])
    await host.write(ENG0 + 0x100 * eng + E_REG_DATA, [v & 0xFF, v >> 8])


async def wait_halted(host, eng=0, tries=50):
    for _ in range(tries):
        if await eng_read(host, E_STATUS, 1, eng) & E_HALTED:
            return
    raise AssertionError("engine never halted")


async def run_program(host, words, role=0, lines=None):
    """Stop, load `words` at column 1 address 0, set the role (and LINE table), run."""
    await host.write(RUN, [0])
    await host.write_words(COL1, words)
    await host.write(ENG0 + E_ROLE, [role])
    if lines is not None:
        await host.write(LINE, list(lines))
    await host.write(RUN, [1])
