import random

import pytest

from emu.compiler import compile_desc
from emu.desc import Description
from emu.isa import EDGE_LAT, QUANT, Role
from emu.model.engine import Engine
from emu.model.system import Glitch

from simutil import kinds, run_sys

P = 16


def frame(n=8, msb=False, check=False, resync=False, dst="fifo", src="fifo", tail=None, ppm=20000,
          abort=False):
    """COND fall (IDLE) · HOLD 0 (start) · XFER n · HOLD 1 (stop) · [tail] · HALT."""
    d = Description("f", parties=("a", "b"), tolerance_ppm=ppm)
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    d.abort_to("end")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit, abort=True)
    d.xfer(n, ln, owner="a", prof=bit, src=src, dst=dst, msb_first=msb, check=check, resync=resync,
           abort=abort)
    d.hold(ln, 1, owner="a", prof=bit, abort=True)
    if tail:
        tail(d)
    d.label("end")
    d.halt()
    return d


def test_follower_tb_matches_owner_tb_equal_clocks():
    t = run_sys(compile_desc(frame()), tx={0: [0xA5]})
    owner, foll, mon = (t.engines[i].records for i in range(3))
    # COND, start-bit HOLD, XFER, stop-bit HOLD
    assert [r.mode for r in owner] == ["do"] * 4 and [r.mode for r in foll] == ["follow"] * 4
    assert [r.tb for r in owner] == [r.tb for r in foll] == [r.tb for r in mon]
    cond_o, cond_f = owner[0], foll[0]
    assert cond_f.cycle == cond_o.cycle + EDGE_LAT  # detection latency is compensated in tb
    assert all(not et.events for et in t.engines)


def window_desc():
    d = Description("w", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    win = d.profile("W", nom=20, min=16, max=24)
    d.abort_to("end")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.cond("rise", ln, owner="a", prof=win, abort=True)
    d.mark(7)
    d.label("end")
    d.halt()
    return compile_desc(d)


def test_cond_window_early_and_timeout():
    img = window_desc()
    early = run_sys(img, images={0: img.with_profiles({"W": 0.5})})
    assert "EARLY" in kinds(early, 1) and "EARLY" in kinds(early, 2)
    assert "MARK" in kinds(early, 1)  # EARLY is logged, not fatal
    late = run_sys(img, images={0: img.with_profiles({"W": 3.0})})
    for i in (1, 2):
        assert kinds(late, i)[:2] == ["TIMEOUT", "ABORT"]
        assert "MARK" not in kinds(late, i)  # jumped to the abort vector past the MARK
    assert kinds(late, 0) == ["MARK"]  # the owner itself is unaware


def test_hold_level_mismatch_logs_level_and_aborts():
    img = compile_desc(frame())
    # stop bit (slot 9: ticks 145..160) sampled at mid-slot; force it low around the sample
    t = run_sys(img, tx={0: [0x00]}, glitches=[Glitch(0, 146, 12, 0)])
    for i in (1, 2):
        assert "LEVEL" in kinds(t, i) and "ABORT" in kinds(t, i)


@pytest.mark.parametrize("n", [1, 8, 16])
@pytest.mark.parametrize("msb", [False, True])
@pytest.mark.parametrize("pattern", ["zeros", "ones", "random"])
def test_xfer_widths_and_order(n, msb, pattern):
    """n = 16 runs at 1%: at 2% T1 rejects it (last sample at A + 16P = 272, ceil(0.02*272)
    + QUANT + 1 = 8, not < P//2 = 8; test_analysis.test_t1_sixteen_bit_unclocked_frame).
    At 1% the stop bit is the last sample: ceil(0.01*288) + 2 = 5 < 8."""
    mask = (1 << n) - 1
    v = {"zeros": 0, "ones": mask, "random": random.Random(n * 3 + msb).getrandbits(n)}[pattern]
    t = run_sys(compile_desc(frame(n=n, msb=msb, ppm=10000 if n == 16 else 20000)), tx={0: [v]})
    assert t.engines[1].rx == [v] and t.engines[2].rx == [v]
    owner_xfer = [r for r in t.engines[0].records if r.op == "XFER"][0]
    assert owner_xfer.value == v
    assert all(not et.events for et in t.engines)


def test_xfer_wire_order_msb_vs_lsb():
    t_lsb = run_sys(compile_desc(frame(n=8, msb=False)), tx={0: [0x01]})
    t_msb = run_sys(compile_desc(frame(n=8, msb=True)), tx={0: [0x01]})
    # The owner's COND acts at now = 2 (instruction 0's start, isa §4.0), so slot s is on the wire
    # at ticks 3 + sP .. 2 + sP + P: slot 0 is the start bit, slots 1-8 the data, slot 9 the stop
    # bit. LSB-first 0x01 is high in slot 1, right after the start bit; MSB-first in slot 8.
    assert t_lsb.wire_edges[0][:2] == [3, 3 + P]
    assert t_msb.wire_edges[0][:2] == [3, 3 + 8 * P]


def test_xfer_check_flags_stretched_bit():
    img = compile_desc(frame(check=True))
    t = run_sys(img, tx={0: [0x55]}, images={0: img.with_profiles({"BIT": 1.5})})
    assert "EDGE" in kinds(t, 1) and "EDGE" in kinds(t, 2)


def test_xfer_resync_recovers_drift():
    """Follower clock 4% slow over 16 bits drifts ~10 cycles past an 8-cycle half bit."""
    def run(resync):
        img = compile_desc(frame(n=16, resync=resync), check_timing=False)
        return run_sys(img, roles=(Role.P0, Role.P1), tx={0: [0x5555]}, clocks={1: (24, 25)})
    assert run(True).engines[1].rx == [0x5555]
    assert run(False).engines[1].rx != [0x5555]


def test_xfer_fifo_stall_logs_overrun():
    t = run_sys(compile_desc(frame()), roles=(Role.P0, Role.P1), tx={0: [0x3C]}, push_at=200)
    assert "OVERRUN" in kinds(t, 0)


def test_xfer_dst_fifo_only_in_follow_mode():
    t = run_sys(compile_desc(frame()), tx={0: [0x42]})
    assert t.engines[0].rx == [] and t.engines[1].rx == [0x42] and t.engines[2].rx == [0x42]


def test_xfer_dst_reg_written_in_all_roles():
    t = run_sys(compile_desc(frame(dst="r1", tail=lambda d: d.push("r1"))), tx={0: [0x99]})
    assert [et.rx for et in t.engines] == [[0x99]] * 3


# ---------------------------------------------------------------- r2: branch on timeout
def late_desc(second_cond=False):
    """COND fall · HOLD 0 · bounded COND rise (no ABORT) · [COND fall IDLE] · only(b, mon): BR TO.

    MARK 1 when the rise came in time, MARK 2 when the follower timed out.
    """
    d = Description("to", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    win = d.profile("W", nom=20, min=16, max=24)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.cond("rise", ln, owner="a", prof=win)
    if second_cond:
        d.cond("fall", ln, owner="a")
    with d.only("b", "mon"):
        d.br("late", cond="to")
        d.mark(1)
        d.br("done")
        d.label("late")
        d.mark(2)
    d.label("done")
    d.halt()
    return compile_desc(d)


def marks(trace, i):
    return [e.measured for e in trace.engines[i].events if e.kind == "MARK"]


def test_br_to_taken_after_follow_timeout():
    img = late_desc()
    on_time = run_sys(img)
    assert [marks(on_time, i) for i in range(3)] == [[], [1], [1]]
    late = run_sys(img, images={0: img.with_profiles({"W": 3.0})})
    assert [marks(late, i) for i in range(3)] == [[], [2], [2]]
    for i in (1, 2):
        assert kinds(late, i) == ["TIMEOUT", "MARK"]  # no ABORT: the step completes, `to` is set


def test_to_flag_cleared_by_next_timed_step():
    img = late_desc(second_cond=True)
    t = run_sys(img, images={0: img.with_profiles({"W": 3.0})})
    for i in (1, 2):
        assert "TIMEOUT" in kinds(t, i)
        assert marks(t, i) == [1]  # the COND fall after the timeout cleared `to`


# ---------------------------------------------------------------- T1 against the model
@pytest.mark.parametrize("phase", range(4))
@pytest.mark.parametrize("value", [0xA5, 0x5A])
def test_shortest_accepted_slot_runs_clean_at_every_phase(phase, value):
    """At 0 ppm T1 accepts slots from P = 6 (test_analysis). Every engine runs at 1/4 of
    the tick rate, so a follower offset by 1-3 ticks sees each edge a fraction of a cycle
    later than the owner made it. Every such phase must still sample every bit."""
    d = Description("p6", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    d.abort_to("end")
    bit = d.profile("BIT", nom=6)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit, abort=True)
    d.xfer(8, ln, owner="a", prof=bit, src="fifo", dst="fifo")
    d.hold(ln, 1, owner="a", prof=bit, abort=True)
    d.label("end")
    d.halt()
    t = run_sys(compile_desc(d), tx={0: [value]}, clocks={i: (1, 4) for i in range(3)},
                phases={1: phase, 2: phase})
    assert t.engines[1].rx == [value] and t.engines[2].rx == [value]
    assert all(not et.events for et in t.engines)


def test_cond_with_min_zero_runs_clean():
    """Model side of test_analysis.test_t2_accepts_cond_with_min_zero: no EARLY check
    when min = 0, and the edge arrives well before max."""
    d = Description("m0", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.abort_to("end")
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    soon = d.profile("SOON", nom=1, min=0, max=11)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.cond("rise", ln, owner="a", prof=soon, abort=True)
    d.label("end")
    d.halt()
    t = run_sys(compile_desc(d))
    assert all(not et.events for et in t.engines)


@pytest.mark.parametrize("phase", range(0, 8, 3))
@pytest.mark.parametrize("ratios", [((50, 400), (49, 400)), ((49, 400), (50, 400))], ids=["slow", "fast"])
def test_t1_boundary_at_2pct_runs_clean(ratios, phase):
    """At 2% T1 accepts this frame from P = 10 (P = 9 fails: ceil(0.02*90) + 2 = 4, not < 4).
    The follower runs 2% slower or faster than the owner, at sub-cycle phase offsets."""
    d = Description("p10", parties=("a", "b"), tolerance_ppm=20000)
    ln = d.line("l", owner="a", idle=1)
    d.abort_to("end")
    bit = d.profile("BIT", nom=10)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit, abort=True)
    d.xfer(8, ln, owner="a", prof=bit, src="fifo", dst="fifo")
    d.hold(ln, 1, owner="a", prof=bit, abort=True)
    d.label("end")
    d.halt()
    img = compile_desc(d)
    owner, foll = ratios
    for v in (0xA5, 0x5A, 0x01):
        t = run_sys(img, roles=(Role.P0, Role.P1), tx={0: [v]}, clocks={0: owner, 1: foll},
                    phases={1: phase})
        assert t.engines[1].rx == [v] and not t.engines[1].violations


def test_follower_samples_at_slot_midpoints():
    """isa §4.2-4.3: a follower samples slot s at its tb + s*P + P//2 + EDGE_LAT. Equal clocks:
    the COND anchors at the owner's action cycle 2, so the 10 slots (start, 8 data, stop) are
    sampled at 13 + 16s."""
    t = run_sys(compile_desc(frame()), tx={0: [0xA5]})
    for i in (1, 2):
        assert [s.cycle for s in t.engines[i].samples] == [2 + P // 2 + EDGE_LAT + s * P for s in range(10)]


@pytest.mark.parametrize("scale", [14 / 16, 18 / 16], ids=["at-min", "at-max"])
def test_xfer_check_accepts_edges_at_the_window_limits(scale):
    """BIT is (14, 16, 18). An owner whose bits last exactly min or max puts boundary k at
    14 + 14k or 18 + 18k from the anchor, inside the window [min - QUANT, max + QUANT].
    RESYNC follows the edges, so the data is right too."""
    img = compile_desc(frame(check=True, resync=True))
    t = run_sys(img, tx={0: [0x55]}, images={0: img.with_profiles({"BIT": scale})})
    for i in (1, 2):
        assert t.engines[i].rx == [0x55] and not t.engines[i].violations


# ---------------------------------------------------------------- audit follow-ups
def test_chk_xfer_at_anchor_raises_the_false_alarm_t2_prevents():
    """Decision D2 (isa-v0 §6). Right after an anchor the owner drives bit 0 in the issue slot,
    one owner cycle after the COND edge. A follower 2% faster can count that gap as 2 of its
    cycles: here the anchor falls just before one of the owner's 5-tick cycles (49/200 of the
    tick rate) and the follower (50/200) samples every 4 ticks. The data is right, yet EDGE
    fires against the boundary-0 window [-1, 1]. So T2 rejects the step."""
    d = Description("d2", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("BIT", nom=P, min=P - 2, max=P + 2)
    with d.only("a"):
        d.raw_delay(11)  # moves the anchor onto a 5-tick owner cycle
    d.cond("fall", ln, owner="a")
    d.xfer(8, ln, owner="a", prof=bit, src="fifo", dst="fifo", check=True)
    d.hold(ln, 1, owner="a", prof=bit)
    d.halt()
    from emu.compiler import CompileError
    with pytest.raises(CompileError, match="T2-window"):
        compile_desc(d)
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1), tx={0: [0x01]},
                clocks={0: (49, 200), 1: (50, 200)}, phases={1: 1})
    assert t.engines[1].rx == [0x01]
    assert [(e.kind, e.measured, e.lo, e.hi) for e in t.engines[1].events] == [("EDGE", 2, -1, 1)]


def test_idle_cond_accepts_an_early_start_edge_from_a_fast_transmitter():
    """Why COND checks EARLY only when min > 0 (isa §4.2). Back-to-back frames from a
    transmitter 2% fast put each start edge about 4 cycles (2% of 10P) before the receiver's
    expected end of the stop bit. With min = 0 that is legal; checking m >= -QUANT would flag every
    frame. P = 20: v1's T5-watch needs ceil(P/2) - ceil(P/5) >= 6 at 2%, so P >= 19 (isa §6)."""
    from descs import uart_like
    data = list(range(1, 9))
    t = run_sys(compile_desc(uart_like(20)), tx={0: data}, clocks={1: (49, 50), 2: (49, 50)})
    for i in (1, 2):
        recs = t.engines[i].records
        early = [recs[j].tb - recs[j - 1].tb for j in range(1, len(recs)) if recs[j].op == "COND"]
        assert min(early) < -QUANT
        assert t.engines[i].rx == data and not t.engines[i].violations


def test_xfer_check_with_abort_aborts_on_an_out_of_window_edge():
    img = compile_desc(frame(check=True, abort=True))
    t = run_sys(img, tx={0: [0x55]}, images={0: img.with_profiles({"BIT": 1.5})})
    for i in (1, 2):
        assert kinds(t, i) == ["EDGE", "ABORT"]  # jumped to the abort vector: no stop-bit LEVEL
        assert t.engines[i].rx == []


def _owner_holds(first_nom, second_nom, pre_li=False):
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    if pre_li:
        d.li("r0", 1)
    d.hold(ln, 0, owner="a", prof=d.profile("A", nom=first_nom))
    d.hold(ln, 1, owner="a", prof=d.profile("B", nom=second_nom))
    d.halt()
    e = Engine(compile_desc(d, check_timing=False), Role.P0)
    for _ in range(40):
        if e.halted:
            break
        e.cycle([1] * 8)
    return e


def test_hold_at_pc0_is_two_late_without_overrun():
    """isa §4.1: an action planned at offset 0 may happen up to ISSUE = 2 cycles late. HOLD 0 is
    planned at tb = 0 and starts at 2: no OVERRUN, tb := 3. Its successor starts at the first own
    cycle >= max(2 + 1, 2 + 2) = 4, one cycle after its plan: still no OVERRUN."""
    e = _owner_holds(3, 3)
    assert [ev.kind for ev in e.events] == []
    assert e.starts == [(0, 2), (1, 4), (2, 6)]
    assert [r.tb for r in e.records] == [3, 6]


def test_hold_after_li_overruns_by_4():
    """LI at 2 puts HOLD's start at 4, 4 cycles after its plan at tb = 0: past ISSUE, OVERRUN(4).
    An overrun doesn't move the timeline (isa §4.1), so tb := 0 + 3 and the second HOLD, starting at
    6, is 3 late: OVERRUN(3)."""
    e = _owner_holds(3, 3, pre_li=True)
    assert [(ev.kind, ev.measured) for ev in e.events] == [("OVERRUN", 4), ("OVERRUN", 3)]


def test_follower_cond_odd_detect_successor_on_even():
    """A follower COND completes in its detect cycle, which may be odd. A wire that falls at tick 5
    is seen at 5 + SYNC_LAT = 7, so the COND completes at 7 with tb = 7 - EDGE_LAT = 4, and HALT
    starts at the first own cycle >= max(7 + 1, 2 + 2) = 8 (isa §4.0)."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.halt()
    t = run_sys(compile_desc(d), roles=(Role.P1,), glitches=[Glitch(0, 5, 100, 0)])
    (rec,) = t.engines[0].records
    assert (rec.cycle, rec.tb) == (7, 7 - EDGE_LAT)
    assert t.engines[0].starts == [(0, 2), (1, 8)]
