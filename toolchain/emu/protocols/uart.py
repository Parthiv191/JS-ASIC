"""UART 8N1 (or 5..9 data bits): one start bit, LSB first, one stop bit.

The transmitter owns the line; the receiver and a monitor follow it. The start edge is an
unbounded COND, so idle time between frames is free, and a framing error (low stop bit,
or a short start bit) aborts back to the frame start to wait for the next start edge.
"""
from __future__ import annotations

from ..desc import Description
from . import ProtocolSpec, slack


def build(bit_cycles: int = 20, data_bits: int = 8, tolerance_ppm: int = 20000) -> Description:
    P = bit_cycles
    s = slack(P, tolerance_ppm)
    d = Description("uart", parties=("tx", "rx"), tolerance_ppm=tolerance_ppm)
    line = d.line("line", owner="tx", idle=1)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.abort_to("frame")
    d.label("frame")
    with d.only("tx"):
        d.pop("r0")
    d.cond("fall", line, owner="tx")
    d.hold(line, 0, owner="tx", prof=bit, abort=True)
    d.xfer(data_bits, line, owner="tx", prof=bit, src="r0", dst="fifo", check=True)
    d.hold(line, 1, owner="tx", prof=bit, abort=True)
    d.br("frame")
    return d


def payload(rng, n: int, data_bits: int = 8, **_) -> dict[str, list[int]]:
    return {"tx": [rng.randrange(1 << data_bits) for _ in range(n)]}


SPEC = ProtocolSpec(
    name="uart",
    build=build,
    # At 2% the compiler needs P >= 19 (isa §6 T5-watch: the receiver's BR and ROLE_IF before the
    # next start edge). 434 is 115200 baud at 50 MHz.
    defaults=dict(bit_cycles=20, data_bits=8, tolerance_ppm=20000),
    space=dict(bit_cycles=[19, 20, 32, 434], data_bits=[5, 6, 7, 8], tolerance_ppm=[0, 5000, 20000]),
    payload=payload,
)
