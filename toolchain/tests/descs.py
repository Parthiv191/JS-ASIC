"""Shared UART/SPI-shaped descriptions for tests (the protocol library lands in emu.protocols)."""
import math

from emu.desc import Description
from emu.isa import INF


def slack(nom, ppm):
    return math.ceil(2 * ppm * nom / 1e6) + 1


def uart_like(P, ppm=20000):
    d = Description("u", parties=("tx", "rx"), tolerance_ppm=ppm)
    line = d.line("line", owner="tx", idle=1)
    s = slack(P, ppm)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.abort_to("frame")
    d.label("frame")
    with d.only("tx"):
        d.pop("r0")
    d.cond("fall", line, owner="tx")
    d.hold(line, 0, owner="tx", prof=bit, abort=True)
    d.xfer(8, line, owner="tx", prof=bit, src="r0", dst="fifo", check=True)
    d.hold(line, 1, owner="tx", prof=bit, abort=True)
    d.br("frame")
    return d


def spi_like(H, ppm=20000):
    d = Description("s", parties=("ctrl", "tgt"), tolerance_ppm=ppm)
    sck = d.line("sck", owner="ctrl", idle=0)
    mosi = d.line("mosi", owner="ctrl", idle=0)
    miso = d.line("miso", owner="tgt", idle=1)
    cs = d.line("cs", owner="ctrl", idle=1)
    half = d.profile("HALF", nom=H, min=max(1, H - slack(H, ppm)), max=INF)
    su = d.profile("CS_SU", nom=2 * H)  # one SCK period; a 1H hold is too short to midpoint-sample
    hd = d.profile("CS_HD", nom=H, min=1, max=INF)
    out_l = d.lane("out", data=mosi, clk=sck)
    in_l = d.lane("in", data=miso, clk=sck)
    d.label("txn")
    with d.only("ctrl"):
        d.pop("r0")
    d.cond("fall", cs, owner="ctrl")
    d.hold(cs, 0, owner="ctrl", prof=su, abort=True)
    d.xfer(8, out_l, owner="ctrl", prof=half, src="r0", dst="fifo", msb_first=True, check=True)
    d.xfer(8, in_l, owner="tgt", prof=half, src="fifo", dst="fifo", msb_first=True, check=True)
    d.cond("rise", cs, owner="ctrl", prof=hd)
    d.br("txn")
    return d
