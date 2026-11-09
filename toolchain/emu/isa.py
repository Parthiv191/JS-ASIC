"""ISA v1 encoding: the single source of truth shared by compiler, model and RTL tests.

See docs/design/isa.md. Two word shapes, selected by the opcode (isa §3):
  T (COND HOLD XFER): [31:27] op | [26:25] owner | [24:21] line/lane | [20:17] prof | [16:5] operand | [4:0] flags
  U (all others):     [31:27] op | [26:24] rd    | [23:21] ra/line   | [20:17] sub  | [16:5] operand | [4:0] flags
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

# isa §1
SYNC_LAT = 2
EDGE_LAT = SYNC_LAT + 1
QUANT = 1
ISSUE = 2          # max cycles from a timed step's completion to its successor's start
U = 2              # cycles per untimed instruction
NUM_REGS = 8
NUM_LINES = 8
NUM_PROFILES = 8   # built; the prof field allows 16
NUM_LANES = 4      # built; the lane field allows 16
NUM_ENGINES = 2
PROG_DEPTH = 512   # words, one image shared by both engines; branch targets are 12-bit
TX_DEPTH = 4
RX_DEPTH = 8
TIME_BITS = 24     # now, tb, anchors
INF = 0xFFFF       # profile max = INF: no timeout
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
    PROF = 15
    # reserved, no v1 hardware: executing one is ILLEGAL (isa §4.1). Codes 20-31 are free.
    CHOICE = 16
    ARB = 17
    WAIT_ANY = 18
    CODEC = 19


T_OPS = frozenset({Op.COND, Op.HOLD, Op.XFER})  # the T shape; every other opcode is U
TIMED_OPS = T_OPS


class Owner(IntEnum):
    """Step owner (T shape) and line owner codes. Code 3 is reserved: a line's SWITCHED owner,
    and the default for U instructions, which have no owner field."""
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
    TO = 3    # the last timed step timed out; role blocks only (R2)
    ROLE = 4  # taken iff the engine's role is in the mask held in the ra field


class RawOp(IntEnum):
    SET = 0
    REL = 1
    GET = 2
    DELAY = 3
    JPIN = 4
    EVT = 5


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
    FZ = 3  # the next staged fuzz byte (bus-fuzzer.md)
    R0 = 8
    R1 = 9
    R2 = 10
    R3 = 11
    R4 = 12
    R5 = 13
    R6 = 14
    R7 = 15


class Dst(IntEnum):
    DISCARD = 0
    FIFO = 2
    R0 = 8
    R1 = 9
    R2 = 10
    R3 = 11
    R4 = 12
    R5 = 13
    R6 = 14
    R7 = 15


class BusKind(IntEnum):
    NONE = 0
    I2C = 1
    SELECT = 2  # SPI chip select
    SE0 = 3     # USB-LS end of packet [P]


F_CHK = 1 << 0
F_RESYNC = 1 << 1
F_ABORT = 1 << 2
F_MSB = 1 << 3  # XFER only
F_REG = 1 << 3  # ALU only
F_IRQ = 1 << 4  # MARK only: raise the host interrupt

_COMMON = (("op", 27, 5), ("operand", 5, 12), ("flags", 0, 5))
_T = (("owner", 25, 2), ("line", 21, 4), ("prof", 17, 4))
_U = (("rd", 24, 3), ("ra", 21, 3), ("sub", 17, 4))


@dataclass(frozen=True)
class Instr:
    """One instruction word. T instructions use owner/line/prof (line holds the lane for XFER);
    U instructions use rd/ra/sub (ra holds the line for RAW). The other shape's fields stay at
    their defaults, since both shapes share bits [26:17]."""
    op: Op
    owner: Owner = Owner.NONE
    line: int = 0
    prof: int = 0
    rd: int = 0
    ra: int = 0
    sub: int = 0
    operand: int = 0
    flags: int = 0


def _shape(op: Op) -> tuple[tuple[tuple[str, int, int], ...], tuple[tuple[str, int, int], ...]]:
    """(this shape's fields, the other shape's fields)."""
    return (_T, _U) if op in T_OPS else (_U, _T)


def encode(i: Instr) -> int:
    op = Op(i.op)
    mine, other = _shape(op)
    for name, _, _ in other:
        if getattr(i, name) != getattr(Instr, name):
            raise ValueError(f"{op.name} is a {'T' if op in T_OPS else 'U'}-shape instruction and has no "
                             f"{name} field")
    w = 0
    for name, shift, width in _COMMON + mine:
        v = int(getattr(i, name))
        if not 0 <= v < (1 << width):
            raise ValueError(f"field {name}={v} does not fit in {width} bits")
        w |= v << shift
    return w


def decode(w: int) -> Instr:
    if not 0 <= w < (1 << 32):
        raise ValueError(f"word {w:#x} is not 32 bits")
    try:
        op = Op(w >> 27)
    except ValueError as e:
        raise ValueError(f"free opcode {w >> 27} in {w:#010x}") from e
    mine, _ = _shape(op)
    vals = {name: (w >> shift) & ((1 << width) - 1) for name, shift, width in _COMMON[1:] + mine}
    if "owner" in vals:
        vals["owner"] = Owner(vals["owner"])
    return Instr(op=op, **vals)


# ---- operand helpers (isa §3 table)
def xfer_operand(n: int, src: Src, dst: Dst) -> int:
    """XFER operand: [3:0] n-1, [7:4] src, [11:8] dst. Bit order is the MSB flag."""
    if not 1 <= n <= MAX_XFER_BITS:
        raise ValueError(f"xfer width {n} not in 1..{MAX_XFER_BITS}")
    return (n - 1) | (int(src) << 4) | (int(dst) << 8)


def unpack_xfer(operand: int) -> tuple[int, Src, Dst]:
    return (operand & 0xF) + 1, Src((operand >> 4) & 0xF), Dst((operand >> 8) & 0xF)


def cond_operand(kind: CondKind, meas_rd: int | None = None) -> int:
    """COND operand: [1:0] kind, [2] MEAS, [5:3] rd that receives the measurement."""
    if meas_rd is None:
        return int(kind)
    if not 0 <= meas_rd < NUM_REGS:
        raise ValueError(f"measure register r{meas_rd} out of range")
    return int(kind) | 1 << 2 | meas_rd << 3


def unpack_cond(operand: int) -> tuple[CondKind, bool, int]:
    return CondKind(operand & 3), bool(operand >> 2 & 1), operand >> 3 & 7


def li(rd: int, imm: int) -> Instr:
    """LI rd, imm16: imm16 = {sub, operand}."""
    if not 0 <= imm <= 0xFFFF:
        raise ValueError(f"LI immediate {imm!r} not in 0..0xFFFF")
    return Instr(Op.LI, rd=rd, sub=imm >> 12, operand=imm & 0xFFF)


def li_imm(i: Instr) -> int:
    return i.sub << 12 | i.operand


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


def reg_of(code: Src | Dst | int) -> int:
    """Register index for a Src/Dst register code (r0-r7 = 8-15)."""
    if not 8 <= int(code) <= 15:
        raise ValueError(f"code {int(code)} is not a register")
    return int(code) - 8
