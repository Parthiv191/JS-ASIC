"""The bus (isa §4.7): the START/STOP detector, COND START/STOP (§4.2), bus exits, and COND MEAS.

I2C here: SDA (bus_a, shared OD) and SCL (bus_b, OD, owned by a), confirm 15 cycles. A drive in
cycle t is on the wire from t + 1 and seen at t + EDGE_LAT (isa §1)."""
import pytest

from emu.analysis import _steps, completion
from emu.compiler import CompileError, compile_desc, lower
from emu.desc import Description
from emu.isa import EDGE_LAT, INF, QUANT, Role
from emu.model.system import Glitch

from simutil import kinds, run_sys

NOM = 8
CONFIRM = 15


def i2c_desc(body, confirm=CONFIRM, abort_label=None):
    """`body(d, sda, scl, p)` builds the program. The master then waits before its HALT, because
    HALT releases its OD lines: SDA rising while SCL is high would be a STOP."""
    d = Description("bus", parties=("a", "b"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="a", idle=1)
    d.bus("i2c", sda, scl, confirm=confirm)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)  # S-bus: I2C is used with this lane
    if abort_label:
        d.abort_to(abort_label)
    body(d, sda, scl, d.profile("P", nom=NOM, min=0, max=INF))
    with d.only("a"):
        d.raw_delay(200)
    d.halt()
    return d


def i2c(body, **kw):
    return compile_desc(i2c_desc(body, **kw), check_timing=False)


def conds(t, i):
    return [r for r in t.engines[i].records if r.op == "COND"]


def start_body(d, sda, scl, p):
    d.cond("start", owner="a", prof=p)


SEEN = NOM + EDGE_LAT                 # the START's SDA fall, driven at tb + nom = 8, seen at 11
CONFIRMED = SEEN + CONFIRM            # SCL stayed high for the next 15 cycles: end of cycle 26


# ---------------------------------------------------------------- the detector, COND START/STOP
def test_start_is_confirmed_and_followed():
    """The followers confirm the START at the end of cycle 26 and complete their followed START
    at 27 (the flag is visible from the next cycle) with tb := start_e = 8."""
    t = run_sys(i2c(start_body))
    (do,) = conds(t, 0)
    assert do.mode == "do" and do.cycle == do.tb == NOM
    for i in (1, 2):
        (f,) = conds(t, i)
        assert (f.mode, f.cycle, f.tb) == ("follow", CONFIRMED + 1, NOM)
        assert t.engines[i].bus_events == [(CONFIRMED, "START")] and not t.engines[i].events
    assert not t.engines[0].bus_events  # the master line's owner runs no detector


def test_start_completion_matches_the_compiler():
    """isa §6: a followed START completes at E + bus_confirm + 1 after its tb."""
    d = i2c_desc(start_body)
    step = next(iter(_steps(lower(d)).values()))
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = conds(t, i)
        assert rec.cycle - rec.tb == completion(step, r)[0], r


def test_stop_after_start():
    """STOP: SDA rises while SCL is high. START cleared both flags, so the STOP is new, and the
    followed STOP clears both again."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=d.profile("W", nom=40, min=0, max=INF))
    t = run_sys(i2c(body))
    _, stop_do = conds(t, 0)
    for i in (1, 2):
        _, e = conds(t, i)
        assert e.tb == stop_do.cycle and e.cycle == stop_do.cycle + EDGE_LAT + CONFIRM + 1
        assert [k for _, k in t.engines[i].bus_events] == ["START", "STOP"]
        assert t.engines[i].evt & 3 == 0


def test_a_failed_confirm_drops_the_candidate():
    """SDA falls while SCL is high, but SCL falls 5 cycles later (inside the 15-cycle confirm):
    no START, so the follower's bounded START times out."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=60))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(1, NOM + 1 + 5, 3, 0)])
    assert not t.engines[1].bus_events and "TIMEOUT" in kinds(t, 1)


def test_followed_start_with_the_flag_already_set_completes_at_once():
    """isa §4.2: a followed START completes on a flag set before the step (a repeated START):
    P1 reaches its COND START long after the flag is set."""
    def body(d, sda, scl, p):
        with d.only("b"):
            d.raw_delay(40)
        d.cond("start", owner="a", prof=p)
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    (do,), (f,) = conds(t, 0), conds(t, 1)
    start = dict(t.engines[1].starts)[2]  # ROLE_IF, RAW DELAY, COND
    assert start > CONFIRMED and (f.cycle, f.tb) == (start, do.cycle)


def test_followed_start_logs_early():
    """EARLY: m = start_e - tb below min - QUANT. The followers' profile wants the START at least
    18 cycles after tb (nom 24, min 18); the master's comes at 8."""
    def body(nom, lo):
        return lambda d, sda, scl, p: d.cond("start", owner="a", prof=d.profile("S", nom=nom, min=lo, max=INF))
    img, late = i2c(body(NOM, 6)), i2c(body(24, 18))
    t = run_sys(img, images={1: late, 2: late})
    assert "EARLY" in kinds(t, 1) and "EARLY" in kinds(t, 2)


def test_followed_start_times_out_with_the_confirm_included():
    """The timeout uses now - EDGE_LAT - bus_confirm (isa §4.2): with max 30, no START, and
    tb = 0, TIMEOUT at 30 + QUANT + EDGE_LAT + bus_confirm + 1."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=30))
    t = run_sys(i2c(body), roles=(Role.P1,))
    (ev,) = t.engines[0].events
    assert ev.kind == "TIMEOUT" and ev.cycle == 30 + QUANT + EDGE_LAT + CONFIRM + 1


# ---------------------------------------------------------------- bus exits
def exit_desc(abort):
    """START, then a HOLD the slave follows (with or without ABORT), then HALT; abort_pc ("top") is
    an unbounded followed START. A glitch at 30 (after the first START is confirmed at 26) lifts
    SDA for one tick while SCL is high: P1 sees a rise and a fall, so a new START is confirmed at
    48, during its HOLD (it samples at 51)."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.hold(sda, 0, owner="a", prof=d.profile("L", nom=80, min=0, max=INF), abort=abort)
        with d.only("a"):
            d.raw_delay(200)  # a HOLD do completes when it drives: keep the bus until P1 is done
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    return compile_desc(i2c_desc(body, abort_label="top"), check_timing=False)


def test_a_repeated_start_is_a_bus_exit_with_abort():
    """With ABORT a START during a timed step is a bus exit: normal (BUS, no ABORT and no
    violation), the flag stays set, and abort_pc's followed START completes at once."""
    t = run_sys(exit_desc(abort=True), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 30, 1, 1)])
    ks = kinds(t, 1)
    assert "BUS" in ks and "ABORT" not in ks and not t.engines[1].violations
    pcs = [pc for pc, _ in t.engines[1].starts]
    assert pcs[:2] == [0, 1] and pcs[2] == 5  # START, HOLD (cut), then abort_pc (after ROLE_IF,
    top = conds(t, 1)[-1]                      # RAW DELAY, HALT): the followed START
    assert top.cycle == dict(t.engines[1].starts)[5]  # completed in its start cycle


def test_without_abort_a_flag_does_nothing():
    t = run_sys(exit_desc(abort=False), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 30, 1, 1)])
    assert "BUS" not in kinds(t, 1) and [pc for pc, _ in t.engines[1].starts][:3] == [0, 1, 2]
    assert 5 not in [pc for pc, _ in t.engines[1].starts]


# ---------------------------------------------------------------- SELECT
def test_select_start_and_stop():
    """SELECT: CS falling is START, rising is STOP, confirmed after 1 cycle (the default)."""
    d = Description("sel", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    d.line("miso", owner="b", idle=1)
    d.bus("select", cs)
    d.cond("start", owner="a", prof=d.profile("P", nom=NOM, min=0, max=INF))
    d.cond("stop", owner="a", prof=d.profile("W", nom=30, min=0, max=INF))
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1))
    (s_do, e_do), (s_f, e_f) = conds(t, 0), conds(t, 1)
    assert (s_f.tb, s_f.cycle) == (s_do.cycle, s_do.cycle + EDGE_LAT + 1 + 1)
    assert (e_f.tb, e_f.cycle) == (e_do.cycle, e_do.cycle + EDGE_LAT + 1 + 1)


# ---------------------------------------------------------------- MEAS
def meas_desc(maxv):
    d = Description("m", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("P", nom=NOM, min=0, max=maxv), meas="r3")
    d.push("r3")
    d.halt()
    return compile_desc(d, check_timing=False)


def test_cond_meas_in_every_role():
    """MEAS (isa §4.2): rd := min(max(m, 0), 0xFFFF). The follower's m = e - tb, the owner's
    action - tb: 8 in every role."""
    t = run_sys(meas_desc(INF))
    assert [t.engines[i].rx for i in range(3)] == [[NOM]] * 3


def test_cond_meas_timeout_writes_ffff():
    t = run_sys(meas_desc(20), roles=(Role.P1,))
    assert "TIMEOUT" in kinds(t, 0) and t.engines[0].rx == [0xFFFF]


# ---------------------------------------------------------------- compiler rules
def test_start_is_owned_by_the_master_line_owner():
    with pytest.raises(CompileError, match="R1-bus"):
        i2c(lambda d, sda, scl, p: d.cond("start", owner="b", prof=p))


def test_start_needs_a_bus():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a")
    d.cond("start", owner="a", prof=d.profile("P", nom=NOM))
    d.halt()
    with pytest.raises(CompileError, match="S-bus"):
        compile_desc(d, check_timing=False)


def test_i2c_bus_lines_are_od():
    d = Description("t", parties=("a", "b"))
    sda = d.line("sda", owner="a")
    scl = d.line("scl", "od", owner="a")
    d.bus("i2c", sda, scl)
    d.halt()
    with pytest.raises(CompileError, match="S-bus"):
        compile_desc(d, check_timing=False)


def test_meas_taints_its_register():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("P", nom=NOM, max=INF), meas="r3")
    d.br("x", cond="z", reg="r3")
    d.label("x")
    d.halt()
    with pytest.raises(CompileError, match="R2-branch"):
        compile_desc(d, check_timing=False)


def test_a_followed_start_clears_a_stale_stop():
    """START clears both flags (isa §4.2). P1 arrives late, after the master's START, STOP and
    START: both flags are set. Its COND START completes at once and clears the STOP flag too, so
    its COND STOP waits for the master's next real STOP (200 cycles on) instead of completing on
    the stale one."""
    def body(d, sda, scl, p):
        w = d.profile("W", nom=40, min=0, max=INF)
        with d.only("b"):
            d.raw_delay(150)
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=w)
        d.cond("start", owner="a", prof=w)
        d.cond("stop", owner="a", prof=d.profile("L", nom=200, min=0, max=INF))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    master, late = conds(t, 0), conds(t, 1)
    assert [k for _, k in t.engines[1].bus_events][:3] == ["START", "STOP", "START"]
    assert late[0].tb == master[2].cycle       # its START is the second one, at once
    assert late[1].tb == master[3].cycle       # its STOP is the master's last one


def test_an_sda_edge_as_scl_rises_is_no_start():
    """I2C: the candidate needs SCL read 1 now and in the previous cycle. SCL is held low until
    the very tick SDA falls, so P1 sees both change in one cycle: no START."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=60))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(1, 3, NOM + 1 - 3, 0)])
    assert t.wire_edges[1] == [3, NOM + 1]   # SCL low from 3, back high with SDA's fall at 9
    assert not t.engines[1].bus_events and "TIMEOUT" in kinds(t, 1)


# ---------------------------------------------------------------- review of #33
def test_a_followed_stop_that_sees_start_exits_with_abort():
    """isa §4.2: a followed STOP that sees START is a bus event: with ABORT a bus exit (the I2C
    target waiting for STOP gets a repeated START). A glitch lifts SDA for one tick after the
    START, so P1 sees a STOP candidate then a new START while it waits for STOP."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=d.profile("W", nom=200, min=0, max=INF), abort=True)
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    t = run_sys(i2c(body, abort_label="top"), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 40, 1, 1)])
    assert "BUS" in kinds(t, 1) and not t.engines[1].violations
    assert 3 in [pc for pc, _ in t.engines[1].starts]  # abort_pc: the label at pc 3


def test_a_bus_exit_at_the_step_start_does_not_act_and_times_the_fetch():
    """A timed step with ABORT that starts with a flag set exits in its start cycle before it
    acts (P1's HOLD of OD line x never pulls it), and abort_pc starts as an abort's does: the
    first own cycle >= t + 1, then 2 later (isa §4.0). P1 runs alone; glitches make the bus: SDA
    falls at 9 (a START), rises for one tick at 30 and falls again (a second START) while P1 is
    in its RAW DELAY."""
    def body(d, sda, scl, p):
        x = d.line("x", "od", owner="b", idle=1)
        d.cond("start", owner="a", prof=p)
        with d.only("b"):
            d.raw_delay(60)
        d.hold(x, 0, owner="b", prof=d.profile("H", nom=8, min=0, max=INF), abort=True)
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    t = run_sys(i2c(body, abort_label="top"), roles=(Role.P1,),
                glitches=[Glitch(0, 9, 21, 0), Glitch(0, 31, 300, 0)])
    assert [k for _, k in t.engines[0].bus_events][-1] == "START"
    starts = dict(t.engines[0].starts)
    hold_start = starts[3]                          # START, ROLE_IF, RAW DELAY, HOLD
    (bus,) = [e for e in t.engines[0].events if e.kind == "BUS"]
    assert bus.cycle == hold_start
    assert not t.wire_edges[2]                      # x was never pulled low
    assert starts[5] == hold_start + 1 + ((hold_start + 1) & 1) + 2  # abort_pc ("top")


@pytest.mark.parametrize("g", [40, 41])
def test_a_bus_exit_mid_step_is_at_the_flag_and_releases_od_lines(g):
    """The decision is the cycle the flag becomes visible (bus_e + E + bus_confirm + 1, isa §6),
    and a bus exit releases the OD lines the engine holds."""
    def body(d, sda, scl, p):
        x = d.line("x", "od", owner="b", idle=1)
        d.hold(x, 0, owner="b", prof=d.profile("H", nom=4, min=0, max=INF))   # P1 pulls x low
        d.cond("start", owner="a", prof=p)                                    # at 12, P1 done at 31
        d.hold(sda, 0, owner="a", prof=d.profile("L", nom=120, min=0, max=INF), abort=True)
        with d.only("a"):
            d.raw_delay(200)  # the master keeps the bus: its HALT would release SDA (a STOP)
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    # SDA lifted for one tick at g while P1 follows the long HOLD (its sample would be at 75);
    # two glitch ticks give an odd and an even decision cycle
    t = run_sys(i2c(body, abort_label="top"), roles=(Role.P0, Role.P1), glitches=[Glitch(0, g, 1, 1)])
    (bus,) = [e for e in t.engines[1].events if e.kind == "BUS"]
    assert bus.cycle == g + 1 + 2 + CONFIRM + 1     # SDA back low on the wire at g + 1, seen 2 later
    assert t.wire_edges[2][-1] == bus.cycle + 1     # x released by the exit


def test_a_followed_start_ignores_stop():
    """A followed START waits through a STOP: a glitch lifts SDA for 30 ticks after the first
    START, so P1 sees a STOP, then a new START when SDA falls back; its second followed START
    anchors on that one."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("start", owner="a", prof=p)  # SDA is already low: no edge of its own
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 40, 30, 1)])
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP", "START"]
    assert conds(t, 1)[1].tb == 40 + 30 - 1         # the fall on the wire at 70: e = 69


def test_followed_start_meas_and_done_meas_use_the_real_times():
    """MEAS on a followed START is start_e - tb; a done COND's is action - tb, here a late action
    (the master starts 20 cycles late), so it differs from nom."""
    def body(d, sda, scl, p):
        with d.only("a"):
            d.raw_delay(20)
        d.cond("start", owner="a", prof=p, meas="r2")
        d.push("r2")
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    act = conds(t, 0)[0].cycle
    assert act > NOM and t.engines[0].rx == [act] and t.engines[1].rx == [act]


def test_select_confirm_can_fail():
    """SELECT with confirm 5: CS dips low for 2 ticks and comes back; no START from it."""
    d = Description("sel", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    d.bus("select", cs, confirm=5)
    d.cond("start", owner="a", prof=d.profile("P", nom=60, min=0, max=INF))
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 10, 2, 0)])
    # the dip's fall fails its confirm; its rise is a real STOP (CS rising and staying high)
    starts = [c for c, k in t.engines[1].bus_events if k == "START"]
    assert len(starts) == 1 and starts[0] > 60


def test_a_new_edge_at_the_confirm_cycle_confirms_first():
    """If SCL stayed high for the whole window, the pending START is confirmed even when a new
    SDA edge is seen in the confirm cycle itself; that edge starts a new candidate."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=d.profile("W", nom=200, min=0, max=INF))  # P1 waits
    rise = CONFIRMED - 2                            # SDA lifted on the wire then: seen at CONFIRMED
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(0, rise, 40, 1)])
    assert [k for _, k in t.engines[1].bus_events][:2] == ["START", "STOP"]


def test_an_se0_bus_fails_loudly():
    from dataclasses import replace
    from emu.image import BusCfg
    from emu.isa import BusKind
    img = replace(i2c(start_body), bus=BusCfg(BusKind.SE0, 0, 1))
    with pytest.raises(NotImplementedError, match="SE0"):
        run_sys(img)


def test_i2c_needs_its_lane_and_a_party_owned_scl():
    """S-bus (isa §5): I2C START/STOP are used with a cpol 1, cpha 1 lane on (bus_a, bus_b), so
    SCL has a party owner; a shared SCL would make both parties detect their own START."""
    def desc(scl_owner, lane):
        d = Description("t", parties=("a", "b"))
        sda = d.line("sda", "od", owner="shared", idle=1)
        scl = d.line("scl", "od", owner=scl_owner, idle=1)
        d.bus("i2c", sda, scl)
        if lane:
            d.lane("L", sda, clk=scl, cpol=1, cpha=1)
        d.cond("start", owner="a" if scl_owner == "a" else "shared", prof=d.profile("P", nom=NOM))
        d.halt()
        return d
    compile_desc(desc("a", lane=True), check_timing=False)
    for scl_owner, lane in (("a", False), ("shared", True)):
        with pytest.raises(CompileError, match="S-bus|R1"):
            compile_desc(desc(scl_owner, lane), check_timing=False)


def test_unclocked_abort_without_check_is_allowed_on_a_bus():
    """S-flags: an unclocked XFER may have ABORT without CHK when a bus is active."""
    def body(d, sda, scl, p):
        d.xfer(8, d.line("u", owner="a"), owner="a", prof=d.profile("U", nom=16), abort=True)
        d.label("end")
    i2c(body, abort_label="end")


def test_t5_abort_on_a_bus_needs_a_bus_cond_or_no_abort():
    """After a bus exit the flag stays set, so a step at abort_pc with ABORT that is not a
    followed COND START/STOP would exit again at once, forever: T5-abort rejects it."""
    def body(d, sda, scl, p):
        x = d.line("x", owner="a", idle=1)
        d.cond("start", owner="a", prof=p)
        d.hold(sda, 0, owner="a", prof=d.profile("L", nom=80, min=0, max=INF), abort=True)
        d.halt()
        d.label("top")
        d.cond("fall", x, owner="a", prof=p, abort=True)
    with pytest.raises(CompileError, match="T5-abort"):
        compile_desc(i2c_desc(body, abort_label="top"))


# ---------------------------------------------------------------- gated lines (isa §2, §4.7)
def select_desc(body, abort_label=None):
    """SELECT: CS (line 0) owned by a; MISO (line 1) a gated PP line owned by b; x (line 2) a
    plain PP line owned by b. `body(d, cs, miso, x, p)` builds the program."""
    d = Description("sel", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    miso = d.line("miso", owner="b", idle=1, gated=True)
    x = d.line("x", owner="b", idle=1)
    d.bus("select", cs)
    if abort_label:
        d.abort_to(abort_label)
    body(d, cs, miso, x, d.profile("P", nom=NOM, min=0, max=INF))
    d.halt()
    return compile_desc(d, check_timing=False)


def stops(t, i):
    return [c for c, k in t.engines[i].bus_events if k == "STOP"]


def test_a_gated_line_is_in_the_image_and_only_on_pp():
    img = select_desc(lambda d, cs, miso, x, p: None)
    assert [ln.gated for ln in img.lines] == [False, True, False]
    d = Description("g", parties=("a", "b"))
    d.line("o", "od", owner="a", idle=1, gated=True)
    d.halt()
    with pytest.raises(CompileError, match="S-line"):
        compile_desc(d, check_timing=False)


def test_a_select_stop_releases_gated_lines_only():
    """b drives MISO and x low; a raises CS (STOP). b's detector confirms the STOP at c and
    releases MISO, so the wire is 1 again from c + 1; x, not gated, stays low (isa §2)."""
    def body(d, cs, miso, x, p):
        d.cond("start", owner="a", prof=p)
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
        d.hold(x, 0, owner="b", prof=d.profile("H2", nom=10))
        d.cond("stop", owner="a", prof=d.profile("W", nom=30, min=0, max=INF))
    t = run_sys(select_desc(body), roles=(Role.P0, Role.P1))
    (c,) = stops(t, 1)
    assert len(t.wire_edges[1]) == 2 and t.wire_edges[1][1] == c + 1
    assert len(t.wire_edges[2]) == 1


def test_a_stop_mid_step_keeps_the_gated_line_released_until_the_next_owned_step():
    """CS is lifted by a glitch while b sends 0x00 on MISO without ABORT: after the STOP the
    rest of the XFER drives nothing, and b's next owned step on MISO (a HOLD 0) drives it again."""
    def body(d, cs, miso, x, p):
        d.cond("start", owner="a", prof=p)
        d.xfer(8, miso, owner="b", prof=d.profile("U", nom=10), src="zero")
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
    t = run_sys(select_desc(body), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 40, 400, 1)])
    (c,) = stops(t, 1)
    first = next(now for pc, now in t.engines[1].starts if pc == 1)
    xfer, hold = [r for r in t.engines[1].records if r.op in ('XFER', 'HOLD')]
    assert first < c < xfer.cycle - 20                 # the STOP lands mid-XFER
    halt = t.engines[1].starts[-1][1]
    assert t.wire_edges[1] == [first + 1, c + 1, hold.tb - 10 + 1, halt + 1]  # the HOLD drives at its planned tb


def test_an_abort_releases_gated_lines():
    """b drives MISO low, then times out on a COND with ABORT (a is late): the abort releases
    MISO in its cycle (isa §2), as it does OD lines."""
    def body(d, cs, miso, x, p):
        y = d.line("y", owner="a", idle=1)
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
        with d.only("a"):
            d.raw_delay(200)
        d.cond("fall", y, owner="a", prof=d.profile("T", nom=NOM, min=0, max=20), abort=True)
        d.label("top")
        d.cond("rise", y, owner="a", prof=p)
    t = run_sys(select_desc(body, abort_label="top"), roles=(Role.P0, Role.P1))
    (ab,) = [e for e in t.engines[1].events if e.kind == "ABORT"]
    assert t.wire_edges[1][-1] == ab.cycle + 1


def test_a_bus_exit_releases_gated_lines():
    """After START and STOP, b drives MISO low and follows a long COND with ABORT; a glitch
    drops CS (a START): the bus exit releases MISO in its cycle."""
    def body(d, cs, miso, x, p):
        y = d.line("y", owner="a", idle=1)
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=p)
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
        d.cond("fall", y, owner="a", prof=d.profile("L", nom=300, min=0, max=INF), abort=True)
        d.label("top")
        d.cond("start", owner="a", prof=p)
    t = run_sys(select_desc(body, abort_label="top"), roles=(Role.P0, Role.P1),
                glitches=[Glitch(0, 120, 400, 0)])
    (bus,) = [e for e in t.engines[1].events if e.kind == "BUS"]
    assert t.wire_edges[1][-1] == bus.cycle + 1


def test_an_i2c_stop_leaves_gated_lines_driven():
    """Only a SELECT STOP releases gated lines (isa §4.7): on I2C, b's gated PP line stays low
    through the STOP."""
    def body(d, sda, scl, p):
        g = d.line("g", owner="b", idle=1, gated=True)
        d.cond("start", owner="a", prof=p)
        d.hold(g, 0, owner="b", prof=d.profile("H", nom=10))
        d.hold(sda, 0, owner="a", prof=d.profile("Z", nom=10))
        d.cond("stop", owner="a", prof=d.profile("W", nom=30, min=0, max=INF))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP"]
    (c,) = stops(t, 1)
    halt = t.engines[1].starts[-1][1]
    assert c < halt and t.wire_edges[2][1:] == [halt + 1]   # g stayed low through the STOP, until HALT


# ---------------------------------------------------------------- review of #39
def test_halt_releases_gated_lines():
    """A halted engine runs no detector, so HALT releases its gated lines itself (isa §4.1):
    otherwise a target that halts mid-frame would hold MISO forever."""
    def body(d, cs, miso, x, p):
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
    t = run_sys(select_desc(body), roles=(Role.P0, Role.P1))
    halt = t.engines[1].starts[-1][1]
    assert t.wire_edges[1] == [3, halt + 1]


def test_a_gated_line_idles_at_1_and_is_not_the_select_masters():
    """S-line: a released gated line reads 1 (the pull-up), so its idle is 1; and the SELECT
    master runs no detector, so a gated line it owns would never see the STOP."""
    def desc(owner, idle):
        d = Description("g", parties=("a", "b"))
        cs = d.line("cs", owner="a", idle=1)
        d.line("g", owner=owner, idle=idle, gated=True)
        d.bus("select", cs)
        d.halt()
        return d
    compile_desc(desc("b", 1), check_timing=False)
    for owner, idle in (("b", 0), ("a", 1)):
        with pytest.raises(CompileError, match="S-line"):
            compile_desc(desc(owner, idle), check_timing=False)


def test_the_gate_starts_shut_at_run():
    """A gated line is driven only from the first timed step: a RAW SET before it does nothing."""
    def body(d, cs, miso, x, p):
        with d.only("b"):
            d.raw_set(miso, 0)
            d.raw_delay(20)
        d.hold(miso, 0, owner="b", prof=d.profile("H", nom=10))
    t = run_sys(select_desc(body), roles=(Role.P0, Role.P1))
    (hold,) = [r for r in t.engines[1].records if r.op == "HOLD"]
    start = dict(t.engines[1].starts)[hold.pc]
    halt = t.engines[1].starts[-1][1]
    assert t.wire_edges[1] == [max(start, hold.tb - 10) + 1, halt + 1]   # first driven by the HOLD (late here)


def test_a_stop_confirmed_as_a_step_starts_cuts_that_step():
    """A release in the cycle a timed step starts wins over that step's opening of the gate (the
    detector runs at the end of the cycle): that HOLD drives nothing, the next one drives again."""
    def body(d, cs, miso, x, p):
        d.cond("start", owner="a", prof=p)
        h = d.profile("H", nom=10)
        for _ in range(4):
            d.hold(miso, 0, owner="b", prof=h)
    img = select_desc(body)
    plain = run_sys(img, roles=(Role.P0, Role.P1))
    s2 = dict(plain.engines[1].starts)[3]           # the third HOLD's start
    t = run_sys(img, roles=(Role.P0, Role.P1), glitches=[Glitch(0, s2 - 3, 2000, 1)])
    assert stops(t, 1) == [s2]                      # seen at s2 - 1, confirmed after 1 cycle
    holds = [r for r in t.engines[1].records if r.op == "HOLD"]
    halt = t.engines[1].starts[-1][1]
    assert t.wire_edges[1] == [plain.wire_edges[1][0], s2 + 1, holds[3].tb - 10 + 1, halt + 1]


def test_a_gated_data2_is_cut_by_a_stop_and_drives_the_next_frame():
    """Duplex SPI: MISO is a gated data2, driven by the target in a step it follows. CS lifted
    for 30 ticks mid-frame 1: the rest of frame 1 drives nothing; frame 2 drives MISO again."""
    from emu.model.system import EngineSpec, System
    d = Description("spi", parties=("m", "s"))
    cs = d.line("cs", owner="m", idle=1)
    sck = d.line("sck", owner="m", idle=0)
    mosi = d.line("mosi", owner="m", idle=0)
    miso = d.line("miso", owner="s", idle=1, gated=True)
    d.bus("select", cs)
    L = d.lane("L", mosi, clk=sck, cpol=0, cpha=0, data2=miso)
    d.cond("start", owner="m", prof=d.profile("S", nom=40, min=0, max=INF))
    h = d.profile("H", nom=20, min=10, max=60)
    d.xfer(8, L, owner="m", prof=h, src="r1", dst="r2")
    d.xfer(8, L, owner="m", prof=h, src="r1", dst="r2")
    d.cond("stop", owner="m", prof=d.profile("W", nom=200, min=0, max=INF))
    with d.only("m"):
        d.raw_delay(400)
    d.halt()
    s = System([EngineSpec(compile_desc(d), r) for r in (Role.P0, Role.P1)], glitches=[Glitch(0, 150, 30, 1)])
    s.engines[1].regs[1] = 0x55                     # MISO toggles every bit
    t = s.run(50_000)
    c = stops(t, 1)[0]
    frame1_end = [r for r in t.engines[1].records if r.op == "XFER"][0].cycle
    edges = t.wire_edges[3]
    assert c < frame1_end and not [e for e in edges if c + 1 < e <= frame1_end]   # frame 1 cut
    assert len([e for e in edges if e <= c + 1]) % 2 == 0           # released: MISO reads 1
    assert len([e for e in edges if e > frame1_end]) >= 4            # frame 2 drives it


# ---------------------------------------------------------------- bus timing in the analysis
@pytest.mark.parametrize("delay,ok", [(16, True), (30, False)])
def test_a_followed_start_may_start_until_its_confirm(delay, ok):
    """T5-watch for a followed COND START/STOP (isa §6): the detector runs every cycle and its
    flag is sticky, so the follower only has to be waiting by the confirm, E + bus_confirm + 1
    after the owner's planned edge, not by the edge itself. P1 starts its followed START late (a
    RAW DELAY first); within that bound it compiles with every timing rule and completes when an
    on-time follower does; past it, T5-watch rejects it."""
    def body(d, sda, scl, p):
        with d.only("b"):
            d.raw_delay(delay)
        d.cond("start", owner="a", prof=p)
    d = i2c_desc(body)
    if not ok:
        with pytest.raises(CompileError, match="T5-watch"):
            compile_desc(d)
        return
    t = run_sys(compile_desc(d), roles=(Role.P0, Role.P1))
    (f,) = conds(t, 1)
    start = dict(t.engines[1].starts)[2]            # ROLE_IF, RAW DELAY, COND START
    assert start > SEEN and (f.cycle, f.tb) == (CONFIRMED + 1, NOM)


def bus_sequence(nom2, ppm):
    """START (nom 40), then STOP, START, STOP, each nom2 after the one before."""
    d = Description("bus", parties=("a", "b"), tolerance_ppm=ppm)
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="a", idle=1)
    d.bus("i2c", sda, scl, confirm=CONFIRM)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    p = d.profile("P", nom=40, min=0, max=INF)
    q = d.profile("Q", nom=nom2, min=0, max=INF)
    for i, k in enumerate(("start", "stop", "start", "stop")):
        d.cond(k, owner="a", prof=p if i == 0 else q)
    with d.only("a"):
        d.raw_delay(200)
    d.halt()
    return d


@pytest.mark.parametrize("ppm", [0, 20000])
def test_back_to_back_bus_conds_are_spaced_by_the_confirm(ppm):
    """A bus edge inside the previous one's confirm window replaces its candidate (isa §4.7), so
    a START followed by a STOP too soon is never confirmed and the followers wait in the START
    forever. T5-watch: a followed START/STOP right after another bus COND needs nom - drift -
    QUANT >= bus_confirm. The shortest accepted spacing runs clean with the followers 2% off."""
    def compiles(n):
        try:
            compile_desc(bus_sequence(n, ppm))
            return True
        except CompileError:
            return False
    shortest = next(n for n in range(1, 60) if compiles(n))
    assert shortest >= CONFIRM
    with pytest.raises(CompileError, match="T5-watch"):
        compile_desc(bus_sequence(shortest - 1, ppm))
    img = compile_desc(bus_sequence(shortest, ppm))
    slow, fast = (2451, 10000), (2549, 10000)
    for clocks in ([{}] if ppm == 0 else [{0: (2500, 10000), 1: slow, 2: slow}, {0: (2500, 10000), 1: fast, 2: fast}]):
        t = run_sys(img, clocks=clocks)
        for i in (1, 2):
            assert [k for _, k in t.engines[i].bus_events] == ["START", "STOP", "START", "STOP"], (clocks, i)
            assert len(conds(t, i)) == 4 and not t.engines[i].violations, (clocks, i)


def test_a_followed_start_that_starts_when_the_flag_is_already_set():
    """At 0 ppm the T5-watch bound lets the follower start in the very cycle the flag becomes
    visible: it completes there, anchored on start_e, as an on-time follower does. With confirm
    14 that cycle (SEEN + 14 + 1 = 26) is even, so an instruction can start in it."""
    visible = SEEN + 14 + 1
    def body(d, sda, scl, p):
        with d.only("b"):
            d.raw_delay(visible - 4)                 # ROLE_IF at 2, RAW DELAY at 4: START at 26
        d.cond("start", owner="a", prof=p)
    d = i2c_desc(body, confirm=14)
    d.tolerance_ppm = 0
    t = run_sys(compile_desc(d), roles=(Role.P0, Role.P1))
    start = dict(t.engines[1].starts)[2]
    (f,) = conds(t, 1)
    assert start == visible and t.engines[1].bus_events == [(visible - 1, "START")]
    assert (f.cycle, f.tb) == (visible, NOM)
