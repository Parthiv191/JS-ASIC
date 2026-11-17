"""An XFER on an OD data line needs everyone else released (R2-edge, isa §5): the line is a wired
AND, so where another party holds it low the sender's 1 bits read 0, in every role alike. Found
by the random I2C programs (emu/gen's bus family): the controller's COND START left SDA low, the
target sent a byte from its FIFO, and every role read 0, with no violation; only the `value`
oracle saw it."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.system import EngineSpec, System

from test_edge_rule import runs_clean

SAYS = "another party may hold it low here, so its 1 bits read 0"


def diags(d):
    try:
        compile_desc(d)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def flagged(d, who="P1"):
    return any(r == "R2-edge" and f"XFER by {who} on sda: {SAYS}" in m for r, m in diags(d))


def values(d):
    """MON's XFER values on the model, with the level and timing walks off."""
    img = compile_desc(d, check_levels=False, check_timing=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    return [r.value for r in t.engines[2].records if r.op == "XFER"]


def i2c(after_start, src="ones"):
    """START, then `after_start` (what the controller does to SDA before the target's byte), the
    target's 8 bits from `src`, the controller's ZERO bit and the STOP."""
    d = Description("ox", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.cond("start", owner="ctl", prof=w)
    if after_start == "byte":       # the controller's address byte: its let-go goes at the next edge
        d.xfer(8, lane, owner="ctl", prof=half, src="zero")
    elif after_start == "hold 0":   # SCL low, SDA held low by a HOLD, SCL high
        d.cond("fall", scl, owner="ctl", prof=w)
        d.hold(sda, 0, owner="ctl", prof=d.profile("H0", nom=20))
        d.cond("rise", scl, owner="ctl", prof=w)
    elif after_start == "released":  # SCL low, SDA released, SCL high: a repeated START's setup
        d.cond("fall", scl, owner="ctl", prof=w)
        with d.only("ctl"):
            d.raw_rel(sda)
        d.cond("rise", scl, owner="ctl", prof=w)
        d.cond("start", owner="ctl", prof=w)
        d.xfer(8, lane, owner="ctl", prof=half, src="zero")
    d.xfer(8, lane, owner="tgt", prof=half, src=src)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("after_start", ["nothing", "hold 0"])
def test_an_xfer_while_another_party_holds_the_od_line_low_is_rejected(after_start):
    """The START's low, or a HOLD 0's, is a plain drive: nothing releases it, and the target's
    ONES read 0 in every role."""
    assert flagged(i2c(after_start))
    assert values(i2c(after_start))[-2:] == [0, 0]


@pytest.mark.parametrize("after_start", ["byte", "released"])
def test_an_xfer_after_the_other_partys_let_go_or_release_is_legal(after_start):
    """The controller's byte leaves a let-go, which the target's first SCL edge releases before
    bit 0 is captured (as T4-profile gives the lane); a RAW REL releases SDA at once."""
    assert not flagged(i2c(after_start))
    runs_clean(i2c(after_start))
    assert values(i2c(after_start))[-2:] == [0xFF, 0]


def test_an_xfer_from_zero_is_legal_whatever_the_others_drive():
    """All its bits are 0, which the wired AND reads right."""
    assert not flagged(i2c("nothing", src="zero"))


def test_a_shared_xfer_is_left_to_arb():
    """Both parties send a SHARED XFER (arbitration, isa §6 ARB, open): not this check."""
    d = Description("sx", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    c = d.line("c", owner="a", idle=0)
    lane = d.lane("L", s, clk=c, cpol=0, cpha=1)
    d.cond("fall", s, owner="b", prof=d.profile("W", nom=40, min=0, max=INF))
    d.xfer(4, lane, owner="shared", prof=d.profile("HALF", nom=20, min=0, max=INF), src="ones")
    d.halt()
    assert not any("XFER by" in m and SAYS in m for _, m in diags(d))


def point_to_point(first, cpha=1):
    """A shared OD line clocked by a's push-pull line: a's 4 bits from `first` ("cond": a COND FALL
    by b instead), then b's 4 bits of ONES."""
    d = Description("pp", parties=("a", "b"))
    s = d.line("s", "od", owner="shared", idle=1)
    c = d.line("c", owner="a", idle=0)
    lane = d.lane("L", s, clk=c, cpol=0, cpha=cpha, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    d.nop()
    if first == "cond":
        d.cond("fall", s, owner="a", prof=d.profile("W", nom=40, min=0, max=INF))
    else:
        d.xfer(4, lane, owner="a", prof=half, src=first)
    d.xfer(4, lane, owner="b", prof=half, src="ones")
    d.halt()
    return d


@pytest.mark.parametrize("cpha", [0, 1])
def test_the_other_partys_let_go_counts_as_released_on_any_clocked_lane(cpha):
    """On the model a let-go held at 0 is gone before the next sender's bit 0 is captured, with
    cpha 0 as with 1."""
    d = point_to_point("zero", cpha)
    assert diags(d) == []
    assert values(d) == [0, 0xF]


def test_a_cond_fall_by_the_other_party_is_a_plain_low():
    d = point_to_point("cond")
    assert any(r == "R2-edge" and f"XFER by P1 on s: {SAYS}" in m for r, m in diags(d))
    assert values(d) == [0]


def after_lead(ack_path_low):
    """The controller reads the target's ACK in a lead block: on a NACK it STOPs and starts over;
    on an ACK it falls through, with `ack_path_low` holding SDA low there by RAWs (SCL low, SDA
    low, SCL up). Then the target's byte, the block's successor."""
    d = Description("lx", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.abort_to("frame")
    d.label("frame")
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="zero", dst="r4", abort=True)
    with d.lead("ctl"):
        d.br("nack", cond="nz", reg="r4")
        if ack_path_low:
            d.raw_set(scl, 0)
            d.raw_set(sda, 0)
            d.raw_rel(scl)
        d.br("ok")
        d.label("nack")
        d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)   # SDA low: the STOP's ZERO bit
        d.cond("stop", owner="ctl", prof=w)
        d.br("frame")
    d.label("ok")
    d.xfer(8, lane, owner="tgt", prof=half, src="ones", abort=True)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


def test_a_lead_blocks_exit_paths_drive_nothing_at_its_successor():
    """The target skips the controller's lead block and sends its byte, the block's successor. The
    NACK path's ZERO bit holds SDA low, but that path STOPs, a bus exit for the target: at a
    successor that completes, the controller came by the fall-through, which drives nothing. (The
    library register read, i2c_reg, is this; before, every drive in a skipped block counted.)"""
    assert diags(after_lead(False)) == []
    runs_clean(after_lead(False))


def test_a_drive_on_a_lead_blocks_fall_through_path_still_counts():
    assert any(r == "R2-edge" and f"XFER by P1 on sda: {SAYS}" in m for r, m in diags(after_lead(True)))


def test_a_lead_block_whose_successor_is_the_leaders_own_cond_is_paced_too():
    """S is the controller's COND FALL on x, unclocked: the target waits in it for the controller's
    edge, which no exit path makes (R3-lead), so the NACK exit's ZERO bit doesn't count at the
    target's byte after S."""
    d = Description("lc", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.abort_to("frame")
    d.label("frame")
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="zero", dst="r4", abort=True)
    with d.lead("ctl"):
        d.br("ok", cond="z", reg="r4")
        d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)
        d.cond("stop", owner="ctl", prof=w)
        d.br("frame")
    d.label("ok")
    d.cond("fall", x, owner="ctl", prof=w, abort=True)
    d.xfer(8, lane, owner="tgt", prof=half, src="ones", abort=True)
    d.cond("rise", x, owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero", abort=True)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    assert diags(d) == []
    runs_clean(d)
