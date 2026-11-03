import pytest
from hypothesis import example, given, strategies as st

from emu.isa import (
    Dst, Instr, Op, Owner, Src, decode, encode, role_if_operand, role_mask,
    Role, unpack_role_if, unpack_xfer, xfer_operand,
)


def test_encode_pins_field_layout():
    w = encode(Instr(Op.XFER, Owner.P1, pin=3, prof=5, operand=0x7AB, flags=0b10101))
    assert w == (3 << 27) | (1 << 25) | (3 << 21) | (5 << 17) | (0x7AB << 5) | 0b10101


instrs = st.builds(
    Instr,
    op=st.sampled_from(list(Op)),
    owner=st.sampled_from(list(Owner)),
    pin=st.integers(0, 15),
    prof=st.integers(0, 15),
    operand=st.integers(0, 0xFFF),
    flags=st.integers(0, 31),
)


@given(instrs)
def test_roundtrip_property(i):
    assert decode(encode(i)) == i


@pytest.mark.parametrize("kwargs", [
    dict(pin=16), dict(prof=16), dict(operand=4096), dict(flags=32), dict(pin=-1),
])
def test_field_overflow_raises(kwargs):
    with pytest.raises(ValueError):
        encode(Instr(Op.NOP, **kwargs))


def test_unknown_opcode_raises():
    with pytest.raises(ValueError):
        decode(31 << 27)


@given(st.integers(1, 16), st.sampled_from(list(Src)), st.sampled_from(list(Dst)), st.booleans())
@example(1, Src.ZERO, Dst.DISCARD, False)
@example(16, Src(max(Src)), Dst(max(Dst)), True)
def test_xfer_operand_roundtrip(n, src, dst, msb):
    assert unpack_xfer(xfer_operand(n, src, dst, msb)) == (n, src, dst, msb)


def test_xfer_operand_rejects_bad_width():
    for n in (0, 17):
        with pytest.raises(ValueError):
            xfer_operand(n, Src.ZERO, Dst.DISCARD, False)


def test_role_if_operand_roundtrip():
    mask = role_mask([Role.P1, Role.MON])
    assert mask == 0b110
    assert unpack_role_if(role_if_operand(mask, 300)) == (0b110, 300)
    with pytest.raises(ValueError):
        role_if_operand(mask, 512)


def test_r2_opcode_allocation():
    """isa-v0 r2 §1/§3: 64-word programs and the CRC/FZ/IRQ/TO codes."""
    from emu import isa
    assert isa.PROG_DEPTH == 64
    assert (isa.Op.CRC, isa.Op.FZ) == (13, 14)
    assert isa.BrCond.TO == 3 and isa.Src.FZ == 3
    assert isa.F_IRQ == 1 << 4
    assert [(m.name, m.value) for m in isa.CrcOp] == [("RESET", 0), ("FEED", 1), ("GET", 2)]
    assert [(m.name, m.value) for m in isa.FzOp] == [("NEXT", 0), ("TOK", 1), ("END", 2)]
