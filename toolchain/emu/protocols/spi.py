"""SPI, any mode (cpol, cpha), MSB first, full duplex, `words` bytes per CS frame.

The controller owns CS, SCK and MOSI; the peripheral owns MISO, a gated line (isa §2) that is
driven only while it is selected. CS is a SELECT bus (isa §4.7): CS falling is START, rising is
STOP. The controller's host pushes a frame token (any value) and then the frame's bytes; the
controller takes the token with CS high, so it never selects the peripheral without data, and a
POP stall ends at its own START, which the others wait for (isa §6 T5-stall). Each byte is a
duplex clocked XFER: both parties send from their TX FIFO and push (MISO << 8) | MOSI. A
peripheral with nothing to send sends ONES and logs UNDERRUN. CS rising mid-frame is a bus exit
for the peripheral, back to the frame start, where it waits for the next START. The peripheral
takes its TX word when a byte starts, so a frame cut short (or a CS pulse with no clocks) drops
that word: its later replies shift by one.
"""
from __future__ import annotations

from ..desc import Description
from ..isa import INF
from . import ProtocolSpec

TOKEN = 0


def build(half_cycles: int = 10, cpol: int = 0, cpha: int = 0, words: int = 1,
          tolerance_ppm: int = 20000) -> Description:
    H = half_cycles
    d = Description("spi", parties=("ctl", "per"), tolerance_ppm=tolerance_ppm)
    cs = d.line("cs", owner="ctl", idle=1)
    sck = d.line("sck", owner="ctl", idle=cpol)
    mosi = d.line("mosi", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    lane = d.lane("L", mosi, clk=sck, cpol=cpol, cpha=cpha, data2=miso)
    half = d.profile("HALF", nom=H, min=0, max=INF)
    wait = d.profile("WAIT", nom=H, min=0, max=INF)
    d.abort_to("frame")
    d.label("frame")
    with d.only("ctl"):
        d.pop("r7")  # the frame token
    d.cond("start", owner="ctl", prof=wait)
    for _ in range(words):
        d.xfer(8, lane, owner="ctl", prof=half, src="fifo", dst="fifo", msb_first=True, abort=True)
    d.cond("stop", owner="ctl", prof=wait)
    d.br("frame")
    return d


def payload(rng, frames: int, words: int = 1, **_) -> dict[str, list[int]]:
    ctl: list[int] = []
    for _ in range(frames):
        ctl += [TOKEN] + [rng.randrange(256) for _ in range(words)]
    return {"ctl": ctl, "per": [rng.randrange(256) for _ in range(frames * words)]}


SPEC = ProtocolSpec(
    name="spi",
    build=build,
    defaults=dict(half_cycles=10, cpol=0, cpha=0, words=1, tolerance_ppm=20000),
    space=dict(half_cycles=[10, 25], cpol=[0, 1], cpha=[0, 1], words=[1, 2, 4], tolerance_ppm=[0, 20000]),
    payload=payload,
)
