"""Tests for the compiler's checks (emu/analysis.py) that mutation testing found loose
(verification.md §1): each pins a rule's boundary, or the numbers its message gives. The rule rows
are isa §5-6."""
import pytest

from emu.analysis import BIG, drift
from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF


def diags(desc: Description) -> list[tuple[str, str]]:
    """(rule, message) for each diagnostic, timing checks on, level checks (R2-edge) off."""
    try:
        compile_desc(desc, check_levels=False)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def rules(desc: Description) -> set[str]:
    return {r for r, _ in diags(desc)}


def msgs(desc: Description, rule: str) -> list[str]:
    return [m for r, m in diags(desc) if r == rule]


def two_party(ppm: int = 20000):
    d = Description("t", parties=("a", "b"), tolerance_ppm=ppm)
    return d, d.line("l", owner="a", idle=1)


# ---------------------------------------------------------------- T5-startup
@pytest.mark.parametrize("kind,edge,flagged", [("pp", "fall", True), ("od", "fall", False), ("od", "rise", True)])
def test_t5_startup_flags_a_fall_at_pc_0_only_on_an_idle_0_pp_line(kind, edge, flagged):
    """isa §6 T5-startup: a PP idle-0 line falls back to idle at now = 3, which a COND FALL at
    pc 0 would take as the owner's edge. An OD line rests released, so it never drives that fall,
    but its start-up 1 is still a rise."""
    d = Description("s", parties=("a", "b"))
    ln = d.line("l", kind, owner="a", idle=0)
    d.cond(edge, ln, owner="a")
    d.halt()
    assert rules(d) == ({"T5-startup"} if flagged else set())


# ---------------------------------------------------------------- code reached only from abort_pc
def recovery(P: int, main: bool) -> Description:
    """a's HOLD with ABORT; abort_pc runs an unbounded COND, then an XFER with slots of P. The
    recovery code is in the main loop, or after a HALT, where only an abort reaches it."""
    d, ln = two_party()
    d.abort_to("rec")
    if main:
        d.label("rec")
    d.cond("fall", ln, owner="a", prof=d.profile("G", nom=10, min=0, max=INF))
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=16, min=14, max=18), abort=True)
    if not main:
        d.halt()
        d.label("rec")
        d.cond("fall", ln, owner="a", prof=d.profile("G2", nom=10, min=0, max=INF))
    d.xfer(8, ln, owner="a", prof=d.profile("F", nom=P, min=P - 1, max=P + 1))
    if main:
        d.br("rec")
    else:
        d.halt()
    return d


AT_ANCHOR = ("edge window [0,0] at boundary 0 does not contain 0±1; a CHK XFER that starts right at "
             "an anchor has no window for its first edge")


def test_the_timeline_starts_at_anchor_0_at_reset():
    """At reset the anchor distance and the window are 0: a CHK XFER at pc 0 is at the anchor."""
    d, ln = two_party()
    d.xfer(8, ln, owner="a", prof=d.profile("X", nom=16, min=14, max=18), check=True)
    d.halt()
    (m,) = msgs(d, "T2-window")
    assert m.startswith(f"pc 0: {AT_ANCHOR}")


def test_abort_pc_starts_at_anchor_0_when_a_step_can_abort():
    """With an ABORT step, a CHK XFER first at abort_pc (after a HALT) is checked at the anchor
    (T5-abort rejects it as the first step there too)."""
    d, ln = two_party()
    d.abort_to("rec")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("H", nom=16, min=14, max=18), abort=True)
    d.halt()
    d.label("rec")
    d.xfer(8, ln, owner="a", prof=d.profile("X", nom=16, min=14, max=18), check=True)
    d.halt()
    (m,) = msgs(d, "T2-window")
    assert m.startswith(f"pc 3: {AT_ANCHOR}") and "T5-abort" in rules(d)


def test_abort_pc_is_no_anchor_when_no_step_can_abort():
    """With no ABORT step nothing jumps to abort_pc (a bus exit needs ABORT too), so a CHK XFER
    there after its start HOLD is checked only from the HOLD."""
    d, ln = two_party()
    d.abort_to("x")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("H", nom=16, min=14, max=18))
    d.label("x")
    d.xfer(8, ln, owner="a", prof=d.profile("X", nom=16, min=14, max=18), check=True)
    d.halt()
    assert diags(d) == []


# ---------------------------------------------------------------- T1-unanchored: the caps
@pytest.mark.parametrize("n,bad", [(8, False), (9, True)])
def test_t1_unanchored_past_an_anchor_distance_of_2_to_the_22(n, bad):
    """A_CAP = 2^22, the engine's 24-bit clamp (isa §4.1): n XFERs of 16 bits at 32768 = 2^19
    cycles each, with no re-anchor, reach 8·2^19 = 2^22 (accepted) or 9·2^19 (rejected). The
    windows' lower edge (min 1) stays far below: the cap is on the distance."""
    d, ln = two_party(ppm=0)
    x = d.profile("X", nom=32768, min=1, max=32768)
    d.cond("fall", ln, owner="a")
    for _ in range(n):
        d.xfer(16, ln, owner="a", prof=x)
    d.halt()
    assert ("T1-unanchored" in rules(d)) == bad


@pytest.mark.parametrize("k,bad", [(6, False), (7, True)])
def test_t1_unanchored_past_64_anchor_states_at_one_pc(k, bad):
    """SET_CAP = 64: k branches, each skipping a HOLD of its own length, give 2^k distinct
    (distance, window) states at the last HOLD. (Each HOLD after a BR also overruns, T5-overrun.)"""
    d, ln = two_party(ppm=0)
    hs = [d.profile(f"H{i}", nom=16 << i, min=(16 << i) - 1, max=(16 << i) + 1) for i in range(k)]
    d.li("r0", 1)
    d.cond("fall", ln, owner="a")
    for i in range(k):
        d.br(f"s{i}", cond="nz", reg="r0")
        d.hold(ln, 0, owner="a", prof=hs[i])
        d.label(f"s{i}")
    d.hold(ln, 1, owner="a", prof=hs[0])
    d.halt()
    assert ("T1-unanchored" in rules(d)) == bad


def test_abort_pc_reached_from_pc_0_keeps_only_the_states_that_reach_it():
    """The seed is only for code pc 0 never reaches: here 64 states flow into abort_pc (six
    optional HOLDs after an ABORT HOLD), whose COND re-anchors. A 65th, seeded state would trip
    the cap where nothing reads it."""
    d, ln = two_party(ppm=0)
    hs = [d.profile(f"H{i}", nom=16 << i, min=(16 << i) - 1, max=(16 << i) + 1) for i in range(6)]
    d.abort_to("top")
    d.li("r0", 1)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=hs[0], abort=True)
    for i in range(6):
        d.br(f"s{i}", cond="nz", reg="r0")
        d.hold(ln, 0, owner="a", prof=hs[i])
        d.label(f"s{i}")
    d.label("top")
    d.cond("fall", ln, owner="a", prof=d.profile("G", nom=10, min=0, max=INF))
    d.halt()
    assert "T1-unanchored" not in rules(d)


@pytest.mark.parametrize("live", [True, False])
def test_code_after_abort_pc_is_timed_only_when_a_step_can_abort(live):
    """With no ABORT step nothing reaches the code after the HALT (S-end and R2 skip it too), so
    its second COND, which b reaches late, is no T5-watch; with one, it is. (The path into
    abort_pc itself isn't timed: isa §6, open.)"""
    d, ln = two_party()
    d.abort_to("rec")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("H", nom=16, min=14, max=18), abort=live)
    d.halt()
    d.label("rec")
    d.cond("fall", ln, owner="a", prof=d.profile("G", nom=10, min=0, max=INF))
    with d.only("b"):
        for _ in range(3):
            d.nop()
    d.cond("rise", ln, owner="a", prof=d.profile("G2", nom=0, min=0, max=INF))
    d.halt()
    assert bool(msgs(d, "T5-watch")) == live


@pytest.mark.parametrize("main", [True, False])
def test_t1_checks_the_steps_only_an_abort_reaches(main):
    """isa §6: every timed step gets T1 and T2, from its anchor. After an abort the first timed
    step at abort_pc is an unbounded COND (T5-abort), which re-anchors, so the steps after it are
    checked from it wherever the code is. Slots of 4 cycles are too short (P//2 = 2 is not above
    drift + QUANT + ISSUE); slots of 16 pass."""
    pc = 2 if main else 4
    assert [m.split(":")[0] for m in msgs(recovery(4, main), "T1-sample")] == [f"pc {pc}"]
    assert diags(recovery(16, main)) == []


# ---------------------------------------------------------------- T4-profile
@pytest.mark.parametrize("nom,bad", [(0, True), (1, False)])
def test_t4_needs_nom_at_least_1_for_a_hold(nom, bad):
    d, ln = two_party()
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("H", nom=nom, min=0, max=INF))
    d.halt()
    assert ("T4-profile" in rules(d)) == bad


def clocked(H: int, ppm: int = 20000, mn: int = 0, mx: int = INF, chk: bool = False,
            cs_hold: int = 0, late: int = 0, up: int = 0) -> Description:
    """a clocks a lane whose data it owns too, half period H; b follows the clock. Between a's COND
    FALL on cs and the XFER: a HOLDs cs low for `cs_hold` (± 2), and b and MON run `late` NOPs.
    After it a HOLDs cs high for `up`."""
    d = Description("c", parties=("a", "b"), tolerance_ppm=ppm)
    clk = d.line("clk", owner="a", idle=0)
    dat = d.line("dat", owner="a", idle=0)
    cs = d.line("cs", owner="a", idle=1)
    lane = d.lane("L", data=dat, clk=clk)
    d.li("r0", 0x5A)
    d.cond("fall", cs, owner="a", prof=d.profile("SU", nom=40, min=0, max=INF))
    if cs_hold:
        d.hold(cs, 0, owner="a", prof=d.profile("CS", nom=cs_hold, min=cs_hold - 2, max=cs_hold + 2))
    if late:
        with d.only("b", "mon"):
            for _ in range(late):
                d.nop()
    d.xfer(8, lane, owner="a", prof=d.profile("HALF", nom=H, min=mn, max=mx), src="r0", dst="r1",
           check=chk)
    if up:
        d.hold(cs, 1, owner="a", prof=d.profile("UP", nom=up, min=0, max=INF))
    d.halt()
    return d


@pytest.mark.parametrize("H,bad", [(1, True), (2, False)])
def test_t4_needs_a_half_period_of_at_least_2(H, bad):
    assert ("T4-profile" in rules(clocked(H))) == bad


# ---------------------------------------------------------------- T2-window
def chk_frame(xfer: dict, hold: dict | None = None) -> Description:
    """A start HOLD (16, 14, 18 unless given), then a CHK XFER of 8 bits: the XFER's windows
    accumulate onto the HOLD's (isa §4.3)."""
    d, ln = two_party()
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("H", **(hold or dict(nom=16, min=14, max=18))))
    d.xfer(8, ln, owner="a", prof=d.profile("X", **xfer), check=True)
    d.halt()
    return d


@pytest.mark.parametrize("mn,mx,window", [
    (15, 18, None), (16, 18, "[62,72]"),    # boundary 3: 14 + 3·16 = 62 > 64 − 3
    (14, 17, None), (14, 16, "[56,66]"),    # boundary 3: 18 + 3·16 = 66 < 64 + 3
    (14, INF, None),                        # no upper bound
])
def test_t2_accumulates_each_bit_s_min_and_max(mn, mx, window):
    """isa §6 T2: boundary j of a CHK XFER after the HOLD is checked against [lo + j·min,
    hi + j·max] = [14 + j·min, 18 + j·max], which must hold nominal = 16 + 16j ± (drift + QUANT).
    At j = 3, nominal = 64 and the margin is ceil(0.02·64) + 1 = 3."""
    got = msgs(chk_frame(dict(nom=16, min=mn, max=mx)), "T2-window")
    if window is None:
        assert got == []
    else:
        assert got == [f"pc 2: edge window {window} at boundary 3 does not contain 64±3"]


def test_t2_names_the_anchor_hint_only_for_boundary_0_at_the_anchor():
    """A tight HOLD fails the XFER's boundary 0 at anchor distance 16, not 0: no hint to drop
    check or add a start HOLD (that is for an XFER right at an anchor, D2)."""
    got = msgs(chk_frame(dict(nom=16, min=14, max=18), hold=dict(nom=16, min=16, max=18)), "T2-window")
    assert got == ["pc 2: edge window [16,18] at boundary 0 does not contain 16±2"]


@pytest.mark.parametrize("mn,mx,bad", [(8, INF, False), (9, INF, True), (0, 12, False), (0, 11, True)])
def test_t2_clock_phase_window_holds_h_plus_minus_drift_and_quant(mn, mx, bad):
    """isa §6 T2 for a clocked CHK XFER: [min, max] must hold H ± (ceil(d·H) + QUANT) = 10 ± 2."""
    assert ("T2-window" in rules(clocked(10, mn=mn, mx=mx, chk=True))) == bad


# ---------------------------------------------------------------- T3-latency
@pytest.mark.parametrize("H,bad", [(2, True), (3, False)])
def test_t3_a_clock_follower_needs_h_minus_drift_at_least_2(H, bad):
    """isa §6 T3: H − ceil(0.02·H) ≥ 2, so H = 2 fails (2 − 1) and H = 3 passes (3 − 1)."""
    assert ("T3-latency" in rules(clocked(H))) == bad


# ---------------------------------------------------------------- T5-timeout
def bounded_cond_then(next_step) -> Description:
    d, ln = two_party()
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=16, min=14, max=18))
    d.cond("rise", ln, owner="a", prof=d.profile("W", nom=10, min=8, max=100))
    next_step(d, ln)
    d.halt()
    return d


def test_t5_timeout_says_how_late_a_timed_out_follower_finishes():
    """max − nom + QUANT + EDGE_LAT + 1 = 100 − 10 + 1 + 3 + 1 = 95 cycles late."""
    d = bounded_cond_then(lambda d, ln: d.hold(ln, 1, owner="a", prof=d.profile("P2", nom=16, min=14, max=18)))
    (m,) = msgs(d, "T5-timeout")
    assert "finishes up to 95 cycles late" in m


def test_t5_timeout_rejects_a_bounded_cond_next():
    """Only an unbounded COND may follow (isa §6): a bounded one would time out too."""
    d = bounded_cond_then(lambda d, ln: d.cond("fall", ln, owner="a", prof=d.profile("W2", nom=10, min=0, max=100)))
    assert rules(d) == {"T5-timeout"}


# ---------------------------------------------------------------- T5-watch: a bus edge after another
@pytest.mark.parametrize("nom,bad", [(11, True), (12, False)])
def test_t5_watch_a_stop_right_after_a_start_clears_its_confirm(nom, bad):
    """isa §6 T5-watch: a COND STOP right after a COND START needs nom − ceil(d·nom) − QUANT ≥
    bus_confirm = 10: 12 − 1 − 1 = 10 passes, 11 − 1 − 1 = 9 fails."""
    d = Description("b", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    d.bus("select", cs, confirm=10)
    d.cond("start", owner="a", prof=d.profile("S", nom=20, min=0, max=INF))
    d.cond("stop", owner="a", prof=d.profile("T", nom=nom, min=0, max=INF))
    d.halt()
    assert any("replaces that one's candidate" in m for m in msgs(d, "T5-watch")) == bad


# ---------------------------------------------------------------- T5-abort: fz ops on the way
def test_t5_abort_compares_the_slowest_path_with_and_without_fz_ops():
    """With no FZ op, the paths from abort_pc cost the same either way, even when a branch makes
    the fastest (LI, BR: 4) and the slowest (LI, BR, NOP: 6) differ."""
    d, ln = two_party()
    d.abort_to("top")
    d.label("top")
    d.li("r0", 1)
    d.br("skip", cond="nz", reg="r0")
    d.nop()
    d.label("skip")
    d.cond("fall", ln, owner="a", prof=d.profile("G", nom=10, min=0, max=INF))
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=16, min=14, max=18), abort=True)
    d.br("top")
    assert diags(d) == []


# ---------------------------------------------------------------- the start times the pair checks use
def late_follower(nops: int, after_step: bool) -> Description:
    """b runs `nops` NOPs in a role block, then a's unbounded COND with nom 0: from reset
    (instruction 0 starts at now = 2), or after a's COND FALL."""
    d, ln = two_party()
    if after_step:
        d.cond("fall", ln, owner="a")
    with d.only("b"):
        for _ in range(nops):
            d.nop()
    d.cond("rise" if after_step else "fall", ln, owner="a", prof=d.profile("W", nom=0, min=0, max=INF))
    d.halt()
    return d


def test_pair_checks_start_both_parties_at_2_after_reset():
    """From reset both start at now = 2, and the ROLE_IF costs U = 2: a's COND starts at 4, so its
    event is at max(nom 0, 4) = 4, visible at 4 + EDGE_LAT = 7. b starts at 2 + 2 + 3·2 = 10. The
    anchor distance at reset is 0, so the drift is ceil(d·0) = 0."""
    assert msgs(late_follower(3, after_step=False), "T5-watch") == [
        "pc 4: P1 starts watching at 10 but the event is visible at 7 (drift 0)"]


def test_pair_checks_start_after_a_step_at_its_completion_plus_1_or_issue():
    """After a's COND FALL (a completes at 0, b at EDGE_LAT = 3): a's next step starts at the
    earliest 0 + 1 + 2 (the ROLE_IF), so its event is visible at 3 + 3 = 6; b's at the latest
    3 + ISSUE 2 + 2 + 3·2 = 13."""
    assert msgs(late_follower(3, after_step=True), "T5-watch") == [
        "pc 5: P1 starts watching at 13 but the event is visible at 6 (drift 0)"]


@pytest.mark.parametrize("nom,bad", [(0, False), (1, True), (2, False)])
def test_t5_overrun_at_pc_0_names_the_owner_s_start_at_2(nom, bad):
    """isa §6 T5-overrun: a bounded COND's owner starts by its planned action at nom, or by ISSUE
    when nom = 0. At pc 0 it starts at 2: nom 2 and nom 0 pass, nom 1 fails."""
    d, ln = two_party()
    d.cond("fall", ln, owner="a", prof=d.profile("W", nom=nom, min=0, max=40))
    d.halt()
    assert msgs(d, "T5-overrun") == (["pc 0: owner starts at 2, after the planned action at 1"] if bad else [])


@pytest.mark.parametrize("mn,mx,bad", [(8, 12, False), (9, 12, True), (8, 11, True)])
def test_t2_a_bounded_cond_s_window_holds_its_event_plus_minus_drift_and_quant(mn, mx, bad):
    """isa §6 T2 for a COND: [min, max] holds [event − drift − QUANT, latest action + drift +
    QUANT]. At pc 0 with nom 10 both are at max(10, 2) = 10, and the drift is ceil(0.02·10) = 1,
    so the window must hold [8, 12]."""
    d, ln = two_party()
    d.cond("fall", ln, owner="a", prof=d.profile("W", nom=10, min=mn, max=mx))
    d.halt()
    assert msgs(d, "T2-window") == ([f"pc 0: window [{mn},{mx}] does not contain [8,12]"] if bad else [])


# ---------------------------------------------------------------- the pair checks: clocked XFER
@pytest.mark.parametrize("H,bad", [(2, True), (3, False)])
def test_t5_watch_a_clock_follower_starts_by_the_first_edge(H, bad):
    """After a's COND FALL b starts at the latest at EDGE_LAT + ISSUE = 5. With drift ceil(0.02·H)
    = 1 that must not pass the first edge, visible at H + EDGE_LAT: H = 3 (6) passes, H = 2 (5)
    fails."""
    want = ["pc 2: P1 starts at 5, after the first clock edge is visible at 5"]
    assert msgs(clocked(H), "T5-watch") == (want if bad else [])


@pytest.mark.parametrize("mx,bad", [(10, True), (11, False)])
def test_t3_a_bounded_clock_follower_times_the_first_edge_from_its_own_start(mx, bad):
    """isa §4.1: a bounded clock follower counts max from its own start. a starts at the latest at
    0 + ISSUE = 2 and b at the earliest at EDGE_LAT + 1 = 4. a's first edge (cpha 0) is at 2 + H =
    12, visible at 15, and the drift is ceil(0.02·10) = 1: 15 + 1 − 4 = 12 must not pass
    max + QUANT."""
    want = ["pc 2: P1 may start at 4 and time out: the first clock edge may be visible only at 15 "
            "(drift 1), past max 10 + QUANT after it"]
    assert msgs(clocked(10, mx=mx), "T3-latency") == (want if bad else [])


@pytest.mark.parametrize("mx,bad", [(15, True), (16, False)])
def test_t3_a_clock_owner_that_starts_early_re_bases_to_the_step_start(mx, bad):
    """After its CS HOLD (16) a starts at the latest at 2 − 16 + ISSUE = −12, before the step: it
    re-bases to 0 (cpha 0), so its first edge is at 0 + H = 10, visible at 13. b followed the HOLD
    to its sample at 8 + EDGE_LAT and starts at the earliest at −16 + 11 + 1 = −4, so
    13 − (−4) = 17 must not pass max + QUANT (0 ppm)."""
    want = ["pc 3: P1 may start at -4 and time out: the first clock edge may be visible only at 13 "
            "(drift 0), past max 15 + QUANT after it"]
    assert msgs(clocked(10, ppm=0, mx=mx, cs_hold=16), "T3-latency") == (want if bad else [])


@pytest.mark.parametrize("H,bad", [(50, False), (51, True)])
def test_t3_each_clock_phase_plus_drift_fits_a_bounded_follower_s_max(H, bad):
    """isa §4.1: after the first edge a bounded clock follower counts max from each edge, so
    H + ceil(0.02·H) ≤ max + QUANT, here with max = H: 50 + 1 passes, 51 + 2 fails. (b and MON
    start late, after a NOP, so the first edge's timeout from their start passes.)"""
    want = ["pc 4: a clock phase of 51 plus drift outlasts max 51 + QUANT: a bounded clock follower "
            "would time out"]
    assert msgs(clocked(H, mx=H, late=1), "T3-latency") == (want if bad else [])


# ---------------------------------------------------------------- the pair checks: HOLD
@pytest.mark.parametrize("n,bad", [(4, True), (6, False)])
def test_t5_watch_a_hold_s_follower_starts_by_its_sample(n, bad):
    """isa §6 T5-watch for a HOLD (or an unclocked XFER): the follower's latest start plus drift
    must not pass its first sample at nom//2 + EDGE_LAT. After a clocked XFER (an anchor) b starts
    at the latest at EDGE_LAT + ISSUE = 5, and the drift is ceil(0.02·n) = 1: n = 4 (sample 5)
    fails, n = 6 (sample 6) passes."""
    want = ["pc 3: P1 starts at 5, after its first sample at 5"]
    assert msgs(clocked(10, up=n), "T5-watch") == (want if bad else [])


# ---------------------------------------------------------------- drift, and the control flow
@pytest.mark.parametrize("ppm,x,want", [(20000, 0, 0), (20000, 50, 1), (20000, 51, 2),
                                        (1, 10**6, 1), (1, 10**6 + 1, 2)])
def test_drift_is_ppm_times_x_over_a_million_rounded_up(ppm, x, want):
    """isa §6: drift(x) = ceil(ppm·x / 10^6)."""
    assert drift(ppm, x) == want


def test_a_jpin_jumps_to_its_target_at_an_odd_pc():
    """b's block: NOP, JPIN to pc 5 (odd), BR to the block's exit, then 3 NOPs at pcs 5-7. b's
    slowest path, through the jump, costs 2 (ROLE_IF) + 2 + 2 + 3·2 = 12, so it starts a's COND
    RISE at the latest at EDGE_LAT + ISSUE + 12 = 17."""
    d, ln = two_party()
    d.cond("fall", ln, owner="a")
    with d.only("b"):
        d.nop()
        d.raw_jpin(ln, 0, "late")
        d.br("go")
        d.label("late")
        for _ in range(3):
            d.nop()
    d.label("go")
    d.cond("rise", ln, owner="a", prof=d.profile("W", nom=0, min=0, max=INF))
    d.halt()
    assert msgs(d, "T5-watch") == ["pc 8: P1 starts watching at 17 but the event is visible at 6 (drift 0)"]


def test_the_others_skip_a_role_block_that_halts():
    """The roles of a block that HALTs stop there, and the others skip it, so the code after it is
    still walked: by R2 (r0 from a POP can differ between roles), and for the anchor states (a CHK
    XFER at reset's anchor)."""
    d, ln = two_party()
    d.pop("r0")
    with d.only("b"):
        d.halt()
    d.br("x", cond="nz", reg="r0")
    d.label("x")
    d.halt()
    assert rules(d) == {"R2-branch"}
    d, ln = two_party()
    with d.only("b"):
        d.halt()
    d.xfer(8, ln, owner="a", prof=d.profile("X", nom=16, min=14, max=18), check=True)
    d.halt()
    (m,) = msgs(d, "T2-window")
    assert m.startswith(f"pc 2: {AT_ANCHOR}")


# ---------------------------------------------------------------- gaps: when the walk gives up
def diamonds(k: int) -> Description:
    """k optional NOPs (a BR over each) between a's COND FALL and its unbounded COND RISE (nom
    100): 2^k paths."""
    d, ln = two_party()
    d.li("r0", 1)
    d.cond("fall", ln, owner="a")
    for i in range(k):
        d.br(f"s{i}", cond="nz", reg="r0")
        d.nop()
        d.label(f"s{i}")
    d.cond("rise", ln, owner="a", prof=d.profile("W", nom=100, min=0, max=INF))
    d.halt()
    return d


UNBOUNDED_B = f"P1 starts watching at {BIG + 5} but the event is visible at 103 (drift 2)"


@pytest.mark.parametrize("k,bad", [(12, False), (13, True)])
def test_gaps_follow_at_most_4096_paths_then_count_the_gap_unbounded(k, bad):
    """PATH_CAP = 4096: 12 diamonds make 4096 paths, all followed (b starts by 3 + 2 + 12·4 = 53);
    13 make 8192, and the walk gives up: b may start at any time (BIG)."""
    assert msgs(diamonds(k), "T5-watch") == ([f"pc {2 + 2 * k}: {UNBOUNDED_B}"] if bad else [])


def test_an_untimed_loop_makes_the_gap_unbounded():
    """A loop with no timed step (NOP, BR Z back) can run any number of times: b may reach the
    COND after it at any time."""
    d, ln = two_party()
    d.li("r0", 1)
    d.cond("fall", ln, owner="a")
    d.label("loop")
    d.nop()
    d.br("loop", cond="z", reg="r0")
    d.cond("rise", ln, owner="a", prof=d.profile("W", nom=100, min=0, max=INF))
    d.halt()
    assert msgs(d, "T5-watch") == [f"pc 4: {UNBOUNDED_B}"]


# ---------------------------------------------------------------- T5-stall
def test_t5_stall_looks_only_at_lead_blocks_the_staller_leads():
    """After b's POP (in an only block) and a NOP, b's own unbounded COND makes the others wait.
    The POP's block is no lead block, so where it ends decides nothing."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="b", idle=1)
    with d.only("b"):
        d.pop("r1")
    d.nop()
    d.cond("fall", ln, owner="b", prof=d.profile("G", nom=10, min=0, max=INF))
    d.halt()
    assert diags(d) == []
