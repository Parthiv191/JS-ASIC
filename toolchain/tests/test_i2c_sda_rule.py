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


def said(d, what=""):
    """R1-bus's SDA message, naming `what` (the op) if given."""
    return any(r == "R1-bus" and SAYS in m and (not what or f"{what} on SDA" in m) for r, m in diags(d))


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
    else:
        with d.only("ctl"):
            if how == "RAW REL":
                d.raw_rel(sda)
            else:
                d.raw_set(sda, 1)
        d.cond("fall", x, owner="ctl", prof=w)
    tail(d, sda, scl, lane, half, w)
    return d


@pytest.mark.parametrize("how", ["HOLD 1", "COND RISE", "RAW REL", "RAW SET 1"])
def test_an_sda_rise_with_scl_high_is_rejected(how):
    """A STOP to the followers, which no COND STOP times."""
    assert said(after_start(how), how)
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
    assert said(idle_fall(how), how)
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


# ---- the review of this rule: HALT, and an XFER with SCL as its data
def halts(who):
    """The target halts after its ACK (SDA low, SCL high at its end), or the controller right
    after the START: HALT releases SDA at once (isa §4.1), a STOP."""
    d = Description("th", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    if who == "ctl":
        d.halt()
        return d
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    d.xfer(1, lane, owner="tgt", prof=half, src="zero")
    with d.only("tgt"):
        d.halt()
    d.xfer(1, lane, owner="ctl", prof=d.profile("H40", nom=40, min=0, max=INF), src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("who", ["tgt", "ctl"])
def test_a_halt_that_releases_sda_with_scl_high_is_rejected(who):
    assert any(r == "R1-bus" and "HALT by" in m for r, m in diags(halts(who)))
    img = compile_desc(halts(who), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    stops = [at for at, k in t.engines[2].bus_events if k == "STOP"]
    assert stops and (who == "ctl" or stops[0] < max(t.wire_edges[img.bus.a]))   # before the program's own


def scl_as_data():
    """The controller's byte (SDA's let-go held, ldelay 12), then an unclocked 2-bit XFER of 0b10
    with SCL as its data line: SCL falls, taking the let-go, and rises 10 later, before the
    release (found by the review of this rule; a 9-clock bus recovery is written this way)."""
    d = Description("sd", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=12)
    u = d.lane("U", scl)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.li("r1", 0xB4)
    d.li("r5", 0b10)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=d.profile("HALF", nom=20, min=0, max=INF), src="r1", msb_first=True)
    d.cond("fall", x, owner="ctl", prof=w)
    d.xfer(2, u, owner="ctl", prof=d.profile("P", nom=10, min=9, max=11), src="r5")
    d.cond("rise", x, owner="ctl", prof=w)
    d.halt()
    return d


def test_an_xfer_with_scl_as_its_data_makes_edges_the_walk_cant_pin_down():
    """So with a let-go on SDA it is rejected; on the model the followers see a STOP (the program
    makes none)."""
    assert any("may or may not have an edge" in m for _, m in diags(scl_as_data()))
    img = compile_desc(scl_as_data(), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert "STOP" in [k for _, k in t.engines[2].bus_events]


def test_a_halt_after_its_own_byte_from_a_register_is_rejected():
    """The target's byte from r5 leaves its let-go of an unknown bit with SCL high: its HALT may
    release SDA then."""
    d = Description("hb", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    with d.only("tgt"):
        d.li("r5", 0x3C)
    d.xfer(8, lane, owner="tgt", prof=half, src="r5", msb_first=True)
    with d.only("tgt"):
        d.halt()
    d.halt()
    assert any(r == "R1-bus" and "HALT by P1" in m for r, m in diags(d))


def test_a_halt_by_a_party_that_released_sda_is_no_edge():
    """The target never drove SDA; it halts while the controller's let-go (a byte from r1) holds
    SDA at an unknown bit with SCL high. Its HALT changes nothing; the controller then drops SCL,
    which takes its let-go, and ends with a STOP."""
    d = Description("hr", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    with d.only("ctl"):
        d.li("r1", 0xB4)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="r1", msb_first=True)
    with d.only("tgt"):
        d.halt()
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H0", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    assert diags(d) == []
    runs_clean(d)


def test_a_halt_while_another_party_holds_sda_low_too_is_no_edge():
    """After the START the target pulls SDA low as well (no edge: it is low); the controller halts.
    Its release leaves SDA low, so no STOP. The target then drops SCL, lets SDA go with SCL low,
    lets SCL go and halts in its own block (a HALT both run would count the controller as still
    there: the walk doesn't know its block always halts); MON's HALT ends the program."""
    d = Description("hl", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    y = d.line("y", owner="tgt", idle=1)
    d.cond("start", owner="ctl", prof=w)
    d.cond("fall", y, owner="tgt", prof=w)      # a HOLD may not start late after the START
    d.hold(sda, 0, owner="tgt", prof=d.profile("T0", nom=40))
    with d.only("ctl"):
        d.halt()
    d.cond("fall", scl, owner="tgt", prof=w)
    with d.only("tgt"):
        d.raw_rel(sda)     # (a HOLD 1 would need the controller known released: R2-edge)
    d.cond("rise", scl, owner="tgt", prof=w)
    with d.only("tgt"):
        d.halt()
    d.halt()
    assert diags(d) == []
    runs_clean(d)


def target_holds_sda_with_scl_low(d):
    """START, the controller's byte, SCL low, then the target pulls SDA low."""
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("fall", sda, owner="tgt", prof=d.profile("C", nom=10, min=0, max=INF))
    return sda, scl, x, lane, half, w


def halts_in_its_own_block():
    """The target halts in its own block with SCL low (its release is no edge then), but for the
    controller, which skips that block, it may still hold SDA low: the controller's HOLD 0 and
    HOLD 1 with SCL high are a START and a STOP (found by the re-review)."""
    d = Description("h1", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = target_holds_sda_with_scl_low(d)
    with d.only("tgt"):
        d.halt()
    d.cond("rise", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("A", nom=40))
    d.hold(sda, 1, owner="ctl", prof=d.profile("B", nom=40))
    d.cond("fall", x, owner="ctl", prof=w)
    with d.only("ctl"):
        d.halt()
    d.halt()
    return d


def both_halt():
    """Both halt in one block: the controller may let SCL go first, then the target SDA, a STOP
    (found by the re-review)."""
    d = Description("h2", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = target_holds_sda_with_scl_low(d)
    d.cond("fall", x, owner="ctl", prof=w)
    with d.only("ctl", "tgt"):
        d.halt()
    with d.only("mon"):
        d.raw_delay(200)
    d.halt()
    return d


@pytest.mark.parametrize("build", [halts_in_its_own_block, both_halt], ids=["one halts in its block", "both halt"])
def test_a_halt_leaves_the_others_walk_its_lines_released_or_not(build):
    assert said(build(), "HOLD 0") if build is halts_in_its_own_block else any(
        r == "R1-bus" and "HALT by P0 and P1" in m for r, m in diags(build()))
    img = compile_desc(build(), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert "STOP" in [k for _, k in t.engines[2].bus_events]       # MON saw one; the program makes none


def test_a_block_without_a_halt_leaves_the_skipped_partys_drive_known():
    """After the START the target pulls SDA low too, then runs a block of its own with no HALT.
    For the controller it still holds SDA low, so the controller's RAW REL of SDA with SCL high
    changes nothing. Then SCL low, the target lets go (SDA rises with SCL low), SCL high, and each
    halts in its own block. (Each RAW is in its party's own block, so the other's walk can't see it:
    neither could then release SDA with a HOLD 1, nor the controller STOP, which needs the target
    known released.)"""
    d = Description("nb", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    y = d.line("y", owner="tgt", idle=1)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    d.cond("fall", y, owner="tgt", prof=w)
    d.hold(sda, 0, owner="tgt", prof=d.profile("T0", nom=40))
    with d.only("tgt"):
        d.nop()
    with d.only("ctl"):
        d.raw_rel(sda)         # (a HOLD 1 would be followed and checked at 1: LEVEL, as SDA stays low)
    d.cond("fall", x, owner="ctl", prof=w)
    d.cond("fall", scl, owner="ctl", prof=w)
    with d.only("tgt"):
        d.raw_rel(sda)
    d.cond("rise", scl, owner="ctl", prof=w)
    with d.only("ctl"):
        d.halt()
    with d.only("tgt"):
        d.halt()
    d.halt()
    assert diags(d) == []
    runs_clean(d)


# ---- found by the random I2C programs (emu/gen's bus family)
def scl_after(bus_cond, raw, spare=False):
    """A START (and, for a STOP, a bit and the STOP), then at once, in the controller's own block,
    `raw` on SCL ("SET 0", "REL"); with `spare`, a COND on x first. The followers confirm a START or
    STOP only if SCL stays high for the confirm window (isa §4.7)."""
    d = Description("sc", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    if bus_cond == "stop":
        d.xfer(1, lane, owner="ctl", prof=half, src="zero")
        d.cond("stop", owner="ctl", prof=w)
    if spare:
        d.cond("fall", x, owner="ctl", prof=w)
    with d.only("ctl"):
        if raw == "SET 0":
            d.raw_set(scl, 0)
        else:
            d.raw_rel(scl)
    if bus_cond == "start":   # then SCL up again, a bit and the STOP
        with d.only("ctl"):
            d.raw_rel(scl)
        d.xfer(1, lane, owner="ctl", prof=half, src="zero")
        d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


def hangs(d):
    img = compile_desc(d, check_levels=False, check_timing=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    return not all(et.halted for et in t.engines)


@pytest.mark.parametrize("bus_cond", ["start", "stop"])
def test_scl_low_by_raw_before_the_followers_confirm_a_start_or_stop_is_rejected(bus_cond):
    """The controller finishes its COND START/STOP at the edge, so its RAW SET 0 on SCL comes in
    the confirm window: the followers drop the candidate and wait for it forever."""
    d = scl_after(bus_cond, "SET 0")
    assert any(r == "R1-bus" and f"RAW SET 0 on SCL (scl) by P0 comes before the followers confirm its "
               f"COND {bus_cond.upper()}" in m for r, m in diags(d))
    assert hangs(d)


def test_scl_released_after_a_stop_is_no_change():
    assert not any("before the followers confirm" in m for _, m in diags(scl_after("stop", "REL")))


def test_scl_low_by_raw_after_the_next_timed_step_is_legal():
    """T5-watch has the followers start the COND on x before its edge, so after their confirm."""
    d = scl_after("stop", "SET 0", spare=True)
    assert not any("before the followers confirm" in m for _, m in diags(d))
    assert not hangs(d)
