"""ISA v0 encoding: the single source of truth shared by compiler, model and RTL.

See docs/design/isa-v0.md for semantics. Word layout:
  [31:27] op  [26:25] owner  [24:21] pin  [20:17] prof  [16:5] operand  [4:0] flags
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

SYNC_LAT = 2
EDGE_LAT = SYNC_LAT + 1
QUANT = 1
NUM_LINES = 8
NUM_REGS = 4
NUM_PROFILES = 16
NUM_LANES = 16
PROG_DEPTH = 64  # words per image (team draft); branch targets stay 12-bit
INF = 0xFFFF
REG_MASK = 0xFFFF
MAX_XFER_BITS = 16


class Op(IntEnum):
    NOP = 0
    COND = 1
    HOLD = 2
    XFER = 3
    LI = 4
    ALU = 5
    BR = 6
    ROLE_IF = 7
    PUSH = 8
    POP = 9
    MARK = 10
    RAW = 11
    HALT = 12
    CRC = 13
    FZ = 14
    # reserved for later phases
    CHOICE = 16
    ARB = 17
    WAIT_ANY = 18
    CODEC = 19


TIMED_OPS = frozenset({Op.COND, Op.HOLD, Op.XFER})


class Owner(IntEnum):
    P0 = 0
    P1 = 1
    SHARED = 2
    NONE = 3


class Role(IntEnum):
    P0 = 0
    P1 = 1
    MON = 2


class CondKind(IntEnum):
    FALL = 0
    RISE = 1
    START = 2
    STOP = 3


class AluOp(IntEnum):
    ADD = 0
    SUB = 1
    AND = 2
    OR = 3
    XOR = 4
    SHL = 5
    SHR = 6
    EQ = 7
    NE = 8
    LT = 9


class BrCond(IntEnum):
    ALWAYS = 0
    Z = 1
    NZ = 2
    TO = 3  # last timed step timed out; role blocks only (R2)


class RawOp(IntEnum):
    SET = 0
    REL = 1
    GET = 2
    DELAY = 3
    JPIN = 4


class CrcOp(IntEnum):
    RESET = 0
    FEED = 1
    GET = 2


class FzOp(IntEnum):
    NEXT = 0
    TOK = 1
    END = 2


class Src(IntEnum):
    ZERO = 0
    ONES = 1
    FIFO = 2
    FZ = 3  # next staged fuzz byte (bus-fuzzer.md)
    R0 = 4
    R1 = 5
    R2 = 6
    R3 = 7


class Dst(IntEnum):
    DISCARD = 0
    FIFO = 2
    R0 = 4
    R1 = 5
    R2 = 6
    R3 = 7


F_CHK = 1 << 0
F_RESYNC = 1 << 1
F_ABORT = 1 << 2
F_REG = 1 << 3
F_IRQ = 1 << 4  # MARK raises the host interrupt

_FIELDS = (  # name, shift, width
    ("op", 27, 5),
    ("owner", 25, 2),
    ("pin", 21, 4),
    ("prof", 17, 4),
    ("operand", 5, 12),
    ("flags", 0, 5),
)


@dataclass(frozen=True)
class Instr:
    op: Op
    owner: Owner = Owner.NONE
    pin: int = 0
    prof: int = 0
    operand: int = 0
    flags: int = 0


def encode(i: Instr) -> int:
    w = 0
    for name, shift, width in _FIELDS:
        v = int(getattr(i, name))
        if not 0 <= v < (1 << width):
            raise ValueError(f"field {name}={v} does not fit in {width} bits")
        w |= v << shift
    return w


def decode(w: int) -> Instr:
    if not 0 <= w < (1 << 32):
        raise ValueError(f"word {w:#x} is not 32 bits")
    vals = {name: (w >> shift) & ((1 << width) - 1) for name, shift, width in _FIELDS}
    try:
        op = Op(vals.pop("op"))
    except ValueError as e:
        raise ValueError(f"unknown opcode in {w:#010x}") from e
    return Instr(op=op, owner=Owner(vals.pop("owner")), **vals)


def xfer_operand(n: int, src: Src, dst: Dst, msb_first: bool) -> int:
    if not 1 <= n <= MAX_XFER_BITS:
        raise ValueError(f"xfer width {n} not in 1..{MAX_XFER_BITS}")
    return (n - 1) | (int(src) << 4) | (int(dst) << 7) | (int(bool(msb_first)) << 10)


def unpack_xfer(operand: int) -> tuple[int, Src, Dst, bool]:
    return ((operand & 0xF) + 1, Src((operand >> 4) & 0x7), Dst((operand >> 7) & 0x7),
            bool((operand >> 10) & 1))


def role_mask(roles: Iterable[Role]) -> int:
    m = 0
    for r in roles:
        m |= 1 << int(r)
    return m


def role_if_operand(mask: int, count: int) -> int:
    if not 0 <= mask < 8:
        raise ValueError(f"role mask {mask} out of range")
    if not 0 <= count < 512:
        raise ValueError(f"role block length {count} exceeds 511")
    return mask | (count << 3)


def unpack_role_if(operand: int) -> tuple[int, int]:
    return operand & 0x7, operand >> 3


def reg_of(code: int) -> int:
    """Register index for a Src/Dst code in 4..7."""
    return int(code) - 4
