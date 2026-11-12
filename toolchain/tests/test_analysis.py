import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF

from descs import spi_like, uart_like  # noqa: F401


def diag_rules(desc, check_timing=True):
    try:  # R2-edge has its own tests (test_edge_rule.py); these loops fall on a low line
        compile_desc(desc, check_timing=check_timing, check_levels=False)
    except CompileError as e:
        return {d.rule for d in e.diags}
    return set()


def two_party():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    return d, ln, p


# ---------------------------------------------------------------- R2: branch taint
def test_r2_rejects_branch_on_popped_reg():
    d, ln, p = two_party()
    d.label("x")
    with d.only("a"):
        d.pop("r0")
    d.br("x", cond="z", reg="r0")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_rejects_branch_on_reg_never_written():
    """Registers keep their values across RUN (isa §1), so the two engines may start with
    different values: a register is tainted until the program writes it."""
    d, ln, p = two_party()
    d.label("x")
    d.br("x", cond="z", reg="r0")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_rejects_branch_on_reg_written_in_role_block():
    d, ln, p = two_party()
    d.label("x")
    with d.only("b"):
        d.li("r1", 3)
    d.br("x", cond="nz", reg="r1")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_accepts_branch_on_xfer_dst():
    d, ln, p = two_party()
    d.label("x")
    with d.only("a"):
        d.pop("r0")
    d.xfer(8, ln, owner="a", prof=p, src="r0", dst="r1")
    d.alu("eq", "r2", "r1", 0x55)
    d.br("x", cond="nz", reg="r2")
    assert diag_rules(d, check_timing=False) == set()


def test_r2_taint_joins_over_loop():
    """r0 is clean after LI but tainted along the back edge, so the loop head is tainted."""
    d, ln, p = two_party()
    d.li("r0", 0)
    d.label("head")
    d.br("out", cond="z", reg="r0")
    with d.only("a"):
        d.pop("r0")
    d.br("head")
    d.label("out")
    d.halt()
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_rejects_jpin_outside_block():
    d, ln, p = two_party()
    d.label("x")
    d.raw_jpin(ln, 0, "x")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}
    d2, ln2, p2 = two_party()
    d2.label("y")
    with d2.only("b"):
        d2.nop()
        d2.label("z")
        d2.raw_jpin(ln2, 0, "z")
    assert diag_rules(d2, check_timing=False) == set()


# ---------------------------------------------------------------- T rules
def test_t1_uart_period_boundary():
    """Two boundaries at d = 2% (isa §6, U = ISSUE = 2).

    T1, the stop-bit sample (A = 9P from the anchor, slot end 10P): ceil(d*10P) + QUANT + ISSUE
    < P//2. P=14 -> 3+3 < 7 holds; P=13 -> 3+3 < 6 fails.
    T5-watch, the next start edge: the follower finishes the stop bit at -P + P//2 + E, then runs
    BR and ROLE_IF (U each) and starts at most ISSUE late, so L = -P + P//2 + 9. It must be
    watching by the event at 0 + E: L + ceil(d*10P) <= 3, i.e. ceil(P/2) - ceil(P/5) >= 6.
    P=19 -> 10-4 = 6 holds; P=18 -> 9-4 = 5 fails.
    """
    assert diag_rules(uart_like(19)) == set()
    assert diag_rules(uart_like(18)) == {"T5-watch"}
    assert diag_rules(uart_like(14)) == {"T5-watch"}
    assert diag_rules(uart_like(13)) == {"T1-sample", "T5-watch"}
    assert diag_rules(uart_like(434)) == set()


def test_t1_rejects_unanchored_loop():
    d, ln, p = two_party()
    d.label("x")
    d.hold(ln, 1, owner="a", prof=p)
    d.br("x")
    assert "T1-unanchored" in diag_rules(d)


def test_t2_rejects_tight_cond_window():
    d, ln, p = two_party()
    tight = d.profile("TIGHT", nom=20, min=20, max=20)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.cond("rise", ln, owner="a", prof=tight)
    d.halt()
    assert diag_rules(d) == {"T2-window"}


def test_t3_rejects_spi_target_half_period_4():
    """Later bits need H >= EDGE_LAT + 1 + ceil(d*2H): H=4 -> 4 >= 5 fails."""
    assert "T3-latency" in diag_rules(spi_like(4))


def test_t3_accepts_7():
    """Bit 0 (cpha 0): the target starts at most E + ISSUE = 5 after the clock owner's tb, so
    H >= 5 + 1 + ceil(d*H) = 7; H=6 fails only the bit-0 bound. Clocked XFER is exempt from
    T5-overrun (isa §6)."""
    assert diag_rules(spi_like(7)) == set()
    assert diag_rules(spi_like(6)) == {"T3-latency"}


def test_t4_rejects_min_gt_nom():
    d, ln, p = two_party()
    bad = d.profile("BAD", nom=10, min=12, max=14)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bad)
    d.halt()
    assert "T4-profile" in diag_rules(d)


def test_t4_rejects_hold_with_idle_profile():
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=None)
    d.halt()
    assert "T4-profile" in diag_rules(d)


def test_t5_rejects_long_follower_gap():
    """After the anchor, the owner is ready at 2 (event at max(nom=2, 2)); rx starts at
    E + 1 + 7 = 11 > 2 + E. MON skips the block and starts at 5, which is fine."""
    d = Description("t", parties=("tx", "rx"))
    ln = d.line("l", owner="tx", idle=1)
    soon = d.profile("SOON", nom=2, min=0, max=INF)
    d.cond("fall", ln, owner="tx")
    with d.only("rx"):
        for _ in range(6):
            d.nop()
    d.cond("rise", ln, owner="tx", prof=soon)
    d.halt()
    assert diag_rules(d) == {"T5-watch"}


def test_t5_rejects_owner_overrun():
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    with d.only("a"):
        d.nop()
        d.nop()
    d.hold(ln, 0, owner="a", prof=p)
    d.halt()
    assert diag_rules(d) == {"T5-overrun"}


def test_r2_rejects_br_to_outside_block():
    """isa-v0 §5 R2: BR TO is only legal inside a role block; `to` differs between roles."""
    d, ln, p = two_party()
    d.label("x")
    d.br("x", cond="to")
    d.halt()
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_accepts_br_to_inside_block():
    d, ln, p = two_party()
    with d.only("a"):
        d.label("x")
        d.br("x", cond="to")
    d.halt()
    assert diag_rules(d, check_timing=False) == set()


def test_t1_message_names_the_real_bound():
    """At 0 ppm a 4-cycle slot still fails: QUANT + ISSUE = 3 is not < 4//2. The message must say so."""
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=4))
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    (msg,) = [x.msg for x in e.value.diags if x.rule == "T1-sample"]
    assert "drift 0 + QUANT 1 + ISSUE 2 = 3" in msg and "P//2 = 2" in msg


@pytest.mark.parametrize("P,ok", [(7, False), (8, True)])
def test_t1_shortest_hold_slot_at_zero_ppm(P, ok):
    """Even without drift a follower needs QUANT + ISSUE = 3 < P//2, so the shortest followed slot
    is 8."""
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=d.profile("P", nom=P))
    d.halt()
    assert (diag_rules(d) == set()) == ok


@pytest.mark.parametrize("ppm,ok", [(20000, False), (10000, True)])
def test_t1_sixteen_bit_unclocked_frame(ppm, ok):
    """P=16 after a 1P start bit: the last sample is at A + 16P = 272.
    2%: ceil(5.44) + QUANT + ISSUE = 9, not < 8. 1%: 3 + 3 = 6 < 8."""
    from descs import slack
    d = Description("t", parties=("a", "b"), tolerance_ppm=ppm)
    ln = d.line("l", owner="a", idle=1)
    s = slack(16, ppm)
    bit = d.profile("BIT", nom=16, min=16 - s, max=16 + s)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    d.xfer(16, ln, owner="a", prof=bit)
    d.halt()
    assert ("T1-sample" not in diag_rules(d)) == ok


def test_t2_accepts_cond_with_min_zero():
    """The engine checks EARLY only when min > 0 (isa-v0 §4.1), so a COND with min = 0 needs
    no lower margin. nom = 1 used to fail: window [0,11] does not contain [-1,3]."""
    d, ln, p = two_party()
    soon = d.profile("SOON", nom=1, min=0, max=11)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.cond("rise", ln, owner="a", prof=soon)
    d.halt()
    assert diag_rules(d) == set()


def test_t2_still_checks_cond_max_when_min_is_zero():
    d, ln, p = two_party()
    soon = d.profile("SOON", nom=1, min=0, max=2)  # needs max >= 1 + drift 1 + QUANT 1 = 3
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.cond("rise", ln, owner="a", prof=soon)
    d.halt()
    assert diag_rules(d) == {"T2-window"}


# ---------------------------------------------------------------- D2: CHK XFER at an anchor
def _xfer_at_anchor(start_hold, check):
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    if start_hold:
        d.hold(ln, 0, owner="a", prof=p)
    d.xfer(8, ln, owner="a", prof=p, check=check)
    d.halt()
    return d


def test_t2_rejects_chk_xfer_at_anchor():
    """Decision D2 (isa-v0 §6): kept. Right after an anchor the owner drives bit 0 a cycle
    late (issue slot), and the follower can measure that edge 2 cycles out, past the engine's
    0 ± QUANT window. The message names the two ways around it."""
    with pytest.raises(CompileError) as e:
        compile_desc(_xfer_at_anchor(start_hold=False, check=True))
    (msg,) = [x.msg for x in e.value.diags if x.rule == "T2-window"]
    assert "boundary 0" in msg and "drop check" in msg and "start-bit HOLD" in msg


def test_t2_accepts_xfer_at_anchor_without_check():
    assert diag_rules(_xfer_at_anchor(start_hold=False, check=False)) == set()


def test_t2_accepts_chk_xfer_after_start_hold():
    assert diag_rules(_xfer_at_anchor(start_hold=True, check=True)) == set()


# ---------------------------------------------------------------- audit follow-ups
def test_r2_rejects_branch_on_open_drain_xfer_dst():
    """On an open-drain line another device can pull a 1 low. The owner stores what it sent and
    the followers what they read, so the value can differ between roles (isa-v0 §4.3)."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", kind="od", owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    d.label("x")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.xfer(8, ln, owner="a", prof=p, dst="r0")
    d.br("x", cond="nz", reg="r0")
    d.halt()
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def test_r2_taints_registers_at_the_abort_target():
    """A role that aborts restarts at the abort target while the others carry on, so a counter
    kept across frames can differ between roles (b aborts before its increment)."""
    d, ln, p = two_party()
    d.abort_to("top")
    d.li("r0", 0)
    d.label("top")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p, abort=True)
    d.alu("add", "r0", "r0", 1)
    d.alu("ne", "r1", "r0", 3)
    d.br("top", cond="nz", reg="r1")
    d.halt()
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


def _cond_then(next_step, abort=False):
    d, ln, p = two_party()
    w = d.profile("W", nom=10, min=8, max=100)
    d.abort_to("end")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.cond("rise", ln, owner="a", prof=w, abort=abort)
    next_step(d, ln, p)
    d.label("end")
    d.halt()
    return d


def test_t5_rejects_a_timed_step_after_a_cond_that_can_time_out():
    """A follower that times out finishes up to max - nom + QUANT + EDGE_LAT + 1 cycles late and
    no longer shares the owner's timeline; the model then logs OVERRUN on the next steps."""
    d = _cond_then(lambda d, ln, p: d.hold(ln, 1, owner="a", prof=p))
    assert diag_rules(d) == {"T5-timeout"}


def test_t5_accepts_an_unbounded_cond_after_a_cond_that_can_time_out():
    """The trailing COND is unbounded, as T5-timeout requires. It plans its edge at nom = 4: a
    follower of the rise starts watching at most E + ISSUE = 5 after it, before the edge is
    visible at 4 + E. With nom = 0 the owner's edge could come at 1 and be missed (T5-watch)."""
    gap = lambda d: d.profile("GAP", nom=4, min=0, max=INF)  # noqa: E731
    d = _cond_then(lambda d, ln, p: d.cond("fall", ln, owner="a", prof=gap(d)))
    assert diag_rules(d) == set()
    assert diag_rules(_cond_then(lambda d, ln, p: d.cond("fall", ln, owner="a"))) == {"T5-watch"}


def test_t5_accepts_a_timed_step_after_a_cond_with_abort():
    d = _cond_then(lambda d, ln, p: d.hold(ln, 1, owner="a", prof=p), abort=True)
    assert diag_rules(d) == set()


def test_t5_rejects_an_abort_target_that_starts_with_a_hold():
    d, ln, p = two_party()
    d.abort_to("mid")
    d.cond("fall", ln, owner="a")
    d.label("mid")
    d.hold(ln, 0, owner="a", prof=p, abort=True)
    d.halt()
    assert diag_rules(d) == {"T5-abort"}


def test_t5_rejects_an_abort_target_whose_first_cond_the_aborting_role_owns():
    """b aborts on a's HOLD; at the target b would drive its own edge into a's frame. Only a COND
    START/STOP the aborting role does is accepted (isa §6 T5-abort): on a bus the others exit on it."""
    d = Description("t", parties=("a", "b"))
    la = d.line("la", owner="a", idle=1)
    lb = d.line("lb", owner="b", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    d.abort_to("x")
    d.cond("fall", la, owner="a")
    d.hold(la, 0, owner="a", prof=p, abort=True)
    d.label("x")
    d.cond("fall", lb, owner="b")
    d.halt()
    assert diag_rules(d) == {"T5-abort"}


def test_t5_accepts_uart_abort_to_frame_start():
    assert diag_rules(uart_like(20)) == set()  # P >= 19 at 2% (test_t1_uart_period_boundary)


def test_t5_overrun_owner_hold_after_untimed():
    """An owner HOLD is planned at offset 0, so it may start at most ISSUE = 2 late (isa §6
    T5-overrun). At pc 0 it starts at 2 (reset completes at 0): accepted. After an LI it starts
    at 4: rejected."""
    def desc(pre_li):
        d = Description("t", parties=("a", "b"), tolerance_ppm=0)
        ln = d.line("l", owner="a", idle=1)
        if pre_li:
            d.li("r0", 1)
        d.hold(ln, 0, owner="a", prof=d.profile("P", nom=16))
        d.halt()
        return d
    assert diag_rules(desc(False)) == set()
    assert diag_rules(desc(True)) == {"T5-overrun"}


@pytest.mark.parametrize("nops,ok", [(2, True), (3, False)])
def test_t5_overrun_hold_completion_carries_the_issue_slot(nops, ok):
    """Rule cross-check (final review, issue 1). An owner HOLD drives, and completes, when it
    starts: up to ISSUE late (isa §4.2). At 0 ppm with nom 8 the COND acts at 2 (tb := 2); HOLD 0
    starts at 4, 2 late, so it completes at -nom + 2 = -6 from its end tb 10. ROLE_IF plus k NOPs
    put HOLD 1 at 4 + 2(k + 1) against its plan at 10: 2 late for k = 2 (inside ISSUE), 4 late for
    k = 3 (OVERRUN). The compiler must accept exactly the programs the model runs clean."""
    from emu.isa import Role
    from emu.model.engine import Engine
    d = Description("t", parties=("a", "b"), tolerance_ppm=0)
    ln = d.line("l", owner="a", idle=1)
    bit = d.profile("P", nom=8)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=bit)
    with d.only("a"):
        for _ in range(nops):
            d.nop()
    d.hold(ln, 1, owner="a", prof=bit)
    d.halt()
    assert diag_rules(d) == (set() if ok else {"T5-overrun"})
    e = Engine(compile_desc(d, check_timing=False), Role.P0)
    for _ in range(60):
        if e.halted:
            break
        e.cycle([1] * 8)
    assert [(ev.kind, ev.measured) for ev in e.events] == ([] if ok else [("OVERRUN", 4)])


def test_r2_rejects_branch_on_raw_evt_reg():
    """isa §6 R2: RAW EVT taints its rd (evt differs between roles). r0 starts clean (LI), since
    every register is tainted at entry."""
    d, ln, p = two_party()
    d.li("r0", 1)
    d.label("x")
    d.raw_evt("r0", 0)
    d.br("x", cond="z", reg="r0")
    assert diag_rules(d, check_timing=False) == {"R2-branch"}


# ---------------------------------------------------------------- T5-stall
def test_t5_stall_accepts_pop_then_an_unbounded_cond_it_does():
    """isa §6: the library UART shape. b never pops, so its followed COND is fine."""
    d, ln, p = two_party()
    with d.only("a"):
        d.pop("r0")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    assert "T5-stall" not in diag_rules(d)


def test_t5_stall_rejects_pop_then_an_owned_hold():
    d, ln, p = two_party()
    with d.only("a"):
        d.pop("r0")
    d.hold(ln, 0, owner="a", prof=p)
    assert "T5-stall" in diag_rules(d)


def test_t5_stall_rejects_pop_then_a_followed_step():
    """The stricter reading (decisions 11-10): the very next timed step must be the staller's."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="b", idle=1)
    with d.only("a"):
        d.pop("r0")
    d.cond("fall", ln, owner="b")
    assert "T5-stall" in diag_rules(d)


def test_t5_stall_rejects_an_unclocked_src_fifo_owner():
    """The XFER itself would start late; only a clock owner (or a lead block) makes followers wait."""
    d, ln, p = two_party()
    d.cond("fall", ln, owner="a")
    d.xfer(8, ln, owner="a", prof=p, src="fifo")
    assert "T5-stall" in diag_rules(d)


def test_t5_stall_accepts_src_fifo_on_a_lane_its_owner_clocks():
    """A clock owner's src = FIFO waits before its first edge, so the followers wait with it."""
    d = Description("t", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=0)
    mosi = d.line("mosi", owner="a", idle=0)
    half = d.profile("HALF", nom=8, min=7, max=INF)
    d.xfer(8, d.lane("out", data=mosi, clk=sck), owner="a", prof=half, src="fifo")
    assert "T5-stall" not in diag_rules(d)
def _xfer_then_hold(kind):
    """XFER 8, 5 NOPs, then an owned HOLD: the gap only fits a PP owner, which finishes on its last
    bit. An OD owner reads its bits back and finishes P//2 + E later (isa §6 completion offsets)."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", kind=kind, owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    d.cond("fall", ln, owner="a")
    d.xfer(8, ln, owner="a", prof=p)
    for _ in range(5):
        d.nop()
    d.hold(ln, 1, owner="a", prof=p)
    d.halt()
    return d


def test_t5_overrun_uses_an_od_owners_readback_completion():
    from simutil import run_sys
    from emu.isa import Role
    assert "T5-overrun" not in diag_rules(_xfer_then_hold("pp"))
    assert "T5-overrun" in diag_rules(_xfer_then_hold("od"))
    # the model agrees: the OD owner's HOLD starts late
    t = run_sys(compile_desc(_xfer_then_hold("od"), check_timing=False), roles=(Role.P0,))
    assert [e.kind for e in t.engines[0].events] == ["OVERRUN"]


def _clocked(bounded, data_owner="a", fifo=False):
    """a clocks a lane whose data line `data_owner` owns. Without `fifo`, a POPs first."""
    d = Description("t", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=0)
    data = d.line("data", owner=data_owner, idle=0)
    half = d.profile("HALF", nom=8, min=7, max=9 if bounded else INF)
    if not fifo:
        with d.only("a"):
            d.pop("r0")
    d.xfer(8, d.lane("ln", data=data, clk=sck), owner=data_owner, prof=half,
           src="fifo" if fifo else "zero")
    return d


def test_t5_stall_needs_an_unbounded_clocked_xfer():
    """A clock follower's wait for the first edge times out at max + QUANT from its own start
    (isa §4.3), so only an unbounded clocked XFER makes it wait through a stall."""
    assert "T5-stall" in diag_rules(_clocked(bounded=True))
    assert "T5-stall" in diag_rules(_clocked(bounded=True, fifo=True))
    assert "T5-stall" not in diag_rules(_clocked(bounded=False))


def test_t5_stall_accepts_a_read_its_staller_clocks():
    """A master read after a POP: a clocks the lane and b owns the data. The followers of the
    clock wait for a, so the step counts as a's (isa §6: a clocked XFER it clocks)."""
    assert "T5-stall" not in diag_rules(_clocked(bounded=False, data_owner="b"))


def test_t5_stall_covers_mon():
    """MON owns no step, so nothing after a MON POP can make the others wait for it."""
    d, ln, p = two_party()
    with d.only("mon"):
        d.pop("r0")
    d.hold(ln, 0, owner="a", prof=p)
    assert "T5-stall" in diag_rules(d)


def test_t5_stall_checks_code_reached_only_from_abort_pc():
    d, ln, p = two_party()
    d.abort_to("recover")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p, abort=True)
    d.halt()
    d.label("recover")
    with d.only("a"):
        d.pop("r0")
    d.hold(ln, 1, owner="a", prof=p)
    d.halt()
    assert "T5-stall" in diag_rules(d)
