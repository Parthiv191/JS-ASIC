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


def test_a_stretch_timeout_without_abort_sets_to_and_falls_through():
    """isa §4.3: the clock owner's wait for a stretched clock is bounded by max, counted from the
    previous edge; a TIMEOUT(m) without ABORT sets `to` and ends the step, and the next
    instruction runs (the setup of test_a_stretch_past_max_times_out)."""
    from test_engine_clocked import HS, stretch_desc, trailing

    def body(d, scl, sda, lane, prof):
        d.xfer(8, lane, owner="a", prof=prof, src="ones", dst="r4")
        d.li("r2", 7)
    bounded = compile_desc(stretch_desc(hi=HS + 4, body=body), check_timing=False)
    free = compile_desc(stretch_desc(body=body), check_timing=False)
    s = System([EngineSpec(bounded, Role.P0), EngineSpec(free, Role.P1), EngineSpec(free, Role.MON)],
               glitches=[Glitch(0, trailing(2) + 1, 40, 0)])
    t = s.run(5000)
    (ev,) = [e for e in t.engines[0].events if e.kind == "TIMEOUT"]
    assert ev.measured == HS + 4 + QUANT + 1 and "ABORT" not in kinds(t, 0)
    assert s.engines[0].to and t.engines[0].regs[2] == 7 and t.engines[0].halted


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


# ---------------------------------------------------------------- FZ auto-tokens (isa §4.6)
def armed_pair(build, armer="ctl", peer=True, peer_scale=None):
    """The program `build()` makes, with `armer` as engine 0 holding the unit; the other party
    (engine 1) runs it with its profiles scaled by `peer_scale`, if given."""
    from emu.fuzz.unit import FuzzConfig, FuzzUnit
    d = build()
    img = compile_desc(d, check_timing=False, check_levels=False)
    u = FuzzUnit(FuzzConfig(length=1))
    u.load(0, bytes([0xA4]))
    roles = {"ctl": Role.P0, "per": Role.P1, "tgt": Role.P1}
    other = {"ctl": "per" if "per" in d.parties else "tgt", "per": "ctl", "tgt": "ctl"}[armer]
    specs = [EngineSpec(img, roles[armer], engine=0, fuzz=u)]
    if peer:
        specs.append(EngineSpec(img.with_profiles(peer_scale) if peer_scale else img, roles[other], engine=1))
    return u, System(specs).run(20_000).engines[0]


def uart_from_tgt(n=9, value=0x1BC, prof=None):
    def build():
        from emu.protocols import slack
        d = Description("w", parties=("ctl", "tgt"))
        rx = d.line("rx", owner="tgt", idle=1)
        bit = d.profile("BIT", nom=20, min=20 - slack(20, 20000), max=20 + slack(20, 20000))
        with d.only("ctl"):
            d.fz_next("r1")
        with d.only("tgt"):
            d.li("r2", value)
            d.raw_delay(100)
        d.cond("fall", rx, owner="tgt", **({"prof": d.profile("W", **prof)} if prof else {}))
        d.hold(rx, 0, owner="tgt", prof=bit)
        d.xfer(n, rx, owner="tgt", prof=bit, src="r2", dst="r3")
        d.hold(rx, 1, owner="tgt", prof=bit)
        with d.only("ctl"):
            d.fz_end("r4")
        d.halt()
        return d
    return build


def test_a_followed_xfer_wider_than_8_bits_gives_both_bytes():
    """isa §4.6: an XFER gives value[7:0], then value[15:8] if n > 8. A 9-bit frame from tgt:
    the COND's latency token, then 0xBC and 0x01."""
    u, et = armed_pair(uart_from_tgt())
    assert et.regs[3] == 0x1BC and len(u.tokens) == 3 and u.tokens[1:] == [0xBC, 0x01]


def test_a_followed_cond_gives_its_latency_token_even_when_early():
    """isa §4.6: a followed COND gives 0x40 + bucket(m), m = tb after - tb before; 0xFFFF only on
    a timeout. The edge comes before min, so the COND logs EARLY in the same cycle it completes,
    and still gives its own latency."""
    from emu.fuzz.unit import bucket
    u, et = armed_pair(uart_from_tgt(n=8, value=0x5A, prof=dict(nom=200, min=180, max=INF)),
                       peer_scale={"W": 0.5})  # tgt acts at about 100
    cond = next(r for r in et.records if r.op == "COND")
    assert "EARLY" in [e.kind for e in et.events] and cond.tb < 180
    assert u.tokens[0] == 0x40 + bucket(cond.tb) != 0x40 + bucket(0xFFFF)


def test_a_timed_out_followed_cond_gives_the_0xffff_token():
    from emu.fuzz.unit import bucket
    u, et = armed_pair(uart_from_tgt(n=8, value=0x5A, prof=dict(nom=40, min=0, max=60)), peer=False)
    assert "TIMEOUT" in [e.kind for e in et.events]
    assert u.tokens[0] == 0x40 + bucket(0xFFFF)


def narrow_duplex(armer):
    """A 4-bit duplex XFER under CS: ctl sends 0xB on MOSI, per 0xD on MISO, both from r3 (the
    armed party doesn't send from the unit, so every token is known)."""
    from test_fz import spi_base
    d, lane, half, wait = spi_base()
    with d.only("ctl"):
        d.li("r3", 0x5B)
        d.raw_delay(120)
    with d.only("per"):
        d.li("r3", 0x3D)
    with d.only(armer):
        d.fz_next("r1")
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(4, lane, owner="ctl", prof=half, src="r3", dst="r2")
    d.cond("stop", owner="ctl", prof=wait)
    with d.only(armer):
        d.fz_end("r4")
    d.halt()
    return d


@pytest.mark.parametrize("armer", ["ctl", "per"])
def test_a_narrow_duplex_xfer_gives_each_side_the_other_lines_bits(armer):
    """isa §4.6: on a duplex lane dst = (v2 << n) | v1, and each side's token is the line it
    followed: the controller gets MISO's 4 bits, the peripheral MOSI's (with its START and STOP
    tokens around them)."""
    u, et = armed_pair(lambda: narrow_duplex(armer), armer=armer)
    assert et.regs[2] == 0xD << 4 | 0xB
    if armer == "ctl":
        assert u.tokens == [0xD]
    else:
        assert len(u.tokens) == 3 and u.tokens[1] == 0xB


# ---------------------------------------------------------------- followed START/STOP (isa §4.2)
def select_desc(start, head=None, tail=None, abort=False):
    """SELECT on CS (confirm 1) and a's line u: [head], COND START (profile `start`, MEAS r1,
    with or without ABORT), [tail], HALT; abort_pc "top" is an unbounded followed START."""
    d = Description("sel", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    u = d.line("u", owner="a", idle=1)
    d.bus("select", cs)
    d.abort_to("top")
    if head:
        head(d, u)
    d.cond("start", owner="a", prof=d.profile("S", **start), meas="r1", abort=abort)
    if tail:
        tail(d, u)
    d.halt()
    d.label("top")
    d.cond("start", owner="a", prof=d.profile("T", nom=10, min=0, max=INF))
    d.halt()
    return compile_desc(d, check_timing=False, check_levels=False)


SMIN = 31  # instructions start on even cycles, so m is even: min - QUANT = 30 is one of them


@pytest.mark.parametrize("delay", range(SMIN - 8, SMIN + 4))
def test_a_followed_start_is_early_below_min_minus_quant(delay):
    """EARLY when min > 0 and m = start_e - tb < min - QUANT. The master acts at its start, after
    a RAW DELAY; the followers' tb is 0."""
    def head(d, u):
        with d.only("a"):
            d.raw_delay(delay)
    t = run_sys(select_desc(dict(nom=0, min=SMIN, max=INF), head=head))
    for i in (1, 2):
        (rec,) = [r for r in t.engines[i].records if r.op == "COND"][:1]
        m = rec.tb
        assert t.engines[i].regs[1] == m
        assert kinds(t, i) == (["EARLY"] if m < SMIN - QUANT else [])


def test_a_followed_start_before_tb_is_early_with_min_1():
    """m < 0 (the START came before the follower's tb) is below min - QUANT = 0 for min = 1. The
    master runs the HOLD before it at half length, so its START is early in the followers' time;
    MEAS clamps m to 0."""
    def head(d, u):
        d.hold(u, 1, owner="a", prof=d.profile("H", nom=40, min=0, max=INF))
    img = select_desc(dict(nom=0, min=1, max=INF), head=head)
    t = run_sys(img, images={0: img.with_profiles({"H": 0.5})})
    for i in (1, 2):
        early = [e for e in t.engines[i].events if e.kind == "EARLY"]
        assert len(early) == 1 and early[0].measured < 0 and t.engines[i].regs[1] == 0


def test_a_followed_start_re_anchors_the_chk_window():
    """isa §4.3: a COND re-anchors the window, a followed START too: a HOLD and a CHK XFER of
    zeros after a START at about 30, whose boundary-0 edge is one HOLD after the START."""
    def head(d, u):
        with d.only("a"):
            d.raw_delay(30)

    def tail(d, u):
        bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
        d.hold(u, 1, owner="a", prof=bit)
        d.xfer(4, u, owner="a", prof=bit, src="zero", dst="r2", check=True)
    t = run_sys(select_desc(dict(nom=0, min=0, max=INF), head=head, tail=tail))
    assert not any(et.events for et in t.engines)
    assert [len(et.records) for et in t.engines] == [3, 3, 3]


def test_a_followed_stop_with_abort_and_no_start_completes():
    """A followed STOP exits only when it sees a START; with ABORT and no START it completes on
    the STOP."""
    def tail(d, u):
        d.cond("stop", owner="a", prof=d.profile("W", nom=30, min=0, max=INF), abort=True)
    t = run_sys(select_desc(dict(nom=8, min=0, max=INF), tail=tail), roles=(Role.P0, Role.P1))
    assert "BUS" not in kinds(t, 1)
    assert [r.op for r in t.engines[1].records] == ["COND", "COND"] and t.engines[1].halted


@pytest.mark.parametrize("abort", [False, True])
def test_a_followed_start_timeout(abort):
    """No master: the bounded START times out when now - EDGE_LAT - bus_confirm - tb passes max +
    QUANT, and logs TIMEOUT(m) with that m. With ABORT a fatal abort to abort_pc; without, `to`
    := 1, MEAS 0xFFFF, tb := tb + nom and a re-anchor there (isa §4.2)."""
    def head(d, u):  # a followed HOLD first, so tb = 10 when the START starts
        d.hold(u, 1, owner="a", prof=d.profile("H", nom=10, min=0, max=INF))
    s = System([EngineSpec(select_desc(dict(nom=20, min=0, max=30), head=head, abort=abort), Role.P1)])
    t = s.run(2000)
    e = s.engines[0]
    ev = t.engines[0].events[0]
    assert ev.kind == "TIMEOUT" and ev.measured == 30 + QUANT + 1
    if abort:
        assert kinds(t, 0)[:2] == ["TIMEOUT", "ABORT"] and e.pc == e.image.abort_pc
    else:
        assert kinds(t, 0) == ["TIMEOUT"] and e.halted
        assert e.to and e.regs[1] == 0xFFFF and e.tb == 10 + 20 and e.anchor == 30


def test_clk_phase_measures_the_cycles_between_edges():
    """isa §4.3: CLK_PHASE(m), m the cycles between two edges the follower detects. The clock
    owner runs at half the period (H = 2), so every later phase is 2."""
    from test_engine_clocked import spi
    img = spi(check=True, lo=4, hi=6)
    t = run_sys(img, images={0: img.with_profiles({"H": 0.5})})
    ms = {e.measured for e in t.engines[1].events if e.kind == "CLK_PHASE"}
    assert ms == {2}


# ---------------------------------------------------------------- waits and lateness
@pytest.mark.parametrize("clocked", [False, True])
def test_an_owner_waiting_on_an_empty_tx_is_idle(clocked):
    """isa §4.3: an owned src = FIFO XFER waits while TX is empty, as POP does, so a run with no
    host data ends quiescent rather than at its tick budget."""
    d = Description("q", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    clk = d.line("c", owner="a", idle=0)
    if clocked:
        d.li("r0", 0)
        d.xfer(4, d.lane("L", ln, clk=clk), owner="a", prof=d.profile("H", nom=4, min=0, max=INF),
               src="fifo", dst="discard")
    else:
        d.xfer(4, ln, owner="a", prof=d.profile("BIT", nom=P, min=P - 2, max=P + 2), src="fifo", dst="discard")
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0,), max_ticks=20_000)
    assert t.quiescent and t.ticks < 20_000 and not t.engines[0].halted


def late_xfer(kind, head):
    d = Description("x", parties=("a", "b"))
    ln = d.line("l", kind, owner="a", idle=1)
    if head:
        d.li("r0", 0)
    d.xfer(2, ln, owner="a", prof=d.profile("BIT", nom=P, min=P - 2, max=P + 2), src="zero", dst="discard")
    d.halt()
    return run_sys(compile_desc(d, check_timing=False), roles=(Role.P0,))


@pytest.mark.parametrize("kind", ["pp", "od"])
def test_a_done_xfer_bit_0_may_be_issue_late(kind):
    """isa §4.1: an owned step's first action may be up to ISSUE late. Instruction 0 starts at
    now = 2 with tb = 0, so bit 0 is 2 late and logs nothing (as test_hold_at_pc0 for HOLD)."""
    assert kinds(late_xfer(kind, head=False), 0) == []


@pytest.mark.parametrize("kind", ["pp", "od"])
def test_a_done_xfer_bit_0_after_an_li_overruns_by_4(kind):
    """After an LI, bit 0 goes out at now = 4 with tb = 0: OVERRUN(4); bit 1 is on time."""
    t = late_xfer(kind, head=True)
    assert [(e.kind, e.measured) for e in t.engines[0].events] == [("OVERRUN", 4)]
