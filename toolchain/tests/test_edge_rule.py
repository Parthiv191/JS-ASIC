"""R2-edge (isa §5): a COND its owner does drives the post-edge level (isa §4.2), so the owner must
leave the line at the pre-edge level on every path, or the others wait for an edge that never comes.
Found by the random programs (emu/gen): a clocked XFER whose last bit is 0, then a UART-style
start bit on the same line, compiled with every rule on; the followers hung or misread silently."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description, LineRef
from emu.isa import INF, Role
from emu.model.system import EngineSpec, System
from emu.protocols import PROTOCOLS, slack

P = 20


def desc(between, src="r5"):
    """b sends a UART-style frame on d from `src` (start bit, data, then `between`), then a second
    frame's start bit on d."""
    d = Description("e", parties=("a", "b"))
    dl = d.line("d", owner="b", idle=1)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r5", 0)
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src=src, dst="r2")
    between(d, dl, bit)
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src="ones", dst="r1")
    d.hold(dl, 1, owner="b", prof=bit)
    d.halt()
    return d


def clocked():
    """The random programs' finding: b's clocked XFER from a register, then a start bit on its data line."""
    d = Description("e", parties=("a", "b"))
    dl = d.line("d", owner="b", idle=1)
    clk = d.line("clk", owner="a", idle=0)
    lane = d.lane("L", dl, clk=clk, cpol=0, cpha=1)
    half = d.profile("HALF", nom=16, min=0, max=INF)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r5", 0)
    d.xfer(1, lane, owner="b", prof=half, src="r5")
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src="ones", dst="r1")
    d.hold(dl, 1, owner="b", prof=bit)
    d.halt()
    return d


def nothing(d, dl, bit):
    pass


def stop_bit(d, dl, bit):
    d.hold(dl, 1, owner="b", prof=bit)


def rules(d):
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    return {x.rule for x in e.value.diags}


def test_a_start_bit_after_data_of_unknown_level_is_rejected():
    assert "R2-edge" in rules(desc(nothing))


def test_a_start_bit_after_a_clocked_xfer_is_rejected():
    assert "R2-edge" in rules(clocked())


def test_the_rejected_program_really_hangs_the_followers():
    """What the rule prevents, on the model: b's last bit is 0, so its COND FALL makes no edge."""
    img = compile_desc(clocked(), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(50_000)
    assert t.engines[1].halted and not t.engines[0].halted and not t.engines[2].halted


def test_a_stop_bit_first_makes_it_legal():
    compile_desc(desc(stop_bit))


@pytest.mark.parametrize("src, ok", [("ones", True), ("zero", False)])
def test_a_constant_source_leaves_a_known_level(src, ok):
    if ok:
        compile_desc(desc(nothing, src=src))
    else:
        assert "R2-edge" in rules(desc(nothing, src=src))


def test_a_path_that_skips_the_stop_bit_is_rejected():
    def branchy(d, dl, bit):
        d.br("skip", cond="nz", reg="r2")
        d.hold(dl, 1, owner="b", prof=bit)
        d.label("skip")
        d.nop()
    assert "R2-edge" in rules(desc(branchy))


def test_a_select_start_needs_cs_high():
    d = Description("s", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    sck = d.line("sck", owner="ctl", idle=0)
    mosi = d.line("mosi", owner="ctl", idle=1)
    d.bus("select", cs)
    lane = d.lane("L", mosi, clk=sck)
    half = d.profile("HALF", nom=10, min=0, max=INF)
    wait = d.profile("WAIT", nom=10, min=0, max=INF)
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    d.cond("start", owner="ctl", prof=wait)  # CS is already low: no START edge
    d.halt()
    assert "R2-edge" in rules(d)


@pytest.mark.parametrize("name", sorted(PROTOCOLS))
def test_the_library_protocols_pass(name):
    compile_desc(PROTOCOLS[name].build())

# ---- every party's drive (review of the first R2-edge): each compiled with every rule on, and on
# the model a follower hung or logged a violation
def bitprof(d):
    s = slack(P, 20000)
    return d.profile("BIT", nom=P, min=P - s, max=P + s)


# A: shared OD line, b HOLD 0 then a COND FALL
def A():
    d = Description("A", parties=("a", "b"))
    s_ = d.line("s", "od", owner="shared", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.cond("fall", s_, owner="b", prof=w)
    d.hold(s_, 0, owner="b", prof=bit)
    d.cond("fall", s_, owner="a", prof=w)   # b still pulls low: no edge
    d.hold(s_, 1, owner="a", prof=bit)
    d.halt()
    return d

# B: step owner "shared" HOLD 0, then a's COND FALL
def B():
    d = Description("B", parties=("a", "b"))
    s_ = d.line("s", "od", owner="shared", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.cond("fall", s_, owner="a", prof=w)
    d.hold(s_, 1, owner="a", prof=bit)
    d.hold(s_, 0, owner="shared", prof=bit)  # both parties pull low
    d.cond("fall", s_, owner="a", prof=w)
    d.hold(s_, 1, owner="a", prof=bit)
    d.halt()
    return d

# C: non-owner RAW SET 0 on an OD line, then owner's COND FALL
def C():
    d = Description("C", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    with d.only("b"):
        d.raw_set(x, 0)
    d.cond("fall", x, owner="a", prof=w)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    return d

# D: gated MISO: per HOLD 0, ctl STOP releases it, per COND RISE makes no edge
def D():
    d = Description("D", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("start", owner="ctl", prof=w)
    d.hold(miso, 0, owner="per", prof=bit)
    d.cond("stop", owner="ctl", prof=w)
    d.cond("rise", miso, owner="per", prof=w)
    d.halt()
    return d

# E: I2C START with SCL low
def E():
    d = Description("E", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("start", owner="ctl", prof=w)     # SDA falls with SCL low: no START
    d.cond("rise", scl, owner="ctl", prof=w)
    d.halt()
    return d

# F: I2C: target's ACK let-go holds SDA low; controller's START right after
def F():
    d = Description("F", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones", msb_first=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="zero")   # ACK: tgt holds SDA low until SCL falls
    d.cond("start", owner="ctl", prof=w)                  # ctl's own level is 1, the wire 0
    d.xfer(8, lane, owner="ctl", prof=half, src="ones", msb_first=True)
    d.halt()
    return d

# G: OD line with idle 0: released at RUN, so it reads 1; owner's COND RISE first
def G():
    d = Description("G", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=0)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("rise", x, owner="a", prof=w)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    return d

# H: RAW SET on a gated line while the gate is shut (RUN), then COND RISE
def H():
    d = Description("H", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    w = d.profile("W", nom=P, min=0, max=INF)
    with d.only("per"):
        d.raw_set(miso, 0)     # gate shut: does nothing (isa §2)
    d.cond("rise", miso, owner="per", prof=w)
    d.halt()
    return d


# I: a timed step on another line opens the gate, so a RAW SET 0 on the gated line counts; the
# others arrive after that fall (4 NOPs), so its owner's COND FALL makes no edge they see
def I():
    d = Description("I", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    g = d.line("g", owner="b", idle=1, gated=True)
    d.cond("fall", x, owner="a", prof=d.profile("W0", nom=P, min=0, max=INF))
    with d.only("b"):
        d.raw_set(g, 0)
    with d.only("a"):
        for _ in range(4):
            d.nop()
    d.cond("fall", g, owner="b", prof=d.profile("W", nom=30, min=0, max=INF))
    d.halt()
    return d


@pytest.mark.parametrize("build", [A, B, C, D, E, F, G, H, I], ids=lambda f: f.__name__)
def test_r2_edge_sees_every_partys_drive(build):
    """A: b still pulls a shared OD line low. B: a SHARED HOLD 0. C: a RAW SET 0 by the other party.
    D: a SELECT STOP releases a gated line. E: an I2C START with SCL low. F: the target's ACK
    let-go still holds SDA. G: an OD line with idle 0 starts released, at 1. H: RAW SET on a gated
    line while its gate is shut does nothing. I: once any timed step has opened the gate, it
    drives the line."""
    assert "R2-edge" in rules(build())


def gated_loop(set_level=None):
    """b drives its gated line g low, then high (COND FALL, RISE), in a loop back to top. Reset
    reaches top with the gate shut, the loop with g driven high. With `set_level`, b RAW SETs g
    first, which the shut gate ignores on the first pass only."""
    d = Description("gl", parties=("a", "b"))
    g = d.line("g", owner="b", idle=1, gated=True)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.label("top")
    if set_level is not None:
        with d.only("b"):
            d.raw_set(g, set_level)
    d.cond("fall", g, owner="b", prof=w)
    d.cond("rise", g, owner="b", prof=w)
    d.br("top")
    return d


@pytest.mark.parametrize("set_level,ok", [(None, True), (1, True), (0, False)])
def test_a_gated_line_reads_1_where_a_shut_gate_meets_a_high_drive(set_level, ok):
    """Released (gate shut) and driven high both read 1, so the loop's COND FALL is legal, and
    after a RAW SET 1 too. A RAW SET 0 there is ignored on the first pass and drives g low on the
    next ones: the level is unknown."""
    d = gated_loop(set_level)
    if not ok:
        assert rules(d) == {"R2-edge"}
        return
    img = compile_desc(d)
    from emu.model.engine import VIOLATIONS
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1)]).run(3000)
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []
    assert len([r for r in t.engines[0].records if r.op == "COND"]) > 100


def test_a_timed_step_opens_a_gate_shut_on_only_some_paths():
    """Reset reaches top with g's gate shut, the loop with g high. a's COND FALL on x opens the
    gate on both, so b's RAW SET 0 drives g low and its COND RISE makes an edge."""
    d = Description("go", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    g = d.line("g", owner="b", idle=1, gated=True)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.label("top")
    d.cond("fall", x, owner="a", prof=w)
    with d.only("b"):
        d.raw_set(g, 0)
    d.cond("rise", g, owner="b", prof=w)
    d.cond("rise", x, owner="a", prof=w)
    d.br("top")
    img = compile_desc(d)
    from emu.model.engine import VIOLATIONS
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1)]).run(3000)
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []
    assert len([r for r in t.engines[0].records if r.op == "COND"]) > 100


def test_a_timed_step_opens_the_gate_for_raw_set_on_the_model_too():
    """I on the model: b's RAW SET drives g low, so b's COND FALL makes no edge and a waits."""
    img = compile_desc(I(), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1)]).run(50_000)
    assert t.engines[1].halted and not t.engines[0].halted



def runs_clean(d):
    """Accepted, and on the model every role halts with no violation."""
    from emu.model.engine import VIOLATIONS
    img = compile_desc(d)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert all(et.halted for et in t.engines) and t.quiescent
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []


def test_an_od_line_with_idle_0_starts_released():
    """It reads 1 at RUN, so a COND FALL first is legal (and G's RISE first is not)."""
    d = Description("g2", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=0)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("fall", x, owner="a", prof=w)
    d.hold(x, 0, owner="a", prof=bit)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    runs_clean(d)


@pytest.mark.parametrize("build", [lambda: desc(stop_bit), lambda: desc(nothing, src="ones")],
                         ids=["stop bit", "ones"])
def test_the_accepted_cases_run_clean(build):
    runs_clean(build())


# ---- the let-go of a clocked OD byte from a register or the FIFO (isa §4.3): the engine holds
# the last bit, whatever it is, until the lane's next clock edge, then releases the line
def od_byte(after, ldelay=2, src="r1", start_nom=40):
    """ctl's clocked OD byte from `src` (r1 = 0xB4: MSB first, the last bit is 0), then `after`,
    then a repeated START, one bit and a STOP. `x` is a spare line."""
    d = Description("lg", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=ldelay)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    with d.only("ctl"):
        d.li("r1", 0xB4)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src=src, dst="r2", msb_first=True)
    after(d, sda, scl, lane, half, w)
    d.cond("start", owner="ctl", prof=d.profile("WS", nom=start_nom, min=0, max=INF))
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


def scl_low_high(d, sda, scl, lane, half, w):
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("rise", scl, owner="ctl", prof=w)


def ack_then_scl_low_high(d, sda, scl, lane, half, w):
    d.xfer(1, lane, owner="tgt", prof=half, src="zero")
    scl_low_high(d, sda, scl, lane, half, w)


def test_before_the_next_clock_edge_a_clocked_od_byte_may_still_hold_sda_low():
    assert "R2-edge" in rules(od_byte(lambda *a: None))


@pytest.mark.parametrize("after", [scl_low_high, ack_then_scl_low_high], ids=["scl low", "ack, scl low"])
def test_the_next_clock_edge_lets_a_clocked_od_byte_go(after):
    """The repeated START of an I2C register read: SDA is high once SCL has fallen after the byte
    (and the target's ACK, if any, has been let go the same way)."""
    runs_clean(od_byte(after))


# ---- the review of the let-go change: the release comes `ldelay` after a real edge of the clock
# owner's drive (isa §4.3), so a COND/HOLD-made edge counts only if it moves that drive, and the
# line stays possibly low until the steps after it have taken that long
def soon(nom):
    def after(d, sda, scl, lane, half, w):
        p = d.profile(f"N{nom}", nom=nom, min=0, max=INF)
        d.cond("fall", scl, owner="ctl", prof=p)
        d.cond("rise", scl, owner="ctl", prof=p)
    return after


def hold_scl_high(d, sda, scl, lane, half, w):
    d.cond("fall", LineRef("x", 2), owner="ctl", prof=w)       # a timed step, but no clock edge
    d.hold(scl, 1, owner="ctl", prof=d.profile("H1", nom=20))


def tgt_clocks(d, sda, scl, lane, half, w):
    d.cond("fall", scl, owner="tgt", prof=w)
    d.cond("rise", scl, owner="tgt", prof=w)


def model_goes_wrong(d):
    """A violation, an engine that never halts, or a follower seeing a STOP no COND STOP made (the
    program has one)."""
    from emu.model.engine import VIOLATIONS
    t = System([EngineSpec(compile_desc(d, check_levels=False), r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    bad = [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS]
    stops = max([k for _, k in et.bus_events].count("STOP") for et in t.engines)
    return bad or not all(et.halted for et in t.engines) or stops > 1


LATE_RELEASE = {
    "SCL low and high, then the START, too soon after the byte": dict(after=soon(4), ldelay=15, start_nom=4),
    "the START long after, but SCL rose first": dict(after=soon(4), ldelay=15, start_nom=60),
    "a HOLD at SCL's own level": dict(after=hold_scl_high),
    "the edges made by a party that doesn't own the clock": dict(after=tgt_clocks),
}


@pytest.mark.parametrize("src", ["r1", "zero"])
@pytest.mark.parametrize("case", LATE_RELEASE)
def test_a_let_go_not_yet_released_is_rejected(case, src):
    """Each compiled with every rule on before this fix. On the model the repeated START makes no
    edge and the followers hang, or the release comes with SCL high: a STOP no COND STOP made."""
    d = od_byte(src=src, **LATE_RELEASE[case])
    assert "R2-edge" in rules(d)
    assert model_goes_wrong(od_byte(src=src, **LATE_RELEASE[case]))


def test_the_release_comes_in_time_at_the_longest_ldelay():
    """ldelay 15 at H 20 (the most T4-profile allows): SCL low for 20, then high for 40."""
    d = od_byte(soon(20), ldelay=15)
    runs_clean(d)
    assert not model_goes_wrong(od_byte(soon(20), ldelay=15))


# ---- the second review of the let-go: the walk ends, SCL "rises" only from low, and a RAW drive or
# another lane's XFER on SCL takes the let-go too
def i2c_byte(d, src="r1", ldelay=2, owner="ctl"):
    """Lines sda, scl, x (spare, PP); lane L on (sda, scl); START, then `owner`'s byte from `src`."""
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=ldelay)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    with d.only(owner):
        d.li("r1", 0xB4)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner=owner, prof=half, src=src, dst="r2", msb_first=True)
    return sda, scl, x, lane, half, w


def repeated_start_and_stop(d, sda, scl, lane, half, w):
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()


def test_a_loop_of_nom_0_steps_after_a_pending_release_compiles_in_finite_time():
    """A step's nom less drift and QUANT is below 0 for nom 0: counted as is, a pending release
    grew each time round the loop and the walk never ended."""
    import threading
    d = Description("lp", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = i2c_byte(d)
    z = d.profile("Z", nom=0, min=0, max=INF)
    d.cond("fall", scl, owner="ctl", prof=w)
    d.label("L")
    with d.only("ctl"):
        for _ in range(6):
            d.nop()
    d.cond("fall", x, owner="ctl", prof=z)
    with d.only("ctl"):
        for _ in range(6):
            d.nop()
    d.cond("rise", x, owner="ctl", prof=z)
    d.br("L")
    done = []

    def walk():
        try:
            compile_desc(d)
        except CompileError:
            pass
        done.append(True)
    th = threading.Thread(target=walk, daemon=True)
    th.start()
    th.join(30)
    assert done, "the level walk didn't end"
    compile_desc(d)          # and it runs clean on the model, so it is accepted


def test_a_hold_1_on_scl_while_it_is_high_is_no_rise():
    """It makes no SCL edge, so no let-go is released with SCL high; then a real fall and rise."""
    d = Description("p7", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = i2c_byte(d)
    d.cond("fall", x, owner="ctl", prof=w)
    d.hold(scl, 1, owner="ctl", prof=d.profile("H1", nom=20))
    repeated_start_and_stop(d, sda, scl, lane, half, w)
    runs_clean(d)


def raw_on_scl(src="r1"):
    d = Description("raw", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = i2c_byte(d, src=src, ldelay=8)
    d.cond("fall", x, owner="ctl", prof=w)
    with d.only("ctl"):
        d.raw_set(scl, 0)
        d.raw_rel(scl)
    repeated_start_and_stop(d, sda, scl, lane, half, w)
    return d


def second_lane_on_scl(src="r1"):
    """Lane M (PP data z) on SCL with H 6, against lane L's ldelay 15."""
    d = Description("m2", parties=("ctl", "tgt"))
    sda, scl, x, lane, half, w = i2c_byte(d, src=src, ldelay=15)
    m = d.lane("M", x, clk=scl, cpol=1, cpha=1)
    d.xfer(1, m, owner="ctl", prof=d.profile("HM", nom=6, min=0, max=INF), src="ones")
    d.cond("start", owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="ones")
    d.cond("fall", scl, owner="ctl", prof=w)
    d.hold(sda, 0, owner="ctl", prof=d.profile("H", nom=20))
    d.cond("rise", scl, owner="ctl", prof=w)
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("src", ["r1", "zero"])
@pytest.mark.parametrize("build", [raw_on_scl, second_lane_on_scl], ids=["RAW on SCL", "another lane's XFER"])
def test_other_edges_on_scl_take_the_let_go_too(build, src):
    """A RAW SET 0 / REL on SCL, or a short-H XFER on another lane clocked by SCL: the let-go is
    taken at the fall and released ldelay later, after SCL is high again: a STOP (both compiled
    before)."""
    assert "R2-edge" in rules(build(src))
    assert model_goes_wrong(build(src))
