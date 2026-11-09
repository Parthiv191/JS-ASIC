"""Program image: instruction words plus the header tables both engines share (isa.md §2).

`table_bytes()` and `program_bytes()` are what the host writes over SPI (architecture.md §4):
the shared tables at TABLE_BASE (written only while stopped) and the program at PROGRAM_BASE.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Literal

from .isa import INF, NUM_LANES, NUM_LINES, NUM_PROFILES, PROG_DEPTH, BusKind, Instr, Owner, decode

TABLE_BASE = 0x0400
PROGRAM_BASE = 0x1000
TABLE_SIZE = 0x58

_PROF, _LINE, _LANE = 0x00, 0x30, 0x38
_ABORT_PC, _CRC_WIDTH, _CRC_FLAGS, _CRC_POLY, _CRC_INIT, _CRC_XOROUT = 0x4A, 0x4C, 0x4D, 0x4E, 0x50, 0x52
_BUS_KIND, _BUS_AB, _BUS_CONFIRM, _PROF_SHIFT = 0x54, 0x55, 0x56, 0x57

BUS_CONFIRM_DEFAULT = {BusKind.NONE: 0, BusKind.I2C: 15, BusKind.SELECT: 1, BusKind.SE0: 33}


@dataclass(frozen=True)
class Profile:
    name: str
    min: int
    nom: int
    max: int

    @property
    def bounded(self) -> bool:
        return self.max != INF


IDLE = Profile("IDLE", 0, 0, INF)


@dataclass(frozen=True)
class LineCfg:
    name: str
    kind: Literal["pp", "od"]
    owner: Owner      # P0, P1, SHARED (OD only); code 3 = SWITCHED, reserved
    idle: int = 1
    gated: bool = False
    pin: int | None = None  # uio pin the line maps to; None = the line's own index


@dataclass(frozen=True)
class LaneCfg:
    data: int
    clk: int | None = None
    cpol: int = 0
    cpha: int = 0
    data2: int | None = None  # duplex data line, or the complement with the CODEC
    stretch: int = 0
    ldelay: int = 0           # data launch/release delay after the launching edge, cycles
    nrzi: int = 0             # CODEC fields [P] (isa §4.10)
    stuff_n: int = 0
    d2_comp: int = 0


@dataclass(frozen=True)
class CrcCfg:
    """Catalogue form (isa §4.4). The default is CRC-16/CCITT-FALSE."""
    width: int = 16
    poly: int = 0x1021
    init: int = 0xFFFF
    xorout: int = 0
    lsb_first: bool = False


@dataclass(frozen=True)
class BusCfg:
    kind: BusKind = BusKind.NONE
    a: int = 0
    b: int = 0
    confirm: int | None = None  # cycles; None = the kind's default

    @property
    def confirm_cycles(self) -> int:
        return BUS_CONFIRM_DEFAULT[BusKind(self.kind)] if self.confirm is None else self.confirm


@dataclass
class Image:
    name: str
    parties: tuple[str, str]
    words: list[int]
    profiles: list[Profile]
    lines: list[LineCfg]
    lanes: list[LaneCfg]
    abort_pc: int = 0
    tolerance_ppm: int = 20000  # compiler only; not written to the chip
    debug: dict[int, str] = field(default_factory=dict)
    crc: CrcCfg = CrcCfg()
    bus: BusCfg = BusCfg()
    prof_shift: int = 4

    def instrs(self) -> list[Instr]:
        return [decode(w) for w in self.words]

    def profile_index(self, name: str) -> int:
        for i, p in enumerate(self.profiles):
            if p.name == name:
                return i
        raise KeyError(name)

    def with_profiles(self, scale: dict[str, float]) -> "Image":
        """Copy with the named profiles' nominal times scaled; windows unchanged.

        Models a device that runs the same description with sloppy timing.
        """
        profs = [
            replace(p, nom=max(1, round(p.nom * scale[p.name]))) if p.name in scale else p
            for p in self.profiles
        ]
        return replace(self, profiles=profs, words=list(self.words), debug=dict(self.debug))

    # ---- host writes (architecture §4)
    def table_bytes(self) -> bytes:
        """The shared tables, TABLE_SIZE bytes from TABLE_BASE."""
        _limit("profiles", len(self.profiles), NUM_PROFILES)
        _limit("lines", len(self.lines), NUM_LINES)
        _limit("lanes", len(self.lanes), NUM_LANES)
        t = bytearray(TABLE_SIZE)
        for i, p in enumerate(self.profiles):
            for k, v in enumerate((p.min, p.nom, p.max)):
                _put16(t, _PROF + 6 * i + 2 * k, v, f"profile {p.name}")
        for i in range(NUM_LINES):
            t[_LINE + i] = _line_byte(self.lines[i], i) if i < len(self.lines) else _UNUSED_LINE | i
        for i, la in enumerate(self.lanes):
            t[_LANE + 4 * i:_LANE + 4 * i + 4] = _lane_bytes(la)
        _check(0 <= self.abort_pc < PROG_DEPTH, f"abort_pc {self.abort_pc} outside the program")
        _put16(t, _ABORT_PC, self.abort_pc, "abort_pc")
        c = self.crc
        _check(1 <= c.width <= 16, f"CRC width {c.width} not in 1..16")
        t[_CRC_WIDTH] = c.width
        t[_CRC_FLAGS] = int(bool(c.lsb_first))
        for off, v, what in ((_CRC_POLY, c.poly, "poly"), (_CRC_INIT, c.init, "init"), (_CRC_XOROUT, c.xorout, "xorout")):
            _put16(t, off, v, f"CRC {what}")
        b = self.bus
        t[_BUS_KIND] = BusKind(b.kind)
        t[_BUS_AB] = _bits(b.a, 3, "bus_a") | _bits(b.b, 3, "bus_b") << 3
        t[_BUS_CONFIRM] = _bits(b.confirm_cycles, 8, "bus_confirm")
        t[_PROF_SHIFT] = _bits(self.prof_shift, 8, "prof_shift")
        return bytes(t)

    def program_bytes(self) -> bytes:
        """The program from PROGRAM_BASE: 4 bytes per word, little-endian (a word commits on its
        4th byte)."""
        _limit("program words", len(self.words), PROG_DEPTH)
        return b"".join(_bits(w, 32, "word").to_bytes(4, "little") for w in self.words)

    def host_writes(self) -> list[tuple[int, bytes]]:
        return [(TABLE_BASE, self.table_bytes()), (PROGRAM_BASE, self.program_bytes())]

    # ---- file format
    def to_json(self) -> str:
        return json.dumps({
            "format": 1,
            "name": self.name,
            "parties": list(self.parties),
            "words": [f"{w:08x}" for w in self.words],
            "profiles": [[p.name, p.min, p.nom, p.max] for p in self.profiles],
            "lines": [[ln.name, ln.kind, int(ln.owner), ln.idle, ln.gated, ln.pin] for ln in self.lines],
            "lanes": [asdict(la) for la in self.lanes],
            "abort_pc": self.abort_pc,
            "tolerance_ppm": self.tolerance_ppm,
            "debug": {str(k): v for k, v in self.debug.items()},
            "crc": asdict(self.crc),
            "bus": {**asdict(self.bus), "kind": int(self.bus.kind)},
            "prof_shift": self.prof_shift,
        }, indent=1)

    @classmethod
    def from_json(cls, s: str) -> "Image":
        j = json.loads(s)
        return cls(
            name=j["name"],
            parties=tuple(j["parties"]),
            words=[int(w, 16) for w in j["words"]],
            profiles=[Profile(*p) for p in j["profiles"]],
            lines=[LineCfg(n, k, Owner(o), i, g, p) for n, k, o, i, g, p in j["lines"]],
            lanes=[LaneCfg(**la) for la in j["lanes"]],
            abort_pc=j["abort_pc"],
            tolerance_ppm=j["tolerance_ppm"],
            debug={int(k): v for k, v in j["debug"].items()},
            crc=CrcCfg(**j["crc"]),
            bus=BusCfg(**{**j["bus"], "kind": BusKind(j["bus"]["kind"])}),
            prof_shift=j["prof_shift"],
        )


# An unused LINE slot: a released OD line (owner SHARED, idle 1), so no engine ever drives its pin.
_UNUSED_LINE = 1 << 3 | int(Owner.SHARED) << 4 | 1 << 6


def _line_byte(ln: LineCfg, index: int) -> int:
    pin = index if ln.pin is None else ln.pin
    return (_bits(pin, 3, f"line {ln.name} pin") | int(ln.kind == "od") << 3
            | _bits(int(ln.owner), 2, "owner") << 4 | _bits(ln.idle, 1, "idle") << 6 | int(bool(ln.gated)) << 7)


def _lane_bytes(la: LaneCfg) -> bytes:
    has_clk, has_d2 = la.clk is not None, la.data2 is not None
    b0 = (_bits(la.data, 3, "lane data") | _bits(la.clk or 0, 3, "lane clk") << 3 | int(has_clk) << 6
          | _bits(la.cpol, 1, "cpol") << 7)
    b1 = (_bits(la.cpha, 1, "cpha") | _bits(la.data2 or 0, 3, "lane data2") << 1 | int(has_d2) << 4
          | _bits(la.stretch, 1, "stretch") << 5 | _bits(la.nrzi, 1, "nrzi") << 6)
    b2 = _bits(la.ldelay, 4, "ldelay") | _bits(la.stuff_n, 3, "stuff_n") << 4 | _bits(la.d2_comp, 1, "d2_comp") << 7
    return bytes([b0, b1, b2, 0])


def _bits(v: int, width: int, what: str) -> int:
    _check(0 <= int(v) < 1 << width, f"{what} = {v} does not fit in {width} bits")
    return int(v)


def _put16(t: bytearray, off: int, v: int, what: str) -> None:
    t[off:off + 2] = _bits(v, 16, what).to_bytes(2, "little")


def _limit(what: str, n: int, most: int) -> None:
    _check(n <= most, f"{n} {what} (the chip has {most})")


def _check(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)
