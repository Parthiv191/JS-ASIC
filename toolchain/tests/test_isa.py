"""ISA v1 encoding (docs/design/isa.md §1-§3).

The golden words are built from the bit positions in isa.md §3 with explicit shifts, not
from emu.isa's field table, so a misread layout in emu.isa fails here.
"""
import pytest
from hypothesis import example, given, strategies as st

from emu import isa
from emu.isa import (
    F_ABORT, F_CHK, F_IRQ, F_MSB, F_REG, F_RESYNC, AluOp, BrCond, BusKind, CondKind, CrcOp, Dst,
    FzOp, Instr, Op, Owner, RawOp, Role, Src, T_OPS, cond_operand, decode, encode, li, li_imm,
    reg_of, role_if_operand, role_mask, unpack_cond, unpack_role_if, unpack_xfer, xfer_operand,
)


def t_word(op, owner, line, prof, operand=0, flags=0):
    return (op << 27) | (owner << 25) | (line << 21) | (prof << 17) | (operand << 5) | flags


def u_word(op, rd=0, ra=0, sub=0, operand=0, flags=0):
    return (op << 27) | (rd << 24) | (ra << 21) | (sub << 17) | (operand << 5) | flags


GOLDEN = [
    # COND RISE on line 2, P1 drives, profile 3, MEAS into r5, ABORT
    (Instr(Op.COND, owner=Owner.P1, line=2, prof=3, operand=1 | 1 << 2 | 5 << 3, flags=F_ABORT),
     t_word(1, 1, 2, 3, 1 | 1 << 2 | 5 << 3, 1 << 2)),
    # HOLD line 6 at level 1, SHARED, profile 1
    (Instr(Op.HOLD, owner=Owner.SHARED, line=6, prof=1, operand=1), t_word(2, 2, 6, 1, 1)),
    # XFER lane 3, 8 bits, src r2 (code 10), dst FIFO (2), MSB first, CHK
    (Instr(Op.XFER, owner=Owner.P0, line=3, prof=4, operand=7 | 10 << 4 | 2 << 8,
           flags=F_MSB | F_CHK),
     t_word(3, 0, 3, 4, 7 | 10 << 4 | 2 << 8, 1 << 3 | 1 << 0)),
    # LI r7, 0xBEEF: imm16 = {sub, operand}
    (Instr(Op.LI, rd=7, sub=0xB, operand=0xEEF), u_word(4, rd=7, sub=0xB, operand=0xEEF)),
    # ALU r1 = r2 SUB r3 (REG)
    (Instr(Op.ALU, rd=1, ra=2, sub=AluOp.SUB, operand=3, flags=F_REG),
     u_word(5, rd=1, ra=2, sub=1, operand=3, flags=1 << 3)),
    # BR NZ r4 -> 0x1FF
    (Instr(Op.BR, ra=4, sub=BrCond.NZ, operand=0x1FF), u_word(6, ra=4, sub=2, operand=0x1FF)),
    # BR ROLE: taken iff the role is in the mask {P1, MON} held in the ra field
    (Instr(Op.BR, ra=0b110, sub=BrCond.ROLE, operand=0x40), u_word(6, ra=0b110, sub=4, operand=0x40)),
    # ROLE_IF mask {P0}, count 3
    (Instr(Op.ROLE_IF, operand=1 | 3 << 3), u_word(7, operand=1 | 3 << 3)),
    (Instr(Op.PUSH, ra=6), u_word(8, ra=6)),
    (Instr(Op.POP, rd=3), u_word(9, rd=3)),
    (Instr(Op.MARK, operand=0x2A, flags=F_IRQ), u_word(10, operand=0x2A, flags=1 << 4)),
    # RAW GET r2 <- line 5 (line in [23:21], rd for GET)
    (Instr(Op.RAW, rd=2, ra=5, sub=RawOp.GET), u_word(11, rd=2, ra=5, sub=2)),
    # RAW JPIN line 1, level 1, target 0x123
    (Instr(Op.RAW, ra=1, sub=RawOp.JPIN, operand=1 << 11 | 0x123),
     u_word(11, ra=1, sub=4, operand=1 << 11 | 0x123)),
    # RAW EVT r0, clear mask 0x3F
    (Instr(Op.RAW, rd=0, sub=RawOp.EVT, operand=0x3F), u_word(11, sub=5, operand=0x3F)),
    (Instr(Op.HALT), u_word(12)),
    # CRC FEED r3, 8 bits (operand n-1)
    (Instr(Op.CRC, ra=3, sub=CrcOp.FEED, operand=7), u_word(13, ra=3, sub=1, operand=7)),
    # PROF 7 <- r1
    (Instr(Op.PROF, ra=1, sub=7), u_word(15, ra=1, sub=7)),
]


@pytest.mark.parametrize("ins,word", GOLDEN, ids=[f"{i.op.name}-{w:08x}" for i, w in GOLDEN])
def test_golden_words(ins, word):
    assert encode(ins) == word
    assert decode(word) == ins


def test_opcode_table():
    assert [(o.name, o.value) for o in Op] == [
        ("NOP", 0), ("COND", 1), ("HOLD", 2), ("XFER", 3), ("LI", 4), ("ALU", 5), ("BR", 6),
        ("ROLE_IF", 7), ("PUSH", 8), ("POP", 9), ("MARK", 10), ("RAW", 11), ("HALT", 12),
        ("CRC", 13), ("FZ", 14), ("PROF", 15), ("CHOICE", 16), ("ARB", 17), ("WAIT_ANY", 18),
        ("CODEC", 19),
    ]
    assert T_OPS == {Op.COND, Op.HOLD, Op.XFER}


def test_sub_and_code_tables():
    assert [(m.name, m.value) for m in BrCond] == [
        ("ALWAYS", 0), ("Z", 1), ("NZ", 2), ("TO", 3), ("ROLE", 4)]
    assert [(m.name, m.value) for m in RawOp] == [
        ("SET", 0), ("REL", 1), ("GET", 2), ("DELAY", 3), ("JPIN", 4), ("EVT", 5)]
    assert [(m.name, m.value) for m in CrcOp] == [("RESET", 0), ("FEED", 1), ("GET", 2)]
    assert [(m.name, m.value) for m in FzOp] == [("NEXT", 0), ("TOK", 1), ("END", 2)]
    assert [m.value for m in CondKind] == [0, 1, 2, 3]
    assert [m.value for m in AluOp] == list(range(10))
    assert [(m.name, m.value) for m in Src] == [("ZERO", 0), ("ONES", 1), ("FIFO", 2), ("FZ", 3)] + [
        (f"R{i}", 8 + i) for i in range(8)]
    assert [(m.name, m.value) for m in Dst] == [("DISCARD", 0), ("FIFO", 2)] + [
        (f"R{i}", 8 + i) for i in range(8)]
    assert [(m.name, m.value) for m in BusKind] == [("NONE", 0), ("I2C", 1), ("SELECT", 2), ("SE0", 3)]
    assert (F_CHK, F_RESYNC, F_ABORT, F_MSB, F_REG, F_IRQ) == (1, 2, 4, 8, 8, 16)


def test_constants():
    """isa.md §1."""
    assert (isa.SYNC_LAT, isa.EDGE_LAT, isa.QUANT, isa.ISSUE, isa.U) == (2, 3, 1, 2, 2)
    assert (isa.NUM_REGS, isa.NUM_LINES, isa.NUM_PROFILES, isa.NUM_LANES) == (8, 8, 8, 4)
    assert (isa.PROG_DEPTH, isa.TX_DEPTH, isa.RX_DEPTH, isa.TIME_BITS) == (512, 4, 8, 24)
    assert isa.INF == 0xFFFF


u_ops = [o for o in Op if o not in T_OPS]
t_instrs = st.builds(
    Instr, op=st.sampled_from(sorted(T_OPS)), owner=st.sampled_from(list(Owner)),
    line=st.integers(0, 15), prof=st.integers(0, 15), operand=st.integers(0, 0xFFF),
    flags=st.integers(0, 31))
u_instrs = st.builds(
    Instr, op=st.sampled_from(u_ops), rd=st.integers(0, 7), ra=st.integers(0, 7),
    sub=st.integers(0, 15), operand=st.integers(0, 0xFFF), flags=st.integers(0, 31))


@given(st.one_of(t_instrs, u_instrs))
def test_roundtrip_property(i):
    assert decode(encode(i)) == i


@given(st.integers(0, (20 << 27) - 1))
def test_decode_encode_property(w):
    """Every word with a defined opcode decodes to exactly one Instr."""
    assert encode(decode(w)) == w


@pytest.mark.parametrize("op,kwargs", [
    (Op.XFER, dict(line=16)), (Op.XFER, dict(prof=16)), (Op.NOP, dict(operand=4096)),
    (Op.NOP, dict(flags=32)), (Op.ALU, dict(rd=8)), (Op.ALU, dict(ra=8)), (Op.ALU, dict(sub=16)),
    (Op.HOLD, dict(line=-1)),
])
def test_field_overflow_raises(op, kwargs):
    with pytest.raises(ValueError):
        encode(Instr(op, **kwargs))


@pytest.mark.parametrize("ins", [
    Instr(Op.LI, owner=Owner.P1), Instr(Op.ALU, line=1), Instr(Op.BR, prof=2),
    Instr(Op.COND, owner=Owner.P0, rd=1), Instr(Op.XFER, owner=Owner.P0, ra=1),
    Instr(Op.HOLD, owner=Owner.P0, sub=1),
])
def test_field_of_the_other_shape_raises(ins):
    """A U instruction has no owner/line/prof and a T instruction no rd/ra/sub: the bits are shared."""
    with pytest.raises(ValueError):
        encode(ins)


@pytest.mark.parametrize("code", range(20, 32))
def test_free_opcode_raises(code):
    with pytest.raises(ValueError):
        decode(code << 27)


@given(st.integers(1, 16), st.sampled_from(list(Src)), st.sampled_from(list(Dst)))
@example(1, Src.ZERO, Dst.DISCARD)
@example(16, Src.R7, Dst.R7)
def test_xfer_operand_roundtrip(n, src, dst):
    assert unpack_xfer(xfer_operand(n, src, dst)) == (n, src, dst)


def test_xfer_operand_layout():
    assert xfer_operand(8, Src.R2, Dst.FIFO) == 7 | 10 << 4 | 2 << 8
    for n in (0, 17):
        with pytest.raises(ValueError):
            xfer_operand(n, Src.ZERO, Dst.DISCARD)


def test_cond_operand():
    assert cond_operand(CondKind.STOP) == 3
    assert cond_operand(CondKind.RISE, meas_rd=5) == 1 | 1 << 2 | 5 << 3
    assert unpack_cond(1 | 1 << 2 | 5 << 3) == (CondKind.RISE, True, 5)
    assert unpack_cond(2) == (CondKind.START, False, 0)
    with pytest.raises(ValueError):
        cond_operand(CondKind.FALL, meas_rd=8)


@given(st.integers(0, 7), st.integers(0, 0xFFFF))
def test_li_imm16(rd, imm):
    ins = li(rd, imm)
    assert (ins.op, ins.rd) == (Op.LI, rd)
    assert li_imm(ins) == imm
    assert decode(encode(ins)) == ins


def test_li_rejects_17_bits():
    with pytest.raises(ValueError):
        li(0, 0x10000)


def test_reg_of():
    assert [reg_of(Src[f"R{i}"]) for i in range(8)] == list(range(8))
    assert reg_of(Dst.R5) == 5
    with pytest.raises(ValueError):
        reg_of(Src.FIFO)


def test_role_if_operand_roundtrip():
    mask = role_mask([Role.P1, Role.MON])
    assert mask == 0b110
    assert unpack_role_if(role_if_operand(mask, 300)) == (0b110, 300)
    with pytest.raises(ValueError):
        role_if_operand(mask, 512)
