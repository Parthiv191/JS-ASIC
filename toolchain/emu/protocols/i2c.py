"""I2C controller write: START, the 7-bit address with R/W = 0, `data_bytes` bytes, STOP.

SDA is a shared OD line and SCL an OD line the controller owns; the bus is I2C (isa §4.7) with a
cpol 1, cpha 1 lane on them. Each bit is a clocked XFER of half period H, MSB first. SDA moves
`ldelay` cycles after SCL falls (the data hold time; isa §2 lane config), never with it.

- The controller's host pushes the address byte (address << 1), then the data bytes. The
  controller takes the address with SCL and SDA high, so a POP stall ends at its own START, which
  the others wait for (isa §6 T5-stall); the data bytes come straight from its TX FIFO.
- The target ACKs only its own address: a lead block for the target (isa §5 R3-lead) compares
  the address it received and either falls through into the ACK bit, which it drives low, or
  waits for the STOP and restarts at the frame start.
- The controller reads the ACK: a lead block for the controller ends a NACKed transaction with
  a STOP; the target, if it ACKed, follows the next data byte speculatively and the STOP makes
  that a bus exit.
- Every data byte is ACKed by the target. The target and a monitor push each data byte into RX;
  the controller drives SDA, the lane's only data line, so it pushes nothing (isa §4.3).
- The STOP: SCL low (the target lets SDA go, isa §4.3 let-go), SDA low, SCL high, then SDA rises
  while SCL is high.
"""
from __future__ import annotations

from ..desc import Description
from ..isa import INF
from . import ProtocolSpec


def build(half_cycles: int = 20, data_bytes: int = 1, address: int = 0x5A, ldelay: int = 2,
          tolerance_ppm: int = 20000) -> Description:
    H = half_cycles
    d = Description("i2c", parties=("ctl", "tgt"), tolerance_ppm=tolerance_ppm)
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=ldelay)
    half = d.profile("HALF", nom=H, min=0, max=INF)
    wait = d.profile("WAIT", nom=H, min=0, max=INF)
    hold = d.profile("HOLD", nom=H)

    def stop() -> None:
        d.cond("fall", scl, owner="ctl", prof=wait)
        d.hold(sda, 0, owner="ctl", prof=hold)
        d.cond("rise", scl, owner="ctl", prof=wait)
        d.cond("stop", owner="ctl", prof=wait)

    d.abort_to("frame")
    d.label("frame")
    with d.only("ctl"):
        d.pop("r1")  # the address byte
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="r1", dst="r2", msb_first=True)
    with d.lead("tgt"):
        d.alu("ne", "r3", "r2", address << 1)
        d.br("nack", cond="nz", reg="r3")
        d.br("ack")
        d.label("nack")
        d.cond("stop", owner="ctl", prof=wait)  # not addressed: wait for the STOP
        d.br("frame")
    d.label("ack")
    d.xfer(1, lane, owner="tgt", prof=half, src="zero", dst="r4", abort=True)
    with d.lead("ctl"):
        d.br("data", cond="z", reg="r4")
        stop()                                  # NACK: end the transaction
        d.br("frame")
    d.label("data")
    for _ in range(data_bytes):
        d.xfer(8, lane, owner="ctl", prof=half, src="fifo", dst="fifo", msb_first=True, abort=True)
        d.xfer(1, lane, owner="tgt", prof=half, src="zero", abort=True)
    stop()
    d.br("frame")
    return d


def payload(rng, n: int, data_bytes: int = 1, address: int = 0x5A, **_) -> dict[str, list[int]]:
    ctl: list[int] = []
    for _ in range(n):
        ctl += [address << 1] + [rng.randrange(256) for _ in range(data_bytes)]
    return {"ctl": ctl, "tgt": []}


SPEC = ProtocolSpec(
    name="i2c",
    build=build,
    defaults=dict(half_cycles=20, data_bytes=1, address=0x5A, ldelay=2, tolerance_ppm=20000),
    # 250 is 100 kHz at 50 MHz; ldelay 15 is 300 ns there
    space=dict(half_cycles=[20, 250], data_bytes=[1, 2, 4], address=[0x08, 0x5A, 0x77], ldelay=[1, 2, 15],
               tolerance_ppm=[0, 20000]),
    payload=payload,
)
