"""Model tests written because a mutant of the model survived every other test (emu/mutate.py,
verification.md §1). Each pins behaviour isa.md specifies and nothing else checked: the event
register's bits, what an abort keeps and releases, boundaries the other tests stay clear of, and
completion cycles against the compiler's offsets (isa §6)."""
import pytest

from emu.analysis import _steps, completion
from emu.compiler import compile_desc, lower
from emu.desc import Description
from emu.isa import EDGE_LAT, INF, QUANT, Role
from emu.model.engine import Engine
from emu.model.system import EngineSpec, Glitch, System

from simutil import kinds, run_sys

P = 16


def follower(d: Description, wire, cycles: int, role=Role.P1, **kw) -> Engine:
    """One engine of `d`, fed `wire(t)` (a list of line levels) for `cycles` cycles."""
    e = Engine(compile_desc(d, check_timing=False, **kw), role)
    for t in range(cycles):
        e.cycle(wire(t))
    return e


def kinds_of(e: Engine) -> list[str]:
    return [ev.kind for ev in e.events]


# ---------------------------------------------------------------- the event register
@pytest.mark.parametrize("kind,bit", [("TIMEOUT", 2), ("LEVEL", 3), ("EDGE", 3), ("CLK_PHASE", 3),
                                      ("OVERFLOW", 4), ("UNDERRUN", 5)])
def test_each_failure_kind_sets_its_evt_bit(kind, bit):
    """isa §4.1: evt bit 2 TIMEOUT, 3 MISMATCH (LEVEL, EDGE, CLK_PHASE), 4 OVERFLOW, 5 UNDERRUN."""
    d = Description("e", parties=("a", "b"))
    d.halt()
    e = Engine(compile_desc(d), Role.P0)
    e.log(kind, measured=1)
    assert e.evt == 1 << bit


# ---------------------------------------------------------------- aborts
def test_an_abort_releases_od_lines_keeps_pp_levels_and_restarts_tb():
    """isa §4.1: an abort releases OD lines, sets tb := now and goes to abort_pc; PP lines keep
    their level. P1 drives its PP and OD lines low, then follows a start bit that is a one-cycle
    pulse: the HOLD reads 1, logs LEVEL and aborts to a COND that waits for ever."""
    d = Description("a", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    pp = d.line("pp", owner="b", idle=1)
    od = d.line("od", kind="od", owner="b", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.abort_to("top")
    with d.only("b"):
        d.raw_set(pp, 0)
        d.raw_set(od, 0)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit, abort=True)
    d.label("top")
    d.cond("rise", ln, owner="a")
    d.halt()
    e = follower(d, lambda t: [0 if t == 20 else 1, 1, 1], 200, check_levels=False)
    assert kinds_of(e) == ["LEVEL", "ABORT"]
    abort = next(ev for ev in e.events if ev.kind == "ABORT")
    assert e.tb == abort.cycle and e.anchor == abort.cycle
    assert e.drv[1] == 0 and e.drv[2] is None
    assert e.pc == e.image.abort_pc and not e.halted


# ---------------------------------------------------------------- COND windows
def cond_desc(prof, meas=True):
    d = Description("c", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("W", **prof), meas="r1" if meas else None)
    d.halt()
    return d


MIN, MAX = 20, 40


@pytest.mark.parametrize("fall", range(MIN - QUANT - 3, MAX + QUANT + 6))
def test_a_followed_cond_is_early_below_min_minus_quant_and_times_out_past_max_plus_quant(fall):
    """isa §4.2: m = e - tb; EARLY when min > 0 and m < min - QUANT; with max < INF and no edge,
    TIMEOUT once now - EDGE_LAT > tb + max + QUANT (an edge seen in that cycle still wins), and
    a timed-out COND measures 0xFFFF. Swept across both boundaries; tb = 0 at reset."""
    d = cond_desc(dict(nom=30, min=MIN, max=MAX))
    e = follower(d, lambda t: [1 if t < fall else 0], 200)
    m = fall - 1  # the wire fed at t is the engine's cycle t + 1, so the edge is at e = fall - 1
    if m > MAX + QUANT + 1:
        assert kinds_of(e) == ["TIMEOUT"] and e.to and e.regs[1] == 0xFFFF
        assert e.events[0].cycle - EDGE_LAT == MAX + QUANT + 1
        assert e.events[0].measured == MAX + QUANT + 1  # TIMEOUT(m): m = now - EDGE_LAT - tb
        assert e.tb == 30  # tb := tb + nom
    else:
        assert e.regs[1] == m and e.tb == m and not e.to
        assert kinds_of(e) == (["EARLY"] if m < MIN - QUANT else [])


# ---------------------------------------------------------------- unclocked XFER windows (isa §4.3)
def chk_frame():
    """COND fall · HOLD 0 · XFER 4 ONES with CHK · HALT, all a's. Bit 0 is 1, so the only edge is
    the rise at boundary 0."""
    d = Description("w", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(4, ln, owner="a", prof=bit, src="ones", dst="r1", check=True)
    d.halt()
    return d


FALL = 10


@pytest.mark.parametrize("rise", range(P // 2 + 2, P + 8))
def test_a_chk_edge_at_boundary_0_is_checked_against_the_accumulated_window(rise):
    """isa §4.3: an edge seen before sample 0 belongs to boundary 0 and is checked against the
    anchor (the COND's edge) plus the accumulated window (the HOLD: [P - 2, P + 2]) widened by
    QUANT, from the XFER's first cycle on. The line falls at FALL and rises `rise` cycles later."""
    lo, hi = P - 2 - QUANT, P + 2 + QUANT
    e = follower(chk_frame(), lambda t: [0 if FALL <= t < FALL + rise else 1], 300)
    xfer_pc = 2
    start = next(now for pc, now in e.starts if pc == xfer_pc)
    seen = FALL + rise + EDGE_LAT  # fed at t (the engine's cycle t + 1), seen EDGE_LAT - 1 later
    checked = seen >= start
    assert ("EDGE" in kinds_of(e)) == (checked and not lo <= rise <= hi), (start, seen)
    assert e.regs[1] == 0xF


def test_a_done_hold_accumulates_into_the_window_of_the_next_followed_xfer():
    """isa §4.3: owners and followers both accumulate. P1 does a COND and a HOLD on its line,
    then follows P0's CHK XFER of zeros on P0's line: its one edge, at boundary 0, sits one HOLD
    after P1's COND, inside the window only if P1's own HOLD was accumulated."""
    d = Description("acc", parties=("a", "b"))
    la = d.line("la", owner="a", idle=1)
    lb = d.line("lb", owner="b", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.cond("fall", lb, owner="b")
    d.hold(lb, 0, owner="b", prof=bit)
    d.xfer(4, la, owner="a", prof=bit, src="zero", dst="r1", check=True)
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False, check_levels=False))
    assert not any(et.events for et in t.engines)
    assert [r.mode for r in t.engines[1].records] == ["do", "do", "follow"]


# ---------------------------------------------------------------- completion against the compiler
@pytest.mark.parametrize("kind", ["pp", "od"])
@pytest.mark.parametrize("n", [1, 8])
def test_hold_and_unclocked_xfer_complete_where_the_compiler_says(kind, n):
    """isa §6 completion offsets, relative to the step's end tb, in every role: HOLD and
    unclocked XFER, a PP owner (it completes when it drives its last bit) and an OD owner (it
    reads back, so it completes as a follower does)."""
    d = Description("c", parties=("a", "b"))
    ln = d.line("l", kind, owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.li("r7", 0x5A)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(n, ln, owner="a", prof=bit, src="r7", dst="discard")
    d.hold(ln, 1, owner="a", prof=bit)
    d.halt()
    steps = _steps(lower(d))
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        recs = [x for x in t.engines[i].records if x.op in ("HOLD", "XFER")]
        assert len(recs) == 3
        for rec in recs:
            lo, hi = completion(steps[rec.pc], r)
            assert lo <= rec.cycle - rec.tb <= hi, (r, rec)


# ---------------------------------------------------------------- the timeout flag
@pytest.mark.parametrize("step", ["hold", "xfer", "clocked"])
def test_every_timed_step_clears_to_when_it_starts(step):
    """isa §4.1: `to` is cleared when a timed step starts and set on TIMEOUT without ABORT. P1's
    bounded COND times out (the line never falls), then it follows a HOLD, an unclocked XFER or
    a clocked XFER, which clears `to` (the clocked one's clock is a's, never driven: unbounded,
    it would wait for ever, so it is bounded too and its own timeout sets `to` again)."""
    d = Description("to", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    clk = d.line("c", owner="a", idle=0)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    # max close to nom: the timeout comes at tb + max + QUANT + EDGE_LAT, before the next sample
    d.cond("fall", ln, owner="a", prof=d.profile("W", nom=20, min=0, max=22))
    if step == "hold":
        d.hold(ln, 1, owner="a", prof=bit)
    elif step == "xfer":
        d.xfer(2, ln, owner="a", prof=bit, src="ones", dst="r1")
    else:
        lane = d.lane("L", ln, clk=clk)
        d.xfer(2, lane, owner="a", prof=d.profile("H", nom=4, min=0, max=10), src="ones", dst="r1")
    d.li("r2", 7)
    d.halt()
    e = follower(d, lambda t: [1, 0], 400, check_levels=False)
    assert e.halted and e.regs[2] == 7  # a timed-out step falls through to the next instruction
    if step == "clocked":
        assert kinds_of(e) == ["TIMEOUT", "TIMEOUT"] and e.to
    else:
        assert kinds_of(e) == ["TIMEOUT"] and not e.to


def test_a_stretch_timeout_without_abort_sets_to():
    """isa §4.3: the clock owner's wait for a stretched clock is bounded by max; a TIMEOUT
    without ABORT sets `to` and ends the step (the setup of test_a_stretch_past_max_times_out)."""
    from test_engine_clocked import stretch_img, trailing
    s = System([EngineSpec(stretch_img(hi=12), r) for r in (Role.P0, Role.P1, Role.MON)],
               glitches=[Glitch(0, trailing(2) + 1, 40, 0)])
    s.engines[1].image = s.engines[2].image = stretch_img()
    t = s.run(5000)
    assert "TIMEOUT" in kinds(t, 0) and "ABORT" not in kinds(t, 0) and s.engines[0].to


def test_clk_phase_is_checked_only_with_chk():
    """isa §4.3: CLK_PHASE is a clock follower's check with CHK. Without CHK, a clock owner at
    half the period gives no CLK_PHASE (test_clock_follower_clk_phase has CHK)."""
    from test_engine_clocked import spi
    img = spi(check=False, lo=4, hi=6)
    t = run_sys(img, images={0: img.with_profiles({"H": 0.5})})
    assert not any("CLK_PHASE" in kinds(t, i) for i in range(3))


# ---------------------------------------------------------------- branches and the bus
def test_raw_jpin_lands_on_an_odd_target():
    """isa §3: RAW JPIN jumps to its 11-bit target, odd or even. The ROLE_IF is pc 0, the JPIN
    pc 1, its target pc 3."""
    d = Description("j", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.raw_jpin(ln, 1, "x")
        d.nop()
    d.label("x")
    d.li("r1", 7)
    d.halt()
    e = follower(d, lambda t: [1], 40, role=Role.P0)
    assert [pc for pc, _ in e.starts] == [0, 1, 3, 4] and e.regs[1] == 7


def test_a_followed_stop_that_sees_start_keeps_waiting_without_abort():
    """isa §4.2: a followed STOP that sees START exits only with ABORT; without it, it keeps
    waiting and completes on the STOP (test_a_followed_stop_that_sees_start_exits_with_abort is
    the same run with ABORT)."""
    from test_engine_bus import i2c
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=d.profile("W", nom=200, min=0, max=INF))
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    t = run_sys(i2c(body, abort_label="top"), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 40, 1, 1)])
    assert "BUS" not in kinds(t, 1) and not t.engines[1].violations
    assert [r.op for r in t.engines[1].records] == ["COND", "COND"]
    assert 3 not in [pc for pc, _ in t.engines[1].starts]


def test_a_followed_start_measures_from_its_own_tb():
    """isa §4.2: MEAS on a followed START is start_e - tb. A STOP first, so tb is not 0: the
    follower measures from the STOP, as the master does from its own. Each bus COND comes 30
    cycles after the last, past its 15-cycle confirm (T5-watch)."""
    from test_engine_bus import conds, i2c
    def body(d, sda, scl, p):
        gap = d.profile("G", nom=30, min=0, max=INF)
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=gap)
        with d.only("a"):
            d.raw_delay(20)
        d.cond("start", owner="a", prof=gap, meas="r2")
        d.push("r2")
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    stop, start = conds(t, 0)[1:3]
    assert stop.tb > 0 and t.engines[0].rx == t.engines[1].rx == [start.cycle - stop.cycle]


# ---------------------------------------------------------------- clocked XFER
def test_a_clock_follower_timeout_measures_from_its_start():
    """isa §4.3: TIMEOUT(m) for a bounded clock follower with no clock: m counts from its start,
    first past max + QUANT (the setup of test_clock_follower_timeout_from_the_step_start)."""
    from test_engine_clocked import H, spi
    t = System([EngineSpec(spi(hi=H + 2), Role.P1)]).run(1000)
    (ev,) = t.engines[0].events
    assert ev.kind == "TIMEOUT" and ev.measured == (H + 2) + QUANT + 1


def test_a_clocked_xfer_writes_r0():
    """isa §4.3: dst may be any register, r0 too, in every role (spi_desc loads r0 with VALUE
    first, so the XFER sends ONES)."""
    from test_engine_clocked import VALUE, spi
    t = run_sys(spi(dst="r0", src="ones"))
    assert VALUE != 0xFF and [et.regs[0] for et in t.engines] == [0xFF] * 3


def test_a_clocked_xfer_re_anchors_the_chk_window():
    """isa §4.3: a COND or a clocked XFER re-anchors the window. After a 64-cycle clocked XFER, a
    HOLD and a CHK XFER of zeros on another line: its edge at boundary 0 is one HOLD after the
    clocked XFER's end, inside the window only if that end is the anchor. (A CHK XFER right
    after it would be at A = 0, which T2-window rejects.)"""
    d = Description("ra", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=0)
    dat = d.line("d", owner="a", idle=1)
    u = d.line("u", owner="a", idle=1)
    lane = d.lane("L", dat, clk=sck)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.li("r0", 0)  # T5-startup: not a clocked XFER on an idle-0 clock at instruction 0
    d.xfer(8, lane, owner="a", prof=d.profile("H", nom=4, min=3, max=INF), src="ones", dst="r1")
    d.hold(u, 1, owner="a", prof=bit)
    d.xfer(4, u, owner="a", prof=bit, src="zero", dst="r2", check=True)
    d.halt()
    t = run_sys(compile_desc(d))
    assert not any(et.events for et in t.engines)
    assert [len(et.records) for et in t.engines] == [3, 3, 3]


# ---------------------------------------------------------------- the system
def test_tx_left_counts_the_fifo_and_the_backlog():
    """tx_left: the words in TX (at most 4, isa §1) plus those the host still holds."""
    from test_system import _pop_loop
    s = System([EngineSpec(_pop_loop(0), Role.P0)])
    s.host_push(0, [1, 2, 3, 4, 5, 6])
    et = s.run(1000).engines[0]
    assert len(s.engines[0].tx) == 4 and et.tx_left == 6
