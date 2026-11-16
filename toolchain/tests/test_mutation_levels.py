"""Tests for R2-edge and R1-bus's level walk (emu/analysis.py, check_levels) that mutation testing
found loose (verification.md §1): each pins which check rejects a program, or the boundary of a
count it keeps. The rules are isa §5 (R2-edge, R1-bus); the let-go is isa §4.3."""
import pytest

from emu.analysis import _LG, _LGU, _LGX, _SHUT, _SHUT1, _join, drift
from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import EDGE_LAT as E, INF, QUANT as Q

from test_edge_rule import i2c_byte, model_goes_wrong, runs_clean


def diags(d: Description) -> list[tuple[str, str]]:
    """(rule, message) for each diagnostic, every check on."""
    try:
        compile_desc(d)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def said(d: Description, words: str) -> bool:
    return any(words in m for _, m in diags(d))


RISES = "rises while a let-go may still release SDA"
MAYBE = "may or may not have an edge"


def stop_after(d, sda, scl, lane, half, w):
    """With SCL high and SDA released: a repeated START, one bit, a STOP."""
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()


# ---------------------------------------------------------------- _join
@pytest.mark.parametrize("x,y,want", [
    (("REL", 3), 1, ("REL", 3)),
    (1, ("REL", 3), ("REL", 3)),
    (("REL", 3), ("REL", 5), ("REL", 5)),
    (("REL", 3), 0, _LGU),
    (("REL", 3), None, _LGU),
    (_LG, 1, _LGX),
    (1, _LG, _LGX),
    (_LG, _LGX, _LGX),
    (("REL", 3), _LG, _LGX),
    (_LGX, ("REL", 3), _LGX),
    (_LG, 0, _LGU),
    (0, _LGX, _LGU),
    (_LGU, 1, _LGU),
    (None, _LGU, _LGU),
    (0, 1, None),
    (0, None, None),
    (_SHUT, 1, _SHUT1),
    (_SHUT1, _SHUT, _SHUT1),
])
def test_join_keeps_a_let_go_that_one_path_may_still_hold(x, y, want):
    """A pending release meets a released line: pending, for the longer time. A let-go taken on one
    path (pending) or not yet taken (held), against a released or held one: it may still be held
    (_LGX). A let-go against a drive or an unknown: unknown, and it may still release (_LGU). A
    gated line shut on one path and high on the other reads 1."""
    assert _join(x, y) == want


def taken_on_one_path(layout):
    """After the controller's byte, one path's SCL fall takes the let-go (pending for ldelay 15),
    the other's doesn't (SDA still held). Where they meet, a HOLD 1 raises SCL on the first path
    with the release still to come: an SDA rise with SCL high, a STOP no COND STOP made. The
    model takes the falling path; `layout` puts it at the branch's target or its fall-through, so
    the walk meets either path first."""
    d = Description("j2", parties=("ctl", "tgt"))
    d.li("r3", 0)
    sda, scl, x, lane, half, w = i2c_byte(d, ldelay=15)
    y = d.line("y", owner="ctl", idle=1)
    f4 = d.profile("F4", nom=4, min=0, max=INF)
    first, then = (lambda: d.cond("fall", scl, owner="ctl", prof=f4),
                   lambda: d.cond("fall", x, owner="ctl", prof=f4))    # a timed step, but no SCL edge
    d.br("other", "z" if layout == "target" else "nz", "r3")
    (then if layout == "target" else first)()
    d.br("join")
    d.label("other")
    (first if layout == "target" else then)()
    d.label("join")
    d.cond("fall", y, owner="ctl", prof=d.profile("Y", nom=10, min=0, max=INF))  # the paths meet (a HOLD may not start late)
    d.hold(scl, 1, owner="ctl", prof=d.profile("H1", nom=40))
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("layout", ["target", "fall-through"])
def test_a_let_go_taken_on_only_one_path_is_still_tracked_where_they_meet(layout):
    """_join(pending, held) was unknown, which no SCL check reads as a let-go: where the walk met
    the held path first, accepted, and on the model the release came with SCL high."""
    assert said(taken_on_one_path(layout), RISES)
    assert model_goes_wrong(taken_on_one_path(layout))


# ---------------------------------------------------------------- the release count (isa §5 R2-edge)
def byte(d, H=20, L=2):
    """i2c_byte's lines and START, then the controller's byte from r1 (0xB4) at half period H,
    with ldelay L on SDA's lane."""
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=L)
    half = d.profile("HALF", nom=H, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    with d.only("ctl"):
        d.li("r1", 0xB4)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="r1", dst="r2", msb_first=True)
    return sda, scl, x, lane, half, w


def held_low(n):
    """ldelay 15 at H 24, 60000 ppm: SCL's fall (a COND of nom 10) takes the let-go, a HOLD keeps
    SCL low for n, then SCL rises."""
    d = Description("n", parties=("ctl", "tgt"), tolerance_ppm=60000)
    sda, scl, x, lane, half, w = byte(d, H=24, L=15)
    d.cond("fall", scl, owner="ctl", prof=d.profile("F10", nom=10, min=0, max=INF))
    d.hold(scl, 0, owner="ctl", prof=d.profile("LO", nom=n))
    d.hold(scl, 1, owner="ctl", prof=d.profile("HI", nom=40))
    stop_after(d, sda, scl, lane, half, w)
    return d


@pytest.mark.parametrize("n,ok", [(22, False), (23, True)])
def test_the_release_needs_max_ldelay_edge_lat_plus_quant_1_and_drift(n, ok):
    """The release may take max(15, EDGE_LAT) + QUANT + 1 + ceil(0.06 * (15 + EDGE_LAT)) = 19. The
    HOLD counts its nom less two parties' drift and QUANT: 22 - ceil(0.12 * 22) - 1 = 18, 23 gives
    19. The fall's own nom comes before the edge, so it doesn't count; the HOLD 1 is checked before
    it counts its own."""
    assert (E, Q) == (3, 1) and drift(60000, 18) == 2 and drift(120000, 22) == drift(120000, 23) == 3
    if ok:
        runs_clean(held_low(n))
    else:
        assert said(held_low(n), RISES)


def zero_steps(k):
    """SCL falls (ldelay 2: the release may take 3 + 1 + 1 + 1 = 6), then k CONDs of nom 0 on x
    (with role-block NOPs, so the target can watch each), then SCL rises."""
    d = Description("z", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = byte(d)
    z = d.profile("Z", nom=0, min=0, max=INF)
    d.cond("fall", scl, owner="ctl", prof=w)
    for i in range(k):
        with d.only("ctl"):
            for _ in range(6):
                d.nop()
        d.cond("fall" if i % 2 == 0 else "rise", x, owner="ctl", prof=z)
    d.hold(scl, 1, owner="ctl", prof=d.profile("HI", nom=40))
    stop_after(d, sda, scl, lane, half, w)
    return d


def test_a_step_of_nom_0_counts_no_time():
    """nom 0 less drift and QUANT is below 0, which counts as 0 (isa §5), not as a cycle: after 6 of
    them the release may still come, so SCL may not rise yet. (Counting below 0 would make the
    walk endless: test_edge_rule.py's loop test.)"""
    assert said(zero_steps(6), RISES)


def second_lane(hm):
    """A 1-bit XFER of half period hm on lane M (PP data x), clocked by SCL, after the byte (SDA's
    lane L has ldelay 15)."""
    d = Description("m", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = byte(d, L=15)
    m = d.lane("M", x, clk=scl, cpol=1, cpha=1)
    d.xfer(1, m, owner="ctl", prof=d.profile("HM", nom=hm, min=0, max=INF), src="ones")
    stop_after(d, sda, scl, lane, half, w)
    return d


@pytest.mark.parametrize("hm,ok", [(19, False), (20, True)])
def test_another_lanes_xfer_releases_sda_within_it_from_h_of_ldelay_plus_edge_lat_plus_2(hm, ok):
    """ldelay 15 <= H - EDGE_LAT - 2 from H 20: released within the XFER (T4-profile's bound)."""
    if ok:
        runs_clean(second_lane(hm))
    else:
        assert said(second_lane(hm), "is too short for the let-go on SDA")


# ---------------------------------------------------------------- what raises SCL (scl_rises)
def rise_after_fall(how):
    """SCL falls 4 after the byte (ldelay 15: the release is still to come), then rises by `how`."""
    d = Description("r", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = byte(d, L=15)
    d.cond("fall", scl, owner="ctl", prof=d.profile("F4", nom=4, min=0, max=INF))
    if how == "HOLD 1":
        d.hold(scl, 1, owner="ctl", prof=d.profile("HI", nom=40))
    else:
        with d.only("ctl"):
            if how == "RAW SET 1":
                d.raw_set(scl, 1)
            else:
                d.raw_rel(scl)
    stop_after(d, sda, scl, lane, half, w)
    return d


@pytest.mark.parametrize("how", ["HOLD 1", "RAW SET 1", "RAW REL"])
def test_each_way_of_raising_scl_waits_for_the_release(how):
    assert said(rise_after_fall(how), RISES)
    assert model_goes_wrong(rise_after_fall(how))


def test_a_raw_set_0_on_a_low_scl_is_no_rise():
    """SCL is already low: RAW SET 0 doesn't raise it, so the pending release is no matter yet."""
    d = Description("r0", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = byte(d, L=15)
    d.cond("fall", scl, owner="ctl", prof=d.profile("F4", nom=4, min=0, max=INF))
    with d.only("ctl"):
        d.raw_set(scl, 0)
    d.cond("fall", x, owner="ctl", prof=d.profile("X60", nom=60, min=0, max=INF))
    d.hold(scl, 1, owner="ctl", prof=d.profile("HI", nom=40))
    stop_after(d, sda, scl, lane, half, w)
    runs_clean(d)


def test_a_raw_fall_of_scl_takes_the_let_go():
    """RAW SET 0 by both parties is SCL's fall: it takes the let-go, and 60 later SDA is released, so
    the RAW REL may raise SCL."""
    d = Description("rf", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = byte(d, L=15)
    with d.only("ctl", "tgt"):
        d.raw_set(scl, 0)
    d.cond("fall", x, owner="ctl", prof=d.profile("X60", nom=60, min=0, max=INF))
    with d.only("ctl", "tgt"):
        d.raw_rel(scl)
    stop_after(d, sda, scl, lane, half, w)
    runs_clean(d)


# ---------------------------------------------------------------- clock edges off I2C
def od_lane(d, cpol, ldelay=2):
    """a's OD data line dl on lane L (ldelay 2), clocked by c, a's (PP for cpol 0, OD for cpol 1).
    A NOP first: instruction 0 may not watch an idle-0 line (T5-startup)."""
    dl = d.line("dl", "od", owner="a", idle=1)
    c = d.line("c", "pp" if cpol == 0 else "od", owner="a", idle=cpol)
    lane = d.lane("L", dl, clk=c, cpol=cpol, cpha=1, ldelay=ldelay)
    d.nop()
    return dl, c, lane, d.profile("HALF", nom=20, min=0, max=INF), d.profile("W", nom=40, min=0, max=INF)


def test_a_clean_rise_of_the_clock_takes_the_let_go():
    """cpol 0: the next edge after the XFER is a rise; 40 later the line is released."""
    d = Description("cr", parties=("a", "b"))
    dl, c, lane, half, w = od_lane(d, 0)
    d.xfer(1, lane, owner="a", prof=half, src="zero")
    d.cond("rise", c, owner="a", prof=d.profile("F4", nom=4, min=0, max=INF))
    d.hold(c, 1, owner="a", prof=d.profile("HI", nom=40))
    d.cond("fall", dl, owner="a", prof=w)
    d.cond("rise", dl, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def test_an_od_xfer_from_ones_leaves_its_line_released():
    d = Description("o1", parties=("a", "b"))
    dl, c, lane, half, w = od_lane(d, 0)
    d.xfer(8, lane, owner="a", prof=half, src="ones")
    d.cond("fall", dl, owner="a", prof=w)
    d.cond("rise", dl, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def other_party_fall(then):
    """cpol 1: after a's XFER from ZERO, b pulls the clock low (a's drive doesn't move, so a still
    holds the let-go), then both RAW SET it low (a's drive moves: a takes the let-go, which
    releases dl 2 later). The walk can't pin either down. Then a waits 40, `then`, and both
    release c."""
    d = Description("op", parties=("a", "b"))
    dl, c, lane, half, w = od_lane(d, 1)
    x = d.line("x", owner="a", idle=1)
    d.xfer(1, lane, owner="a", prof=half, src="zero")
    d.cond("fall", c, owner="b", prof=w)
    with d.only("a", "b"):
        d.raw_set(c, 0)
    d.cond("fall", x, owner="a", prof=w)
    then(d, dl, w)
    with d.only("a", "b"):
        d.raw_rel(c)
    d.halt()
    return d


def nothing(d, dl, w):
    pass


def rise_dl(d, dl, w):
    d.cond("rise", dl, owner="a", prof=w)


def test_an_edge_the_walk_cant_pin_down_off_i2c_leaves_the_let_go_unknown():
    """Not an error off I2C (nothing reads dl)..."""
    runs_clean(other_party_fall(nothing))


def test_a_cond_rise_after_such_an_edge_is_rejected():
    """...but a's COND RISE on dl may find it released already: no edge, and b waits."""
    assert said(other_party_fall(rise_dl), "may not be low by the doer's drive alone")
    assert model_goes_wrong(other_party_fall(rise_dl))


def test_on_i2c_an_unpinned_scl_edge_with_a_let_go_on_another_lane_is_no_error():
    """The let-go on lane M's OD data z can't make a STOP: only SDA's is an error (it is unknown)."""
    d = Description("z", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    z = d.line("z", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    m = d.lane("M", z, clk=scl, cpol=1, cpha=1, ldelay=2)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.xfer(1, m, owner="ctl", prof=d.profile("HALF", nom=20, min=0, max=INF), src="zero")
    d.cond("fall", scl, owner="tgt", prof=w)
    d.cond("rise", scl, owner="tgt", prof=w)
    d.halt()
    runs_clean(d)


# ---------------------------------------------------------------- what a step leaves on a line
def test_a_timed_step_opens_a_gate_and_the_line_reads_released():
    """a's COND opens b's gate: g is released, at the pull-up, so b's COND FALL makes an edge."""
    d = Description("g", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    g = d.line("g", owner="b", idle=1, gated=True)
    w = d.profile("W", nom=20, min=0, max=INF)
    d.cond("fall", x, owner="a", prof=w)
    d.cond("fall", g, owner="b", prof=w)
    d.cond("rise", g, owner="b", prof=w)
    d.cond("rise", x, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def test_only_a_select_stop_shuts_the_gates():
    """After START the peripheral drives its gated line low and back high; only the STOP releases it."""
    d = Description("sg", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    g = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    w = d.profile("W", nom=20, min=0, max=INF)
    d.cond("start", owner="ctl", prof=w)
    d.cond("fall", g, owner="per", prof=w)
    d.cond("rise", g, owner="per", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    runs_clean(d)


def test_an_unclocked_od_xfer_from_a_register_leaves_its_line_unknown():
    """b's UART-style frame on an OD line: its last bit (r5 = 0) may still hold it low, so the next
    start bit's COND FALL may make no edge."""
    from emu.protocols import slack
    d = Description("u", parties=("a", "b"))
    dl = d.line("d", "od", owner="b", idle=1)
    s = slack(20, 20000)
    bit = d.profile("BIT", nom=20, min=20 - s, max=20 + s)
    d.li("r5", 0)
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src="ones", dst="r2")   # released: the line was high
    d.xfer(1, dl, owner="b", prof=bit, src="r5", dst="r2")
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.hold(dl, 1, owner="b", prof=bit)
    d.halt()
    assert said(d, "may not be high here")
    assert model_goes_wrong(d)


def test_raw_set_1_drives_the_line_high():
    d = Description("r1", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    w = d.profile("W", nom=20, min=0, max=INF)
    with d.only("a"):
        d.raw_set(x, 0)
        d.raw_set(x, 1)
    d.cond("fall", x, owner="a", prof=w)
    d.cond("rise", x, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def test_raw_rel_releases_an_od_line_where_paths_meet():
    """One path RAW SETs s low and RAW RELs it, the other leaves it: released on both."""
    d = Description("rr", parties=("a", "b"))
    s = d.line("s", "od", owner="a", idle=1)
    w = d.profile("W", nom=20, min=0, max=INF)
    d.li("r3", 0)
    d.br("skip", "nz", "r3")
    with d.only("a"):
        d.raw_set(s, 0)
        d.raw_rel(s)
    d.label("skip")
    d.cond("fall", s, owner="a", prof=w)
    d.cond("rise", s, owner="a", prof=w)
    d.halt()
    runs_clean(d)


# ---------------------------------------------------------------- R1-bus's CS walk
def test_cs_keeps_its_level_where_two_paths_with_cs_low_meet():
    d = Description("cj", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    sck = d.line("sck", owner="ctl", idle=0)
    mosi = d.line("mosi", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("select", cs)
    lane = d.lane("L", mosi, clk=sck)
    half = d.profile("HALF", nom=10, min=0, max=INF)
    w = d.profile("W", nom=20, min=0, max=INF)
    d.li("r3", 0)
    d.cond("start", owner="ctl", prof=w)
    d.br("a", "z", "r3")
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    d.br("j")
    d.label("a")
    d.xfer(8, lane, owner="ctl", prof=half, src="zero")
    d.label("j")
    d.cond("fall", x, owner="ctl", prof=w)
    d.hold(cs, 0, owner="ctl", prof=d.profile("CS0", nom=10))
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    runs_clean(d)


@pytest.mark.parametrize("start_on", ["target", "fall-through"])
def test_cs_at_a_level_on_only_some_paths_is_unknown_whichever_the_walk_meets_first(start_on):
    """One path into the RAW SET 0 has the START (CS low), the other not (CS high): unknown where
    they meet, whichever the walk takes first (test_engine_bus.py has the high one first)."""
    d = Description("cs2", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    d.bus("select", cs)
    w = d.profile("W", nom=20, min=0, max=INF)
    d.li("r1", 0)
    d.br("go", "z" if start_on == "target" else "nz", "r1")
    if start_on == "fall-through":
        d.cond("start", owner="ctl", prof=w)
    d.br("j")
    d.label("go")
    if start_on == "target":
        d.cond("start", owner="ctl", prof=w)
    d.label("j")
    with d.only("ctl"):
        d.raw_set(cs, 0)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    assert said(d, "RAW SET 0 on the SELECT line 'cs' by P0: cs may not be at 0")


def plain_0_on_one_path(layout):
    """After the controller's ZERO byte (ldelay 15, SDA held by the let-go), one path drives SDA 0
    with a HOLD, the other doesn't. Where they meet, SCL falls (taking the let-go on the second
    path, released 15 later) and a HOLD 1 raises it at once: on that path, SDA rises with SCL
    high, a STOP. The model takes the path without the HOLD; `layout` puts the HOLD at the
    branch's target or its fall-through (found by the review of this pass)."""
    d = Description("j0", parties=("ctl", "tgt"))
    d.li("r3", 0)
    sda, scl, x, lane, half, w = i2c_byte(d, src="zero", ldelay=15)
    f4 = d.profile("F4", nom=4, min=0, max=INF)

    def plain():
        d.cond("fall", x, owner="ctl", prof=f4)

    def held():
        d.cond("fall", x, owner="ctl", prof=f4)
        d.hold(sda, 0, owner="ctl", prof=d.profile("H0", nom=20))
    d.br("other", "nz" if layout == "target" else "z", "r3")
    (plain if layout == "target" else held)()
    d.br("join")
    d.label("other")
    (held if layout == "target" else plain)()
    d.label("join")
    d.cond("fall", scl, owner="ctl", prof=d.profile("F8", nom=8, min=0, max=INF))
    d.hold(scl, 1, owner="ctl", prof=d.profile("H1", nom=40))
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("layout", ["target", "fall-through"])
def test_a_let_go_met_with_a_plain_drive_may_still_release(layout):
    """_join(let-go, 0) was unknown, which the SCL check doesn't read as a let-go: with the HOLD at
    the target (the walk meets it first), accepted, and on the model a STOP."""
    assert said(plain_0_on_one_path(layout), RISES)
    assert model_goes_wrong(plain_0_on_one_path(layout))


# ---------------------------------------------------------------- a block the walker skips
def lead_exit(drive_y):
    """SELECT. In a lead block the controller may leave by an exit (a STOP, then abort_pc); if
    `drive_y`, that exit first pulls the shared OD line y low. The block's successor S is the
    peripheral's own COND, so the peripheral completes it and runs on whichever way the
    controller left, then drives y low and high. The model takes the exit (found by the review
    of this pass)."""
    d = Description("ld", parties=("ctl", "per"))
    if not drive_y:
        y = d.line("y", "od", owner="shared", idle=1)     # line 0, where a START/STOP's line field points
    cs = d.line("cs", owner="ctl", idle=1)
    if drive_y:
        y = d.line("y", "od", owner="shared", idle=1)
    z = d.line("z", owner="per", idle=1)
    d.bus("select", cs)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.abort_to("ab")
    d.li("r3", 1)
    d.cond("start", owner="ctl", prof=w)
    with d.lead("ctl"):
        d.br("fin", "z", "r3")
        if drive_y:
            d.cond("fall", y, owner="ctl", prof=w)
        d.cond("stop", owner="ctl", prof=w)
        d.br("ab")
    d.label("fin")
    d.cond("fall", z, owner="per", prof=w, abort=True)
    d.cond("rise", z, owner="per", prof=w)
    d.cond("fall", y, owner="per", prof=w)
    d.cond("rise", y, owner="per", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    d.label("ab")
    d.cond("start", owner="ctl", prof=d.profile("L", nom=400, min=0, max=INF))
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


def test_a_drive_in_a_lead_block_the_walker_skips_is_unknown_after_it():
    """The exit's y low may still hold when the peripheral's COND FALL comes: no edge, and the
    monitor waits for one."""
    assert said(lead_exit(True), "y may not be high here")
    assert model_goes_wrong(lead_exit(True))


def test_a_start_or_stop_in_a_lead_block_drives_bus_a_not_line_0():
    """Its line field is 0, here y: the exit's STOP leaves y alone, so the peripheral's COND FALL
    on y is legal."""
    assert diags(lead_exit(False)) == []


def test_a_short_xfer_on_scl_with_sda_possibly_held_is_rejected():
    """As second_lane at H 6, but SDA's let-go met a plain 0 on the other path first: it may still
    be held, so lane M's edges may take it and release it with SCL high."""
    d = Description("m6", parties=("ctl", "tgt"))
    d.li("r3", 0)
    sda, scl, x, lane, half, w = i2c_byte(d, src="zero", ldelay=15)
    y = d.line("y", owner="ctl", idle=1)
    m = d.lane("M", y, clk=scl, cpol=1, cpha=1)
    f4 = d.profile("F4", nom=4, min=0, max=INF)
    d.br("held", "nz", "r3")
    d.cond("fall", x, owner="ctl", prof=f4)
    d.br("join")
    d.label("held")
    d.cond("fall", x, owner="ctl", prof=f4)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H0", nom=20))
    d.label("join")
    d.cond("rise", x, owner="ctl", prof=w)       # where the paths meet
    d.xfer(1, m, owner="ctl", prof=d.profile("HM", nom=6, min=0, max=INF), src="ones")
    d.halt()
    assert said(d, "is too short for the let-go on SDA")


def test_a_second_clock_edge_doesnt_restart_a_pending_release():
    """Off I2C, cpol 1, ldelay 15: the clock's fall takes the let-go (the release may take 15 + 1 +
    1 + 1 = 18); the rise, a COND of nom 10 (counting 10 - 1 - 1 = 8), leaves it pending for 10, not
    taken again; a HOLD of 12 (12 - 1 - 1 = 10) covers the rest, so a's COND FALL on dl makes an
    edge."""
    d = Description("re", parties=("a", "b"))
    dl, c, lane, half, w = od_lane(d, 1, ldelay=15)
    d.xfer(1, lane, owner="a", prof=half, src="zero")
    d.cond("fall", c, owner="a", prof=d.profile("F4", nom=4, min=0, max=INF))
    d.cond("rise", c, owner="a", prof=d.profile("R10", nom=10, min=0, max=INF))
    d.hold(c, 1, owner="a", prof=d.profile("H12", nom=12))
    d.cond("fall", dl, owner="a", prof=d.profile("F4b", nom=4, min=0, max=INF))   # counting only 2 itself
    d.cond("rise", dl, owner="a", prof=w)
    d.halt()
    runs_clean(d)


def test_a_duplex_xfer_leaves_data2_at_its_last_bit():
    """b drives data2 (PP) low, then the duplex XFER from ONES leaves it high, so b's COND FALL on it
    makes an edge."""
    d = Description("dx", parties=("a", "b"))
    c = d.line("c", owner="a", idle=0)
    d1 = d.line("d1", owner="a", idle=1)
    d2 = d.line("d2", owner="b", idle=1)
    lane = d.lane("L", d1, clk=c, cpha=1, data2=d2)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    d.cond("fall", d2, owner="b", prof=w)
    d.hold(d2, 0, owner="b", prof=d.profile("H0", nom=20))
    d.xfer(8, lane, owner="a", prof=d.profile("HALF", nom=20, min=0, max=INF), src="ones")
    d.cond("fall", d2, owner="b", prof=w)
    d.cond("rise", d2, owner="b", prof=w)
    d.halt()
    runs_clean(d)
