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
        if raw == "REL":
            d.raw_rel(scl)
        else:
            d.raw_set(scl, int(raw[-1]))
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


@pytest.mark.parametrize("raw", ["REL", "SET 1"])
def test_scl_released_after_a_stop_is_no_change(raw):
    assert not any("before the followers confirm" in m for _, m in diags(scl_after("stop", raw)))
    assert not hangs(scl_after("stop", raw))


def test_an_untimed_loop_after_a_stop_ends_the_slot_walk():
    """The walk over a bus COND's slot follows untimed instructions until a timed step: around a
    loop of them it must still end (the program is rejected for its untimed loop, isa §6)."""
    import threading
    d = Description("ul", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.label("L")
    d.nop()
    d.br("L")
    done = []
    th = threading.Thread(target=lambda: done.append(diags(d)), daemon=True)
    th.start()
    th.join(30)
    assert done, "the slot walk didn't end"


def test_scl_low_by_raw_after_the_next_timed_step_is_legal():
    """T5-watch has the followers start the COND on x before its edge, so after their confirm."""
    d = scl_after("stop", "SET 0", spare=True)
    assert not any("before the followers confirm" in m for _, m in diags(d))
    assert not hangs(d)


def hold_scl_after(bus_cond):
    """A START (or a bit and a STOP), then at once a HOLD 0 on SCL: a HOLD drives when it starts,
    ISSUE after the edge, and T5-watch times only its sample (found by the review)."""
    d = Description("hs", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    if bus_cond == "stop":
        d.xfer(1, lane, owner="ctl", prof=half, src="zero")
        d.cond("stop", owner="ctl", prof=w)
    d.hold(scl, 0, owner="ctl", prof=d.profile("H0", nom=60))
    d.hold(scl, 1, owner="ctl", prof=d.profile("H1", nom=60))
    if bus_cond == "start":
        d.xfer(1, lane, owner="ctl", prof=half, src="zero")
        d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("bus_cond", ["start", "stop"])
def test_a_hold_0_on_scl_right_after_a_start_or_stop_is_rejected(bus_cond):
    assert any(r == "R1-bus" and f"HOLD 0 on SCL (scl) by P0 comes before the followers confirm its "
               f"COND {bus_cond.upper()}" in m for r, m in diags(hold_scl_after(bus_cond)))
    assert hangs(hold_scl_after(bus_cond))


def lead_after_start(nom):
    """A START, then the controller's lead block, whose exit path first takes SCL low by a COND
    FALL of `nom`: nobody follows a lead block's steps, so T5-watch doesn't time it. The exit
    STOPs and retries; S, the controller's byte, is the fall-through (found by the review)."""
    d = Description("ls", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    with d.only("ctl"):
        d.li("r5", 1)
    d.abort_to("frame")
    d.label("frame")
    d.cond("start", owner="ctl", prof=w)
    with d.lead("ctl"):
        d.br("go", cond="z", reg="r5")
        d.li("r5", 0)
        d.cond("fall", scl, owner="ctl", prof=d.profile("S", nom=nom, min=0, max=INF))
        d.cond("rise", scl, owner="ctl", prof=w)
        d.cond("stop", owner="ctl", prof=w)
        d.br("frame")
    d.label("go")
    d.xfer(8, lane, owner="ctl", prof=d.profile("H30", nom=30, min=0, max=INF), src="zero", abort=True)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


def test_a_lead_blocks_scl_fall_in_a_starts_confirm_window_is_rejected():
    flagged = lambda d: any(r == "R1-bus" and "COND FALL on SCL (scl) by P0 in a lead block" in m for r, m in diags(d))
    assert flagged(lead_after_start(4))
    img = compile_desc(lead_after_start(4), check_levels=False, check_timing=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert [k for _, k in t.engines[1].bus_events][0] != "START"   # the target dropped the first START
    assert not flagged(lead_after_start(40))                       # its edge 40 later: after the confirm


def raw_delay_then_scl_low(n, ppm):
    d = Description("rb", parties=("ctl", "tgt"), tolerance_ppm=ppm)
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    with d.only("ctl"):
        if n:
            d.raw_delay(n)
        d.raw_set(scl, 0)
        d.raw_rel(scl)
    d.cond("fall", x, owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("ppm", [0, 20000])
def test_the_doers_time_counts_toward_the_confirm_window(ppm):
    """The doer's time from the edge counts: U per untimed instruction and RAW DELAY. With the
    default confirm of 15, a RAW SET 0 on SCL after RAW DELAY 15 or more is accepted, and runs
    clean at the tolerance corners; on the model the followers hang up to RAW DELAY 10 (the
    compiler leaves out ISSUE, so it is 4 cycles safe)."""
    lo = 1000 - ppm // 1000
    corners = [((1, 1),) * 3, ((1000, 1000), (lo, 1000), (lo, 1000)), ((lo, 1000), (1000, 1000), (1000, 1000))]

    def hangs_at_a_corner(d):
        img = compile_desc(d, check_levels=False, check_timing=False)
        return any(not all(et.halted for et in System([EngineSpec(img, r, clock=c) for r, c in
                                                       zip((Role.P0, Role.P1, Role.MON), cs)]).run(50_000).engines)
                   for cs in corners)
    for n in (0, 10, 14):
        assert any(r == "R1-bus" for r, _ in diags(raw_delay_then_scl_low(n, ppm)))
    assert hangs_at_a_corner(raw_delay_then_scl_low(10, ppm))
    for n in (15, 16, 40):
        assert diags(raw_delay_then_scl_low(n, ppm)) == []
        assert not hangs_at_a_corner(raw_delay_then_scl_low(n, ppm))


# ---- the confirm window's bound, at its edges (the mutation run of this rule)
CONF = 100   # at 2%: need = CONF + 1 + QUANT + drift(40000, CONF + 1 + EDGE_LAT) = 107 of the doer's cycles


def window(body, lead=False, confirm=CONF):
    """A START, then `body(d, sda, scl, x, lane, half)`: in line (`lead` False), or on the exit
    path of the controller's lead block, which starts 6 cycles after the edge (ROLE_IF, BR, LI);
    only the confirm message matters here."""
    d = Description("cw", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl, confirm=confirm)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    if not lead:
        d.cond("start", owner="ctl", prof=w)
        body(d, sda, scl, x, lane, half)
        d.halt()
        return d
    with d.only("ctl"):
        d.li("r5", 1)
    d.abort_to("frame")
    d.label("frame")
    d.cond("start", owner="ctl", prof=w)
    with d.lead("ctl"):
        d.br("go", cond="z", reg="r5")
        d.li("r5", 0)
        body(d, sda, scl, x, lane, half)
        d.cond("rise", scl, owner="ctl", prof=w)
        d.cond("stop", owner="ctl", prof=w)
        d.br("frame")
    d.label("go")
    d.xfer(8, lane, owner="ctl", prof=d.profile("H300", nom=300, min=0, max=INF), src="zero", abort=True)
    d.halt()
    return d


def confirm_flag(d, what):
    return any(r == "R1-bus" and f"{what} on SCL (scl) by P0" in m and "before the followers confirm" in m
               for r, m in diags(d))


def prof(d, nom):
    return d.profile(f"N{nom}", nom=nom, min=0, max=INF)


@pytest.mark.parametrize("nom,bad", [(112, True), (113, False)])
def test_a_lead_cond_fall_on_scl_counts_its_nom_less_drift(nom, bad):
    """A COND acts at tb + nom (its start, at 6, if later): nom - drift(40000, nom) - QUANT against
    107. The untimed instructions before it don't add to its nom (found by the re-review)."""
    d = window(lambda d, sda, scl, x, lane, half: d.cond("fall", scl, owner="ctl", prof=prof(d, nom)), lead=True)
    assert confirm_flag(d, "COND FALL") == bad


def test_a_lead_cond_fall_of_nom_1_counts_nothing():
    """After RAW DELAY 100 the COND comes at 106; its nom 1 counts as 0, so its edge is at 106."""
    def body(d, sda, scl, x, lane, half):
        d.raw_delay(100)
        d.cond("fall", scl, owner="ctl", prof=prof(d, 1))
    assert confirm_flag(window(body, lead=True), "COND FALL")


@pytest.mark.parametrize("H,bad", [(56, True), (57, False)])
def test_a_lead_xfer_on_another_lane_counts_2nh(H, bad):
    """A bit on lane M (y, c), then a COND FALL of nom 1 on SCL: the XFER ends at tb + 2H (less
    drift and QUANT), against 107."""
    def body(d, sda, scl, x, lane, half):
        y = d.line("y", owner="ctl", idle=0)
        c = d.line("c", owner="ctl", idle=0)
        m = d.lane("M", y, clk=c)
        d.xfer(1, m, owner="ctl", prof=prof(d, H), src="zero")
        d.cond("fall", scl, owner="ctl", prof=prof(d, 1))
    assert confirm_flag(window(body, lead=True), "COND FALL") == bad


@pytest.mark.parametrize("H,bad", [(112, True), (113, False)])
def test_a_lead_clocked_xfer_on_scl_falls_after_h(H, bad):
    """Its first leading edge at tb + H, less drift and QUANT."""
    d = window(lambda d, sda, scl, x, lane, half: d.xfer(1, lane, owner="ctl", prof=prof(d, H), src="zero"), lead=True)
    assert confirm_flag(d, "a clocked XFER") == bad


def test_a_lead_cond_on_another_line_is_no_scl_fall():
    d = window(lambda d, sda, scl, x, lane, half: d.cond("fall", x, owner="ctl", prof=prof(d, 4)), lead=True)
    assert not any("before the followers confirm" in m for _, m in diags(d))


@pytest.mark.parametrize("src,bad", [("zero", True), ("ones", False)])
def test_an_unclocked_xfer_on_scl_drives_at_its_start(src, bad):
    """After RAW DELAY 104 (106 with its ROLE_IF), bit 0 at once: 106 < 107. From ONES, SCL stays up."""
    def body(d, sda, scl, x, lane, half):
        with d.only("ctl"):
            d.raw_delay(104)
        d.xfer(1, scl, owner="ctl", prof=d.profile("B", nom=60, min=50, max=70), src=src)
    assert confirm_flag(window(body), "an XFER") == bad


@pytest.mark.parametrize("level,bad", [(0, True), (1, False)])
def test_a_hold_on_scl_drives_at_its_start(level, bad):
    """After RAW DELAY 104 (106 with its ROLE_IF): a HOLD 0 falls at 106; a HOLD 1 is no change."""
    def body(d, sda, scl, x, lane, half):
        with d.only("ctl"):
            d.raw_delay(104)
        d.hold(scl, level, owner="ctl", prof=d.profile("H", nom=60))
    assert any("before the followers confirm" in m for _, m in diags(window(body))) == bad


def test_only_raw_delay_counts_as_a_delay():
    """An LI of 0x3FFF has DELAY's sub field (3) and 0xFFF in its operand: it still takes U."""
    def body(d, sda, scl, x, lane, half):
        with d.only("ctl"):
            d.li("r5", 0x3FFF)
            d.raw_set(scl, 0)
    assert confirm_flag(window(body), "RAW SET 0")


def test_a_lead_hold_moves_the_anchor_not_the_doers_time():
    """A lead COND on x, then a HOLD 1 on x of 50: its owner finishes it at its start, so the RAW
    SET 0 on SCL after it comes some 6 cycles after the START, not 55, inside the default confirm
    window (found by the re-review)."""
    def body(d, sda, scl, x, lane, half):
        d.cond("fall", x, owner="ctl", prof=prof(d, 4))
        d.hold(x, 1, owner="ctl", prof=d.profile("H50", nom=50))
        d.raw_set(scl, 0)
    assert confirm_flag(window(body, lead=True, confirm=None), "RAW SET 0")


@pytest.mark.parametrize("how", ["HOLD 0", "an XFER"])
@pytest.mark.parametrize("nom,bad", [(112, True), (113, False)])
def test_a_lead_hold_moves_the_anchor_by_its_nom(how, nom, bad):
    """A lead HOLD 1 on x of `nom`, then a HOLD 0 or an unclocked XFER on SCL, which drive at
    their start, max(t, tb): tb is nom - drift - QUANT, against 107."""
    def body(d, sda, scl, x, lane, half):
        d.hold(x, 1, owner="ctl", prof=d.profile(f"H{nom}", nom=nom))
        if how == "HOLD 0":
            d.hold(scl, 0, owner="ctl", prof=d.profile("H20", nom=20))
        else:
            d.xfer(1, scl, owner="ctl", prof=d.profile("B", nom=60, min=50, max=70), src="zero")
    assert confirm_flag(window(body, lead=True), how) == bad


def test_a_lead_cond_on_another_line_moves_the_time_too():
    """A lead COND on x of nom 120 acts at tb + 114, past 107: the RAW SET 0 on SCL after it is late
    enough."""
    def body(d, sda, scl, x, lane, half):
        d.cond("fall", x, owner="ctl", prof=prof(d, 120))
        d.raw_set(scl, 0)
    assert not any("before the followers confirm" in m for _, m in diags(window(body, lead=True)))


@pytest.mark.parametrize("confirm,nom,bad", [(97, 109, True), (97, 110, False), (96, 107, True), (96, 108, False)])
def test_need_is_the_confirm_window_plus_drift_over_it(confirm, nom, bad):
    """need = confirm + 1 + QUANT + drift(40000, confirm + 1 + EDGE_LAT): 104 at 97 (drift over 101
    is 5), 102 at 96 (over 100, 4); a lead COND FALL on SCL comes at nom - drift - QUANT."""
    d = window(lambda d, sda, scl, x, lane, half: d.cond("fall", scl, owner="ctl", prof=prof(d, nom)),
               lead=True, confirm=confirm)
    assert confirm_flag(d, "COND FALL") == bad


@pytest.mark.parametrize("delay_first", [False, True])
def test_two_paths_into_a_pc_keep_both_bounds(delay_first):
    """One path HOLDs x for 150 (t stays at 10, tb goes to 143), the other waits 90 (t 98, tb 0).
    After RAW DELAY 10 the first path's RAW SET 0 on SCL comes at 20, the second's past 107: the
    walk must keep both pairs, in either order."""
    def body(d, sda, scl, x, lane, half):
        d.br("b2", cond="nz", reg="r6")
        if delay_first:
            d.raw_delay(90)
        else:
            d.hold(x, 1, owner="ctl", prof=d.profile("H150", nom=150))
        d.br("j")
        d.label("b2")
        if delay_first:
            d.hold(x, 1, owner="ctl", prof=d.profile("H150", nom=150))
        else:
            d.raw_delay(90)
        d.label("j")
        d.raw_delay(10)
        d.raw_set(scl, 0)
    assert confirm_flag(window(body, lead=True), "RAW SET 0")



# ---- the third review
def own_step_then_scl_low(step):
    """A START, then the controller's own followed `step` (it finishes a HOLD at its start, an
    unclocked XFER at its last bit), then at once a RAW SET 0 on SCL in its block."""
    d = Description("os", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.nop()
    d.cond("start", owner="ctl", prof=w)
    if step == "hold sda":
        d.hold(sda, 0, owner="ctl", prof=d.profile("H80", nom=80))
    elif step == "hold x":
        d.hold(x, 1, owner="ctl", prof=d.profile("H80", nom=80))
    else:
        d.xfer(1, x, owner="ctl", prof=d.profile("B", nom=40, min=36, max=44), src="ones")
    with d.only("ctl"):
        d.raw_set(scl, 0)
    d.cond("fall", x, owner="ctl", prof=w)
    with d.only("ctl"):
        d.raw_rel(scl)
    d.cond("rise", x, owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("step", ["hold sda", "hold x", "xfer x"])
def test_the_doers_own_hold_or_unclocked_xfer_doesnt_end_the_window(step):
    """The others are in its step until its sample; the doer is past it at once (found by the third
    review: on the model the target and MON hang at the START)."""
    assert confirm_flag(own_step_then_scl_low(step), "RAW SET 0")
    assert hangs(own_step_then_scl_low(step))


@pytest.mark.parametrize("n,P,bad", [(1, 20, True), (1, 40, True), (8, 10, False)])
def test_a_lead_unclocked_xfer_ends_at_its_last_bit(n, P, bad):
    """On a lead exit path, an unclocked XFER on z (nobody follows it), then an SCL pulse: its owner
    is past it at tb + (n - 1) P, and tb moves by n P (8 bits of 10: 80, past the window)."""
    def body(d, sda, scl, x, lane, half):
        z = d.line("z", owner="ctl", idle=1)
        d.xfer(n, z, owner="ctl", prof=d.profile(f"P{P}", nom=P, min=P - 2, max=P + 2), src="ones")
        d.raw_set(scl, 0)
        d.raw_delay(2)
        d.raw_rel(scl)
    assert confirm_flag(window(body, lead=True, confirm=None), "RAW SET 0") == bad


@pytest.mark.parametrize("P,bad", [(112, True), (113, False)])
def test_a_lead_2_bit_xfer_ends_one_bit_on(P, bad):
    """2 bits of P on z: its owner is past it at tb + P (less drift and QUANT), against 107."""
    def body(d, sda, scl, x, lane, half):
        z = d.line("z", owner="ctl", idle=1)
        d.xfer(2, z, owner="ctl", prof=d.profile(f"P{P}", nom=P, min=P - 2, max=P + 2), src="ones")
        d.raw_set(scl, 0)
    assert confirm_flag(window(body, lead=True), "RAW SET 0") == bad


def test_a_lead_unclocked_xfer_moves_the_anchor_by_its_bits():
    """A bit of 60 on z (tb to 56), then a COND FALL on SCL of 60: it acts at 56 + 56 = 112, past 107."""
    def body(d, sda, scl, x, lane, half):
        z = d.line("z", owner="ctl", idle=1)
        d.xfer(1, z, owner="ctl", prof=d.profile("P60", nom=60, min=58, max=62), src="ones")
        d.cond("fall", scl, owner="ctl", prof=prof(d, 60))
    assert not any("before the followers confirm" in m for _, m in diags(window(body, lead=True)))


def test_the_doers_followed_clocked_xfer_ends_the_window():
    """T5-watch has the followers in it by its first edge, so after their confirm: a RAW after it is
    left to that (here T5-watch rejects the short first bit itself)."""
    d = Description("cx", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = bus(d)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=d.profile("H6", nom=6, min=0, max=INF), src="zero")
    with d.only("ctl"):
        d.raw_set(scl, 0)
        d.raw_rel(scl)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    assert not any("before the followers confirm" in m for _, m in diags(d))


def halt_after_scl_fall(wait: bool = False):
    """c's address byte, t's ACK from ZERO (its let-go holds SDA to the next SCL fall + ldelay),
    c's COND FALL on SCL, then (with `wait`, after its COND FALL on its own line y, 40 cycles on)
    c halts in its block, which releases SCL."""
    from emu.desc import Description
    from emu.isa import INF
    d = Description("hs", parties=("c", "t"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="c", idle=1)
    x = d.line("x", owner="t", idle=1)
    y = d.line("y", owner="c", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=12)
    w = d.profile("W", nom=30, min=0, max=INF)
    b = d.profile("B", nom=27, min=0, max=INF)
    d.cond("start", owner="c", prof=w)
    d.xfer(8, lane, owner="c", prof=b, src="ones")
    d.xfer(1, lane, owner="t", prof=b, src="zero")
    d.cond("fall", scl, owner="c", prof=w)
    if wait:
        d.cond("fall", y, owner="c", prof=d.profile("Y", nom=40, min=0, max=INF))
    with d.only("c"):
        d.halt()
    d.cond("fall", x, owner="t", prof=w)     # t goes on alone, as in the program found
    d.cond("rise", x, owner="t", prof=w)
    d.halt()
    return d


def test_a_halt_that_releases_scl_waits_for_the_sda_let_go():
    """R2-edge: c's HALT right after its SCL fall lets SCL rise before t's let-go releases SDA,
    ldelay after the fall: an SDA rise with SCL high, a STOP no COND made (found by the random bus
    programs; it compiled on main). A step of c's long enough for the release first, and it's fine."""
    from emu.compiler import CompileError, compile_desc
    from emu.gen.check import bus_mismatches
    from emu.isa import Role
    from simutil import run_sys
    with pytest.raises(CompileError, match=r"\[R2-edge\] .*HALT by P0 releases SCL"):
        compile_desc(halt_after_scl_fall(), check_timing=False)
    img = compile_desc(halt_after_scl_fall(), check_levels=False, check_timing=False)
    t = run_sys(img, roles=(Role.P0, Role.P1, Role.MON))
    assert any(m.startswith("wire:") for m in bus_mismatches(img, t))
    compile_desc(halt_after_scl_fall(wait=True), check_timing=False)


def test_a_halt_with_scl_high_releases_no_scl():
    """c halts with SCL high (after t's ACK, a let-go on SDA pending): SCL doesn't rise, so the
    HALT clause for SCL says nothing (t's own HALT later is R1-bus's)."""
    from emu.desc import Description
    from emu.isa import INF
    d = Description("hs", parties=("c", "t"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="c", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=4)
    w = d.profile("W", nom=30, min=0, max=INF)
    d.cond("start", owner="c", prof=w)
    d.xfer(8, lane, owner="c", prof=w, src="ones")
    d.xfer(1, lane, owner="t", prof=w, src="zero")
    with d.only("c"):
        d.halt()
    d.halt()
    try:
        compile_desc(d, check_timing=False)
        msgs = []
    except CompileError as e:
        msgs = [x.msg for x in e.diags]
    assert not any("releases SCL" in m for m in msgs)

