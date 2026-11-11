"""The host driver against a fake bridge: a serial port that speaks uart_spi_bridge.v's framing and
the host SPI protocol (architecture §4) over a byte-addressed register file. Run:
    cd host && python -m pytest -q
"""
import pytest

from emu_host import ENG0, E_REG_DATA, E_REG_SEL, E_RXDATA, E_TXDATA, ID, Bridge, Chip


class FakeBridgePort:
    """Parses bridge frames; answers each SPI byte as the chip would (MISO is the byte read).
    Reads return a dummy byte first; the address auto-increments, except in pair mode, where it
    toggles bit 0 only. TXDATA/RXDATA are FIFOs reached in pair mode."""

    def __init__(self):
        self.mem = {ID: 0x5E}
        self.frames = []
        self.reply = b""
        self.tx = []                 # words pushed into TXDATA (engine 0)
        self.rx = []                 # words waiting in RXDATA (engine 0)
        self.reg = [0] * 8

    def write(self, b):
        assert b[0] == 0xA5, "every bridge frame starts with 0xA5"
        n = b[1] | b[2] << 8
        data = b[3:]
        assert len(data) == n, "the length field counts the SPI bytes"
        self.frames.append(bytes(data))
        self.reply += bytes(self._spi(data))

    def read(self, n):
        out, self.reply = self.reply[:n], self.reply[n:]
        return out

    def _spi(self, data):
        cmd, addr = data[0], data[1] << 8 | data[2]
        rd, pair = cmd & 0x80, cmd & 0x40
        out = [0, 0, 0]
        if rd:
            out.append(0)            # the dummy byte
            body = data[4:]
        else:
            body = data[3:]
        a = addr
        word = []
        for b in body:
            base = a & ~1
            if rd:
                if base == ENG0 + E_RXDATA:
                    if not word:
                        w = self.rx.pop(0) if self.rx else 0
                        word = [w & 0xFF, w >> 8]
                    out.append(word.pop(0))
                elif base == ENG0 + E_REG_DATA:
                    out.append((self.reg[self.mem.get(ENG0 + E_REG_SEL, 0)] >> (8 * (a & 1))) & 0xFF)
                else:
                    out.append(self.mem.get(a, 0))
            else:
                if base == ENG0 + E_TXDATA:
                    word.append(b)
                    if len(word) == 2:
                        self.tx.append(word[0] | word[1] << 8)
                        word = []
                else:
                    self.mem[a] = b
                out.append(0)
            a = (a ^ 1) if pair else a + 1
        return out


@pytest.fixture
def chip():
    return Chip(Bridge(FakeBridgePort()))


def test_a_bridge_frame_is_a5_length_then_data():
    port = FakeBridgePort()
    assert Bridge(port).spi([0x80, 0, 0, 0, 0]) == bytes([0, 0, 0, 0, 0x5E])
    with pytest.raises(ValueError):
        Bridge(port).spi(b"")


def test_read_drops_the_command_address_and_dummy_bytes(chip):
    assert chip.read(ID, 1) == [0x5E]


def test_write_then_read_scratch(chip):
    chip.write(0x0002, [0x3C])
    assert chip.read(0x0002, 1) == [0x3C]


def test_a_long_write_is_split_and_the_address_advances(chip, monkeypatch):
    import emu_host
    monkeypatch.setattr(emu_host, "CHUNK", 512)     # the program SRAM is 2 KB: stay inside it
    data = bytes(range(256)) * 5                     # 1280 bytes: three frames
    chip.write(0x1000, data)
    frames = chip.bridge.port.frames
    assert [f[1] << 8 | f[2] for f in frames] == [0x1000, 0x1200, 0x1400]
    assert bytes(chip.bridge.port.mem[0x1000 + i] for i in range(len(data))) == data


def test_tx_push_and_rx_pop_use_pair_mode_words(chip):
    chip.tx_push(0, [0x1234, 0xBEEF])
    assert chip.bridge.port.tx == [0x1234, 0xBEEF]
    assert chip.bridge.port.frames[-1][0] == 0x40
    chip.bridge.port.rx = [0x00A4, 0x0055]
    assert chip.rx_pop(0, 2) == [0x00A4, 0x0055]


def test_reg_reads_through_the_register_window(chip):
    chip.bridge.port.reg = [i * 0x101 for i in range(8)]
    assert chip.regs(0) == [i * 0x101 for i in range(8)]


def test_load_stops_the_engines_then_writes_the_image():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolchain"))
    from emu.protocols import get
    from emu.compiler import compile_desc
    img = compile_desc(get("uart").build())
    port = FakeBridgePort()
    Chip(Bridge(port)).load(img)
    assert port.frames[0] == bytes([0x00, 0x00, 0x04, 0x00])   # RUN := 0 first
    for addr, data in img.host_writes():
        assert bytes(port.mem.get(addr + i, 0) for i in range(len(data))) == data
