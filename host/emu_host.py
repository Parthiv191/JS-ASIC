"""Host driver for the protocol emulator: the chip's host SPI port (architecture §4), reached
through the Basys 3 harness's UART-to-SPI bridge (fpga/uart_spi_bridge.v).

    chip = Chip.open("COM5")          # pyserial, 1 Mbaud
    chip.read(0x0000, 1)              # [0x5E], the ID register
    chip.load(image); chip.set_role(0, Role.P0); chip.run(0b01)

The bridge frame is 0xA5, a 16-bit length LSB first, then the SPI bytes; it answers one byte per
byte. A host SPI frame is CMD (bit 7 read, bit 6 pair), ADDR_H, ADDR_L, then data; a read returns
one dummy byte first. Multi-byte registers are little-endian.
"""
from __future__ import annotations

import time

ENG0 = 0x2000
E_ROLE, E_STATUS, E_FIFO_COUNTS, E_PC, E_VIOL = 0x00, 0x02, 0x03, 0x04, 0x06
E_REG_SEL, E_REG_DATA, E_TXDATA, E_RXDATA = 0x12, 0x14, 0x30, 0x32
ID, VERSION, SCRATCH, STATUS, RUN, SOFT_RESET = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
CHUNK = 4096   # bytes per write frame; the address advances between frames
TX_DEPTH = 4   # TXDATA words per engine (architecture §4)


class Bridge:
    """One UART link to fpga/uart_spi_bridge.v. `port` is anything with write(bytes) and
    read(n) -> bytes, such as a pyserial Serial."""

    def __init__(self, port):
        self.port = port

    def spi(self, data: bytes | list[int]) -> bytes:
        data = bytes(data)
        if not 0 < len(data) <= 0xFFFF:
            raise ValueError(f"a bridge frame carries 1 to 65535 bytes, not {len(data)}")
        self.port.write(bytes([0xA5, len(data) & 0xFF, len(data) >> 8]) + data)
        got = self.port.read(len(data))
        if len(got) != len(data):
            raise TimeoutError(f"the bridge answered {len(got)} of {len(data)} bytes")
        return bytes(got)


class Chip:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    @classmethod
    def open(cls, device: str, baud: int = 1_000_000, timeout: float = 2.0) -> "Chip":
        import serial  # pyserial; only needed for a real port
        return cls(Bridge(serial.Serial(device, baud, timeout=timeout)))

    # ---- host SPI frames ----
    def write(self, addr: int, data: bytes | list[int], pair: bool = False) -> None:
        data = bytes(data)
        if pair:
            self.bridge.spi(bytes([0x40, addr >> 8, addr & 0xFF]) + data)
            return
        for i in range(0, len(data), CHUNK):
            a = addr + i
            self.bridge.spi(bytes([0x00, a >> 8, a & 0xFF]) + data[i:i + CHUNK])

    def read(self, addr: int, n: int, pair: bool = False) -> list[int]:
        got = self.bridge.spi(bytes([0xC0 if pair else 0x80, addr >> 8, addr & 0xFF]) + bytes(n + 1))
        return list(got[4:])   # CMD, ADDR_H, ADDR_L, then the dummy byte

    def read16(self, addr: int) -> int:
        lo, hi = self.read(addr, 2)
        return lo | hi << 8

    # ---- the chip ----
    def load(self, image) -> None:
        """An emu.image.Image: its tables and program (Image.host_writes), with the engines stopped."""
        self.write(RUN, [0])
        for addr, data in image.host_writes():
            self.write(addr, data)

    def set_role(self, engine: int, role: int) -> None:
        self.write(ENG0 + 0x100 * engine + E_ROLE, [int(role)])

    def run(self, mask: int) -> None:
        self.write(RUN, [mask])

    def stop(self) -> None:
        self.write(RUN, [0])

    def reg(self, engine: int, i: int) -> int:
        base = ENG0 + 0x100 * engine
        self.write(base + E_REG_SEL, [i])
        return self.read16(base + E_REG_DATA)

    def regs(self, engine: int) -> list[int]:
        return [self.reg(engine, i) for i in range(8)]

    def fifo_counts(self, engine: int) -> tuple[int, int]:
        """(TX words left, RX words waiting): TX in [2:0], RX in [7:4] (architecture §4)."""
        (b,) = self.read(ENG0 + 0x100 * engine + E_FIFO_COUNTS, 1)
        return b & 0x7, b >> 4

    def tx_push(self, engine: int, words: list[int]) -> None:
        data = b"".join(w.to_bytes(2, "little") for w in words)
        self.write(ENG0 + 0x100 * engine + E_TXDATA, data, pair=True)

    def tx_stream(self, engine: int, words: list[int], depth: int = TX_DEPTH,
                  timeout: float = 10.0) -> None:
        """Push `words` into TXDATA as the engine pops them, never past `depth` words in the FIFO
        (a push into a full FIFO is lost). TimeoutError if the engine stops popping."""
        words = list(words)
        t0 = time.monotonic()
        while words:
            room = depth - self.fifo_counts(engine)[0]
            if room > 0:
                self.tx_push(engine, words[:room])
                words = words[room:]
                t0 = time.monotonic()
            elif time.monotonic() - t0 > timeout:
                raise TimeoutError(f"engine {engine} stopped popping TX with {len(words)} words left")
            else:
                time.sleep(0.001)

    def rx_pop(self, engine: int, n: int) -> list[int]:
        b = self.read(ENG0 + 0x100 * engine + E_RXDATA, 2 * n, pair=True)
        return [b[2 * i] | b[2 * i + 1] << 8 for i in range(n)]

    def status(self, engine: int) -> dict[str, int]:
        base = ENG0 + 0x100 * engine
        (st,) = self.read(base + E_STATUS, 1)
        return {"running": st & 1, "halted": st >> 1 & 1, "pc": self.read16(base + E_PC),
                "viol": self.read16(base + E_VIOL)}
