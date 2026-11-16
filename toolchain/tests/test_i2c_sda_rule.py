"""R1-bus on I2C (isa §5): every follower's detector takes an SDA edge with SCL high as a START or
STOP (§4.7), but only COND START/STOP are timed and tracked as one. So SDA changes only with SCL
low, except by COND START/STOP. Found by the review of the third mutation pass: a HOLD 1 or a COND
RISE on SDA right after the START compiled, and on the model the followers saw a STOP there."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.system import EngineSpec, System

from test_edge_rule import model_goes_wrong, runs_clean

SAYS = "only COND START/STOP change SDA"


def diags(d):
    try:
        compile_desc(d)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def said(d):
    return any(r == "R1-bus" and SAYS in m for r, m in diags(d))


def bus(d):
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    return sda, scl, x, lane, d.profile("HALF", nom=20, min=0, max=INF), d.profile("W", nom=40, min=0, max=INF)


def tail(d, sda, scl, lane, half, w):
    """A bit, then the STOP: SCL low, SDA low, SCL high, COND STOP."""
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H0", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()


def after_start(how):
    """START (SDA low, SCL high), then `how` on SDA with SCL still high."""
    d = Description("sh", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    if how == "HOLD 1":
        d.hold(sda, 1, owner="ctl", prof=d.profile("H1", nom=40))
    elif how == "COND RISE":
        d.cond("rise", sda, owner="ctl", prof=w)
    elif how == "RAW REL":
        with d.only("ctl"):
            d.raw_rel(sda)
        d.cond("fall", x, owner="ctl", prof=w)
    tail(d, sda, scl, lane, half, w)
    return d


@pytest.mark.parametrize("how", ["HOLD 1", "COND RISE", "RAW REL"])
def test_an_sda_rise_with_scl_high_is_rejected(how):
    """A STOP to the followers, which no COND STOP times."""
    assert said(after_start(how))
    assert model_goes_wrong(after_start(how))


def idle_fall(how):
    """From idle (both high), `how` takes SDA low with SCL high: a START no COND START made."""
    d = Description("sf", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    if how == "COND FALL":
        d.cond("fall", sda, owner="ctl", prof=w)
    elif how == "HOLD 0":
        d.cond("fall", x, owner="ctl", prof=w)
        d.hold(sda, 0, owner="ctl", prof=d.profile("H0s", nom=40))
    else:
        with d.only("ctl"):
            d.raw_set(sda, 0)
        d.cond("fall", x, owner="ctl", prof=w)
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("how", ["COND FALL", "HOLD 0", "RAW SET 0"])
def test_an_sda_fall_with_scl_high_is_rejected(how):
    assert said(idle_fall(how))
    img = compile_desc(idle_fall(how), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert [k for _, k in t.engines[1].bus_events][:1] == ["START"]     # the follower saw one


def test_sda_may_change_with_scl_low():
    """The STOP's setup: SCL low, then SDA low, then SCL high and the COND STOP."""
    d = Description("ok", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    tail(d, sda, scl, lane, half, w)
    runs_clean(d)


def test_a_drive_that_keeps_sda_at_its_level_is_no_edge():
    """After the START SDA is low: a HOLD 0 there, with SCL high, changes nothing."""
    d = Description("keep", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H0k", nom=40))
    tail(d, sda, scl, lane, half, w)
    runs_clean(d)


def test_sda_with_scl_low_on_only_some_paths_is_rejected():
    """One path drops SCL first, the other doesn't: where they meet, SCL may be high."""
    d = Description("sp", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.li("r3", 0)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.br("low", "z", "r3")
    d.cond("fall", x, owner="ctl", prof=w)
    d.br("j")
    d.label("low")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.label("j")
    d.hold(sda, 0, owner="ctl", prof=d.profile("H0j", nom=20))
    d.halt()
    assert said(d)


@pytest.mark.parametrize("lane_kind", ["unclocked", "cpha 0", "another clock", "clocked by SDA"])
def test_only_the_bus_lane_transfers_on_sda(lane_kind):
    """An unclocked XFER, or a lane with cpha 0 or another clock, changes SDA with SCL high; a lane
    clocked by SDA makes its edges there."""
    d = Description("ln", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    c2 = d.line("c2", owner="ctl", idle=1)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    if lane_kind == "unclocked":
        d.xfer(1, sda, owner="ctl", prof=d.profile("B", nom=20, min=18, max=22), src="ones")
    else:
        other = {"cpha 0": dict(data=sda, clk=scl, cpol=1, cpha=0),
                 "another clock": dict(data=sda, clk=c2, cpol=1, cpha=1),
                 "clocked by SDA": dict(data=x, clk=sda, cpol=1, cpha=1)}[lane_kind]
        m = d.lane("M", other.pop("data"), **other)
        d.xfer(1, m, owner="ctl", prof=half, src="ones")
    d.halt()
    assert said(d)
