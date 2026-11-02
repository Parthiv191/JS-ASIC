import math

import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF


def diag_rules(desc, check_timing=True):
    try:
        compile_desc(desc, check_timing=check_timing)
    except CompileError as e:
        return {d.rule for d in e.diags}
    return set()


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
