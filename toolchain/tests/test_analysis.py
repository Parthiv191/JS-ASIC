import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF

from descs import spi_like, uart_like  # noqa: F401


def diag_rules(desc, check_timing=True):
    try:
        compile_desc(desc, check_timing=check_timing)
    except CompileError as e:
        return {d.rule for d in e.diags}
    return set()


def two_party():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    return d, ln, p


# ---------------------------------------------------------------- R2: branch taint
def test_r2_rejects_branch_on_popped_reg():
    d, ln, p = two_party()
    d.label("x")
    with d.only("a"):
        d.pop("r0")
    d.br("x", cond="z", reg="r0")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_rejects_branch_on_reg_written_in_role_block():
    d, ln, p = two_party()
    d.label("x")
    with d.only("b"):
        d.li("r1", 3)
    d.br("x", cond="nz", reg="r1")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_accepts_branch_on_xfer_dst():
    d, ln, p = two_party()
    d.label("x")
    with d.only("a"):
        d.pop("r0")
    d.xfer(8, ln, owner="a", prof=p, src="r0", dst="r1")
    d.alu("eq", "r2", "r1", 0x55)
    d.br("x", cond="nz", reg="r2")
    assert diag_rules(d, check_timing=False) == set()


def test_r2_taint_joins_over_loop():
    """r0 is clean on entry but tainted along the back edge, so the loop head is tainted."""
    d, ln, p = two_party()
    d.li("r0", 0)
    d.label("head")
    d.br("out", cond="z", reg="r0")
    with d.only("a"):
        d.pop("r0")
    d.br("head")
    d.label("out")
    d.halt()
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_rejects_jpin_outside_block():
    d, ln, p = two_party()
    d.label("x")
    d.raw_jpin(ln, 0, "x")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}
    d2, ln2, p2 = two_party()
    d2.label("y")
    with d2.only("b"):
        d2.nop()
        d2.label("z")
        d2.raw_jpin(ln2, 0, "z")
    assert diag_rules(d2, check_timing=False) == set()


# ---------------------------------------------------------------- T rules
def test_t1_uart_period_boundary():
    """Stop-bit sample: A = 9P from the anchor, slot end 10P.

    T1 needs ceil(d*10P) + QUANT + 1 < P//2. At d = 2%: P=10 -> 2+2 < 5 holds;
    P=9 -> 2+2 < 4 fails; P=8 -> 2+2 < 4 fails.
    At P=8 the follower is also late for the next start edge (T5): it finishes the
    stop bit at -P + P//2 + E = -1, then runs BR and ROLE_IF and starts at 2. With
    drift ceil(d*80) = 2 that is 4 > event 0 + E = 3. At P=9 it starts at 1: 3 <= 3.
    """
    assert diag_rules(uart_like(10)) == set()
    assert diag_rules(uart_like(9)) == {"T1-sample"}
    assert diag_rules(uart_like(8)) == {"T1-sample", "T5-watch"}
    assert diag_rules(uart_like(434)) == set()


def test_t1_rejects_unanchored_loop():
    d, ln, p = two_party()
    d.label("x")
    d.hold(ln, 1, owner="a", prof=p)
    d.br("x")
    assert "T1-unanchored" in diag_rules(d)


def test_t2_rejects_tight_cond_window():
    d, ln, p = two_party()
    tight = d.profile("TIGHT", nom=20, min=20, max=20)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.cond("rise", ln, owner="a", prof=tight)
    d.halt()
    assert diag_rules(d) == {"T2-window"}


def test_t3_rejects_spi_target_half_period_4():
    """Later bits need H >= EDGE_LAT + 1 + ceil(d*2H): H=4 -> 4 >= 5 fails."""
    assert "T3-latency" in diag_rules(spi_like(4))


def test_t3_accepts_6():
    """Bit 0 (cpha 0): the target starts at E+1 = 4 after the clock owner's tb, so
    H >= 4 + 1 + ceil(d*H) = 6; H=5 fails only the bit-0 bound."""
    assert diag_rules(spi_like(6)) == set()
    assert diag_rules(spi_like(5)) == {"T3-latency"}


def test_t4_rejects_min_gt_nom():
    d, ln, p = two_party()
    bad = d.profile("BAD", nom=10, min=12, max=14)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bad)
    d.halt()
    assert "T4-profile" in diag_rules(d)


def test_t4_rejects_hold_with_idle_profile():
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=None)
    d.halt()
    assert "T4-profile" in diag_rules(d)


def test_t5_rejects_long_follower_gap():
    """After the anchor, the owner is ready at 2 (event at max(nom=2, 2)); rx starts at
    E + 1 + 7 = 11 > 2 + E. MON skips the block and starts at 5, which is fine."""
    d = Description("t", parties=("tx", "rx"))
    ln = d.line("l", owner="tx", idle=1)
    soon = d.profile("SOON", nom=2, min=0, max=INF)
    d.cond("fall", ln, owner="tx")
    with d.only("rx"):
        for _ in range(6):
            d.nop()
    d.cond("rise", ln, owner="tx", prof=soon)
    d.halt()
    assert diag_rules(d) == {"T5-watch"}


def test_t5_rejects_owner_overrun():
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    with d.only("a"):
        d.nop()
        d.nop()
    d.hold(ln, 0, owner="a", prof=p)
    d.halt()
    assert diag_rules(d) == {"T5-overrun"}


def _with_br_to(d):
    """Lower `d` and turn its single BR into BR TO (the DSL has no name for TO until Task 7)."""
    from dataclasses import replace

    from emu.compiler import lower
    from emu.isa import BrCond, Op
    low = lower(d)
    pc = next(i for i, ins in enumerate(low.instrs) if ins.op == Op.BR)
    low.instrs[pc] = replace(low.instrs[pc], prof=int(BrCond.TO), pin=0)
    return low


def test_r2_rejects_br_to_outside_block():
    """isa-v0 §5 R2: BR TO is only legal inside a role block, whatever r0 holds."""
    from emu.analysis import check_branches
    d, ln, p = two_party()
    d.label("x")
    d.li("r0", 0)  # clean register: the taint check alone would accept this
    d.br("x", cond="z", reg="r0")
    d.halt()
    assert {x.rule for x in check_branches(d, _with_br_to(d))} == {"R2-branch"}


def test_r2_accepts_br_to_inside_block():
    from emu.analysis import check_branches
    d, ln, p = two_party()
    with d.only("a"):
        d.label("x")
        d.li("r0", 0)
        d.br("x", cond="z", reg="r0")
    d.halt()
    assert check_branches(d, _with_br_to(d)) == []


def test_t1_message_names_the_real_bound():
    """At 0 ppm a 4-cycle slot still fails: QUANT + 1 = 2 is not < 4//2. The message must say so."""
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=4))
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    (msg,) = [x.msg for x in e.value.diags if x.rule == "T1-sample"]
    assert "drift 0 + QUANT 1 + 1 = 2" in msg and "P//2 = 2" in msg


@pytest.mark.parametrize("P,ok", [(5, False), (6, True)])
def test_t1_shortest_hold_slot_at_zero_ppm(P, ok):
    """Even without drift a follower needs QUANT + 1 = 2 < P//2, so the shortest followed slot is 6."""
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=P))
    d.halt()
    assert (diag_rules(d) == set()) == ok


@pytest.mark.parametrize("ppm,ok", [(20000, False), (10000, True)])
def test_t1_sixteen_bit_unclocked_frame(ppm, ok):
    """P=16 after a 1P start bit: the last sample is at A + 16P = 272.
    2%: ceil(5.44) + QUANT + 1 = 8, not < 8. 1%: 3 + 2 = 5 < 8."""
    from descs import slack
    d = Description("t", parties=("a", "b"), tolerance_ppm=ppm)
    ln = d.line("l", owner="a", idle=1)
    s = slack(16, ppm)
    bit = d.profile("BIT", nom=16, min=16 - s, max=16 + s)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(16, ln, owner="a", prof=bit)
    d.halt()
    assert ("T1-sample" not in diag_rules(d)) == ok
