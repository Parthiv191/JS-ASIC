"""I2C register read: START, the address with R/W = 0, the register number, a repeated START, the
address with R/W = 1, `data_bytes` bytes from the target, STOP. A sensor's WHO_AM_I read and an
EEPROM's random read look like this; MCU drivers send it with a repeated START (demo steps 8, 9).

The lines, lane and bus are the write's (`i2c.py`): SDA a shared OD line, SCL an OD line the
controller owns, a cpol 1, cpha 1 lane with `ldelay`, bits of half period H, MSB first.

- The controller's host pushes, per transaction, the address byte with W (address << 1), the
  register number, and the address byte with R (address << 1 | 1). The controller takes the first
  with SCL and SDA high, so a POP stall ends at its own START, as in the write. The other two come
  straight from its TX FIFO, in XFERs it clocks.
- The target serves `values`, compiled into the image: on I2C the target doesn't clock, so a POP
  could leave it late for the controller's clock (isa §6 T5-stall). It ACKs only its own
  address, with W and then with R, in a lead block like the write's, and pushes the register
  number into RX for its host.
- The controller reads each address's ACK in a lead block. On a NACK it drops what is left of the
  transaction from TX, pushes NACK (0x100, above any byte) into RX, and STOPs. Otherwise it pushes
  the bytes it reads into RX, ACKs each but the last, and NACKs the last, as the I2C spec asks.
- The repeated START: SCL low (the target lets SDA go, isa §4.3 let-go), SCL high with SDA high,
  then SDA falls while SCL is high. A monitor pushes the register number and the bytes.
"""
from __future__ import annotations

from ..desc import Description
from ..isa import INF
from . import ProtocolSpec

NACK = 0x100
DEFAULT_VALUES = (0x68, 0xA5, 0x3C, 0x01)      # 0x68: a common WHO_AM_I answer
VALUE_REGS = ("r5", "r6", "r7", "r0")


def values_for(data_bytes: int = 1, values: tuple[int, ...] | None = None, **_) -> list[int]:
    vals = list(DEFAULT_VALUES[:data_bytes] if values is None else values)
    if len(vals) != data_bytes or not 1 <= data_bytes <= len(VALUE_REGS) or any(not 0 <= v < 256 for v in vals):
        raise ValueError(f"need 1..{len(VALUE_REGS)} values, one byte each, for data_bytes = {data_bytes}")
    return vals


def build(half_cycles: int = 20, data_bytes: int = 1, address: int = 0x5A, ldelay: int = 12,
          tolerance_ppm: int = 20000, values: tuple[int, ...] | None = None) -> Description:
    H = half_cycles
    vals = values_for(data_bytes, values)
    d = Description("i2c_reg", parties=("ctl", "tgt"), tolerance_ppm=tolerance_ppm)
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

    def target_acks(want: int, label: str) -> None:
        """The target ACKs `want` (in r2): otherwise it waits for the STOP and starts over."""
        with d.lead("tgt"):
            d.alu("ne", "r3", "r2", want)
            d.br(f"{label}_no", cond="nz", reg="r3")
            d.br(label)
            d.label(f"{label}_no")
            d.cond("stop", owner="ctl", prof=wait)
            d.br("frame")
        d.label(label)
        d.xfer(1, lane, owner="tgt", prof=half, src="zero", dst="r4", abort=True)

    def controller_reads_ack(drop: int, label: str) -> None:
        """On a NACK the controller drops `drop` TX words, pushes NACK and STOPs."""
        with d.lead("ctl"):
            d.br(label, cond="z", reg="r4")
            for _ in range(drop):
                d.pop("r6")
            d.li("r6", NACK)
            d.push("r6")
            stop()
            d.br("frame")
        d.label(label)

    with d.only("tgt"):
        for reg, v in zip(VALUE_REGS, vals):
            d.li(reg, v)
    d.abort_to("frame")
    d.label("frame")
    with d.only("ctl"):
        d.pop("r1")  # the address byte with W
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="r1", dst="r2", msb_first=True, abort=True)
    target_acks(address << 1, "ack_w")
    controller_reads_ack(2, "reg")
    d.xfer(8, lane, owner="ctl", prof=half, src="fifo", dst="fifo", msb_first=True, abort=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="zero", abort=True)
    # the repeated START
    d.cond("fall", scl, owner="ctl", prof=wait)
    d.cond("rise", scl, owner="ctl", prof=wait)
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="fifo", dst="r2", msb_first=True, abort=True)
    target_acks(address << 1 | 1, "ack_r")
    controller_reads_ack(0, "data")
    for i, reg in enumerate(VALUE_REGS[:data_bytes]):
        d.xfer(8, lane, owner="tgt", prof=half, src=reg, dst="fifo", msb_first=True, abort=True)
        d.xfer(1, lane, owner="ctl", prof=half, src="ones" if i == data_bytes - 1 else "zero", abort=True)
    stop()
    d.br("frame")
    return d


def payload(rng, n: int, address: int = 0x5A, **_) -> dict[str, list[int]]:
    ctl: list[int] = []
    for _ in range(n):
        ctl += [address << 1, rng.randrange(256), address << 1 | 1]
    return {"ctl": ctl, "tgt": []}


SPEC = ProtocolSpec(
    name="i2c_reg",
    build=build,
    defaults=dict(half_cycles=20, data_bytes=1, address=0x5A, ldelay=12, tolerance_ppm=20000),
    # 200 is 100 kHz at 40 MHz and 52 is 385 kHz; ldelay 12 is 300 ns there
    space=dict(half_cycles=[20, 52, 200], data_bytes=[1, 2, 4], address=[0x08, 0x50, 0x68], ldelay=[1, 2, 12, 15],
               tolerance_ppm=[0, 20000]),
    payload=payload,
)
