"""Image header and host table layout (isa.md §2, architecture.md §4).

Golden bytes are written out by hand from the architecture §4 byte map, with every field of a
slot set non-zero, so a field in the wrong place or overlapping another changes the byte.
"""
import pytest

from emu.image import (
    IDLE, PROGRAM_BASE, TABLE_BASE, TABLE_SIZE, BusCfg, CrcCfg, Image, LaneCfg, LineCfg, Profile,
)
from emu.isa import INF, BusKind, Owner


def image(**kw):
    base = dict(name="t", parties=("a", "b"), words=[], profiles=[IDLE],
                lines=[LineCfg("l0", "pp", Owner.P0, 1)], lanes=[])
    base.update(kw)
    return Image(**base)


def test_bases_and_size():
    assert (TABLE_BASE, PROGRAM_BASE, TABLE_SIZE) == (0x0400, 0x1000, 0x58)
    assert len(image().table_bytes()) == TABLE_SIZE


def test_profile_slots_min_nom_max_le():
    t = image(profiles=[IDLE, Profile("A", 1, 2, 3), Profile("B", 0x1234, 0x5678, INF)]).table_bytes()
    assert t[0x00:0x06] == bytes([0, 0, 0, 0, 0xFF, 0xFF])  # PROF0 = IDLE; the chip ignores it
    assert t[0x06:0x0C] == bytes([1, 0, 2, 0, 3, 0])
    assert t[0x0C:0x12] == bytes([0x34, 0x12, 0x78, 0x56, 0xFF, 0xFF])
    assert t[0x12:0x30] == bytes(0x1E)  # unused slots 3-7


def test_line_byte_pin_od_owner_idle_gated():
    """LINE byte, LSB first: pin[2:0] od[3] owner[5:4] idle[6] gated[7]."""
    t = image(lines=[LineCfg("a", "od", Owner.P1, idle=1, pin=5),
                     LineCfg("b", "pp", Owner.P0, idle=0, gated=True, pin=2),
                     LineCfg("c", "od", Owner.SHARED, idle=1)]).table_bytes()
    assert t[0x30] == 5 | 1 << 3 | 1 << 4 | 1 << 6
    assert t[0x31] == 2 | 1 << 7
    assert t[0x32] == 2 | 1 << 3 | 2 << 4 | 1 << 6  # pin defaults to the line's index


def test_unused_line_slots_are_released():
    """An all-zero slot would read as a P0-owned push-pull line at idle 0, which an engine in
    role P0 drives low at start (isa §2). Unused slots are written as released OD lines."""
    t = image().table_bytes()
    for i in range(1, 8):
        assert t[0x30 + i] == i | 1 << 3 | 2 << 4 | 1 << 6


def test_lane_bytes():
    """LANE: b0 data[2:0] clk[5:3] has_clk cpol; b1 cpha data2[3:1] has_d2 stretch nrzi;
    b2 ldelay[3:0] stuff_n[6:4] d2_comp; b3 0."""
    full = LaneCfg(data=3, clk=5, cpol=1, cpha=1, data2=6, stretch=1, ldelay=9, nrzi=1, stuff_n=6, d2_comp=1)
    t = image(lanes=[LaneCfg(data=2), full]).table_bytes()
    assert t[0x38:0x3C] == bytes([2, 0, 0, 0])
    assert t[0x3C:0x40] == bytes([
        3 | 5 << 3 | 1 << 6 | 1 << 7,
        1 | 6 << 1 | 1 << 4 | 1 << 5 | 1 << 6,
        9 | 6 << 4 | 1 << 7,
        0,
    ])
    assert t[0x40:0x48] == bytes(8)


def test_header_fields():
    img = image(abort_pc=0x1A5, crc=CrcCfg(width=16, poly=0x8005, init=0xFFFF, xorout=0, lsb_first=True),
                bus=BusCfg(BusKind.I2C, a=1, b=4), prof_shift=6)
    t = img.table_bytes()
    assert t[0x48:0x4A] == bytes(2)
    assert t[0x4A:0x4C] == bytes([0xA5, 0x01])
    assert t[0x4C] == 16 and t[0x4D] == 1
    assert t[0x4E:0x54] == bytes([0x05, 0x80, 0xFF, 0xFF, 0x00, 0x00])
    assert t[0x54] == BusKind.I2C
    assert t[0x55] == 1 | 4 << 3
    assert t[0x56] == 15
    assert t[0x57] == 6


def test_header_defaults():
    """CRC-16/CCITT-FALSE (isa §4.4), no bus, prof_shift 4 (isa §2)."""
    t = image().table_bytes()
    assert t[0x4C:0x58] == bytes([16, 0, 0x21, 0x10, 0xFF, 0xFF, 0, 0, BusKind.NONE, 0, 0, 4])


@pytest.mark.parametrize("kind,cycles", [(BusKind.NONE, 0), (BusKind.I2C, 15), (BusKind.SELECT, 1),
                                         (BusKind.SE0, 33)])
def test_bus_confirm_default_per_kind(kind, cycles):
    assert BusCfg(kind).confirm_cycles == cycles
    assert BusCfg(kind, confirm=7).confirm_cycles == 7


def test_program_words_le_and_host_writes():
    img = image(words=[0x11223344, 0xAABBCCDD])
    assert img.program_bytes() == bytes([0x44, 0x33, 0x22, 0x11, 0xDD, 0xCC, 0xBB, 0xAA])
    assert img.host_writes() == [(TABLE_BASE, img.table_bytes()), (PROGRAM_BASE, img.program_bytes())]


@pytest.mark.parametrize("kw", [
    dict(lines=[LineCfg(f"l{i}", "pp", Owner.P0) for i in range(9)]),
    dict(lanes=[LaneCfg(0)] * 5),
    dict(profiles=[IDLE] + [Profile(f"p{i}", 1, 1, 1) for i in range(8)]),
    dict(profiles=[IDLE, Profile("big", 1, 0x10000, 0x10000)]),
    dict(lines=[LineCfg("l", "pp", Owner.P0, pin=8)]),
    dict(lanes=[LaneCfg(0, ldelay=16)]),
    dict(lanes=[LaneCfg(8)]),
    dict(abort_pc=512),
    dict(words=[0] * 513),
    dict(bus=BusCfg(BusKind.I2C, a=8)),
    dict(bus=BusCfg(BusKind.I2C, confirm=256)),
    dict(crc=CrcCfg(width=0)),
    dict(crc=CrcCfg(width=17)),
])
def test_out_of_range_raises(kw):
    img = image(**kw)
    with pytest.raises(ValueError):
        img.table_bytes() if "words" not in kw else img.program_bytes()


def test_json_roundtrip_keeps_v1_fields():
    img = image(profiles=[IDLE, Profile("A", 1, 2, 3)],
                lines=[LineCfg("a", "od", Owner.SHARED, 1, gated=False, pin=6), LineCfg("b", "pp", Owner.P1, 0, gated=True)],
                lanes=[LaneCfg(0, 1, 1, 0, data2=2, stretch=1, ldelay=3, nrzi=1, stuff_n=6, d2_comp=1)],
                abort_pc=9, crc=CrcCfg(5, 0x05, 0x1F, 0x1F, True), bus=BusCfg(BusKind.SELECT, 2, 3, confirm=4),
                prof_shift=2, words=[0xDEADBEEF], debug={0: "x"})
    assert Image.from_json(img.to_json()) == img
