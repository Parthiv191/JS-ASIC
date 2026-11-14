"""Lead blocks (isa §5 R3-lead): a role block for one party that may hold timed steps.

The leader decides in it; the others skip it and follow the block's fall-through successor
speculatively (ABORT, no CHK, max INF). The leader leaves by the fall-through exit, with no timed
step on that path, or by running a bus COND START/STOP and branching to abort_pc.

The example is I2C. The controller c (P0) clocks SCL; the target t (P1) ACKs its address only if
it matches (a lead block for t), and c ends a frame early when r5 says so (a lead block for c)."""
from types import SimpleNamespace

import pytest

from emu.compiler import CompileError, compile_desc, lower
from emu.desc import Description
from emu.isa import INF, Op, Role, unpack_role_if

from simutil import run_sys

ADDR = 0x5A
DATA = 0xC3
H = 20


def i2c(addr_sent=ADDR, target=None, ack=None, after=None, pre=None):
    """top: START, the address byte (t receives it into r2), `target` (default: t's ACK block),
    then at "ack" the ACK bit (`ack`; default: t sends 0, c reads it into r4), then `after`
    (default: c's STOP sequence and HALT). abort_pc is "top"."""
    d = Description("i2c", parties=("c", "t"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="c", idle=1)
    d.bus("i2c", sda, scl)
    P = SimpleNamespace(d=d, sda=sda, scl=scl, L=d.lane("L", sda, clk=scl, cpol=1, cpha=1),
                        bit=d.profile("B", nom=H, min=0, max=INF), wait=d.profile("W", nom=H, min=0, max=INF),
                        hold=d.profile("Ho", nom=H))
    d.abort_to("top")
    d.li("r1", addr_sent)
    if pre:
        pre(P)
    d.label("top")
    d.cond("start", owner="c", prof=P.wait)
    d.xfer(8, P.L, owner="c", prof=P.bit, src="r1", dst="r2", msb_first=True)
    (target or ack_block)(P)
    d.label("ack")
    (ack or ack_bit)(P)
    (after or stop_and_halt)(P)
    return d


def ack_block(P):
    """t ACKs only its own address; otherwise it waits for the STOP and restarts at top."""
    d = P.d
    with d.lead("t"):
        d.alu("ne", "r3", "r2", ADDR)
        d.br("nack", cond="nz", reg="r3")
        d.br("ack")
        d.label("nack")
        d.cond("stop", owner="c", prof=P.wait)
        d.br("top")


def ack_bit(P):
    P.d.xfer(1, P.L, owner="t", prof=P.bit, src="zero", dst="r4", abort=True)


def stop_seq(P):
    """c's STOP after a transfer: SCL low (a target holding SDA lets it go), SDA low, SCL high,
    then SDA rises while SCL is high (isa §4.7)."""
    d = P.d
    d.cond("fall", P.scl, owner="c", prof=P.wait)
    d.hold(P.sda, 0, owner="c", prof=P.hold)
    d.cond("rise", P.scl, owner="c", prof=P.wait)
    d.cond("stop", owner="c", prof=P.wait)


def stop_and_halt(P):
    stop_seq(P)
    P.d.halt()


def run(d, **kw):
    return run_sys(compile_desc(d), roles=(Role.P0, Role.P1), **kw)


def rule_diags(d):
    return {dg.rule for dg in lower(d).diags}


def xfer_values(t, i):
    return [r.value for r in t.engines[i].records if r.op == "XFER"]


# ---------------------------------------------------------------- the description and the compiler
def test_a_lead_block_is_a_role_block_for_its_party_with_timed_steps():
    low = lower(i2c())
    (sp,) = low.spans
    assert sp.roles == frozenset({Role.P1}) and sp.lead
    assert unpack_role_if(low.instrs[sp.role_if_pc].operand)[0] == 1 << int(Role.P1)
    assert any(low.instrs[pc].op == Op.COND for pc in range(sp.start, sp.end))
    compile_desc(i2c())                            # every rule and the timing analysis pass


def test_a_timed_step_in_an_only_block_is_still_rejected():
    def blk(P):
        with P.d.only("t"):
            P.d.br("ack", cond="z", reg="r2")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")
    assert "R3-timed-in-role" in rule_diags(i2c(target=blk))


def test_only_and_lead_blocks_do_not_merge():
    def blk(P):
        with P.d.only("t"):
            P.d.nop()
        ack_block(P)
    assert [sp.lead for sp in lower(i2c(target=blk)).spans] == [False, True]


def test_lead_takes_one_party():
    d = Description("x", parties=("c", "t"))
    for bad in ("mon", "q"):
        with pytest.raises(ValueError):
            with d.lead(bad):
                pass
    with d.lead("c"):
        with pytest.raises(ValueError):
            with d.lead("c"):
                pass


@pytest.mark.parametrize("bad", ["no abort", "check", "bounded", "untimed", "halt"])
def test_the_successor_is_a_timed_step_with_abort_no_check_and_max_inf(bad):
    def ack(P):
        d = P.d
        if bad == "halt":
            d.halt()
            return
        if bad == "untimed":
            d.nop()
        prof = d.profile("Bb", nom=H, min=0, max=200) if bad == "bounded" else P.bit
        d.xfer(1, P.L, owner="t", prof=prof, src="zero", dst="r4", abort=bad != "no abort",
               check=bad == "check")
    assert "R3-lead" in rule_diags(i2c(ack=ack))


def test_no_timed_step_on_the_path_to_the_fall_through_exit():
    def blk(P):
        with P.d.lead("t"):
            P.d.br("nack", cond="nz", reg="r2")
            P.d.xfer(1, P.L, owner="t", prof=P.bit, src="ones")
            P.d.br("ack")                                         # the fall-through exit
            P.d.label("nack")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")
    assert "R3-lead" in rule_diags(i2c(target=blk))


def test_an_exit_to_abort_pc_runs_a_bus_cond_first():
    def blk(P):
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("top", cond="nz", reg="r3")
    assert "R3-lead" in rule_diags(i2c(target=blk))


def test_a_lead_block_path_may_not_halt():
    def blk(P):
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("ack", cond="z", reg="r3")
            P.d.halt()
    assert "R3-lead" in rule_diags(i2c(target=blk))


def test_timed_steps_in_a_lead_block_are_the_leaders():
    """Owned by the leader, or on a lane whose clock it owns; a bus COND START/STOP is allowed
    whoever owns it. A step the other party owns, on a lane the leader doesn't clock, is not."""
    def blk_ok(P):           # t leads: the NACK bit is t's own (it sends ONES), then STOP
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("ack", cond="z", reg="r3")
            P.d.xfer(1, P.L, owner="t", prof=P.bit, src="ones")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")

    def blk_bad(P):          # t leads but would follow c's SCL fall
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("ack", cond="z", reg="r3")
            P.d.cond("fall", P.scl, owner="c", prof=P.wait)
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")
    assert "R3-lead" not in rule_diags(i2c(target=blk_ok))
    assert "R3-lead" in rule_diags(i2c(target=blk_bad))


def controller_block(P, body_before_stop=lambda P: None):
    """c ends the frame early when r5 != 0 (clearing r5 for the next round); else it sends DATA."""
    d = P.d
    with d.lead("c"):
        d.br("data", cond="z", reg="r5")
        d.li("r5", 0)
        body_before_stop(P)
        stop_seq(P)
        d.br("top")
    d.label("data")
    d.xfer(8, P.L, owner="c", prof=P.bit, src="r6", dst="r7", msb_first=True, abort=True)
    stop_and_halt(P)


def test_after_a_pop_a_step_inside_a_lead_block_makes_the_others_wait():
    """T5-stall (isa §6): after c's POP its next timed step on one path is a bounded COND inside
    its lead block (the others wait in the successor, max INF); on the other, the successor, a
    clocked XFER c clocks. Outside a lead block the same bounded COND after a POP is rejected."""
    def pop_then_cond(P):
        P.d.pop("r6")
        P.d.cond("fall", P.scl, owner="c", prof=P.d.profile("Bc", nom=H, min=0, max=3 * H))
        P.d.cond("rise", P.scl, owner="c", prof=P.wait)  # SCL high again for the STOP sequence (R2-edge)
    def blk_inside(P):
        controller_block(P, body_before_stop=pop_then_cond)
    compile_desc(i2c(after=blk_inside))
    with pytest.raises(CompileError, match="T5-stall"):
        compile_desc(i2c(after=lambda P: (pop_then_cond(P), stop_and_halt(P))))


def test_after_a_pop_a_lead_block_s_first_step_waits_as_its_successor_does():
    """T5-stall: c POPs (in an only block) right before its lead block, whose first step is a
    bounded COND. The others wait in the block's successor, a clocked XFER c clocks (max INF), so
    that COND makes them wait too. (The block always leaves by its STOP.)"""
    def blk(P):
        d = P.d
        with d.only("c"):
            d.pop("r6")
        with d.lead("c"):
            d.cond("fall", P.scl, owner="c", prof=d.profile("Bc", nom=H, min=0, max=3 * H))
            d.cond("rise", P.scl, owner="c", prof=P.wait)
            stop_seq(P)
            d.br("top")
        d.xfer(8, P.L, owner="c", prof=P.bit, src="r6", dst="r7", msb_first=True, abort=True)
        stop_and_halt(P)
    d = i2c(after=blk)
    (sp,) = [sp for sp in lower(d).spans if sp.roles == frozenset({Role.P0}) and sp.lead]
    assert lower(d).instrs[sp.start].op == Op.COND
    compile_desc(d)


# ---------------------------------------------------------------- on the model, both parties
def test_the_target_acks_its_own_address():
    t = run(i2c())
    assert xfer_values(t, 0)[1] == 0 and not t.engines[1].violations   # c read ACK (SDA low)
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP"]


def test_a_target_that_is_not_addressed_waits_for_the_stop_and_restarts():
    d = i2c(addr_sent=ADDR ^ 1)
    t = run(d)
    assert xfer_values(t, 0)[1] == 1                                    # NACK: nobody pulled SDA
    low = lower(d)
    top = low.image.abort_pc
    (sp,) = low.spans
    nack_stop = next(pc for pc in range(sp.start, sp.end) if low.instrs[pc].op == Op.COND)
    pcs = [pc for pc, _ in t.engines[1].starts]
    assert nack_stop in pcs and pcs.count(top) == 2                     # back to top after STOP
    assert all(r.pc != sp.end for r in t.engines[1].records)           # t never sent the ACK bit


def test_the_others_follow_the_successor_until_the_leaders_stop():
    """Round 1: c's lead block ends the frame (r5 = 1) while t, which ACKed, follows the data
    byte speculatively; c's STOP makes t's data XFER a bus exit to top. Round 2: the byte goes
    through."""
    def pre(P):
        P.d.li("r5", 1)
        P.d.li("r6", DATA)
    t = run(i2c(after=controller_block, pre=pre))
    assert [e.kind for e in t.engines[1].events] == ["BUS"]
    assert xfer_values(t, 1)[-1] == DATA and not t.engines[0].violations
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP", "START", "STOP"]


# ---------------------------------------------------------------- review of #40
def short_successor_block(P, succ):
    """c leads: on r5 != 0 it ends the frame with its STOP sequence (one SCL pulse, then STOP);
    `succ(P)` is the block's successor, followed speculatively by t."""
    d = P.d
    with d.lead("c"):
        d.br("data", cond="z", reg="r5")
        d.li("r5", 0)
        stop_seq(P)
        d.br("top")
    d.label("data")
    succ(P)
    d.xfer(8, P.L, owner="c", prof=P.bit, src="r6", dst="r7", msb_first=True)   # no abort
    stop_and_halt(P)


@pytest.mark.parametrize("kind", ["1-bit xfer", "cond fall scl", "cond fall sda", "cond stop", "hold", "unclocked"])
def test_the_exit_path_may_not_complete_the_successor(kind):
    """R3-lead: the others wait in the successor S for the leader's bus COND. A leader that makes
    as many clock edges as S needs (a 1-bit S, one SCL pulse), drives S's COND line, or an S that
    completes without the leader (COND STOP, which the leader's STOP completes; HOLD; an
    unclocked XFER) would let them run on past S, into steps without ABORT."""
    def succ(P):
        d = P.d
        if kind == "1-bit xfer":
            d.xfer(1, P.L, owner="c", prof=P.bit, src="r6", abort=True)
        elif kind == "cond fall scl":
            d.cond("fall", P.scl, owner="c", prof=P.wait, abort=True)
        elif kind == "cond fall sda":
            d.cond("fall", P.sda, owner="c", prof=P.wait, abort=True)
        elif kind == "cond stop":
            d.cond("stop", owner="c", prof=P.wait, abort=True)
        elif kind == "hold":
            d.hold(P.scl, 0, owner="c", prof=d.profile("Hi", nom=H, min=0, max=INF), abort=True)
        else:
            u = d.line("u", owner="c", idle=1)
            d.xfer(8, u, owner="c", prof=d.profile("U", nom=H, min=0, max=INF), src="r6", abort=True, check=False)
    d = i2c(after=lambda P: short_successor_block(P, succ))
    assert "R3-lead" in rule_diags(d)


def test_an_eight_bit_successor_outlasts_the_stop_sequence():
    """The same block with an 8-bit S: the STOP sequence's one SCL pulse (2 edges) is fewer than
    the 16 edges S needs, so the STOP still finds t in S."""
    d = i2c(after=lambda P: short_successor_block(
        P, lambda P: P.d.xfer(8, P.L, owner="c", prof=P.bit, src="r6", dst="r7", msb_first=True, abort=True)))
    assert "R3-lead" not in rule_diags(d)


def i2c_start_successor(at_role_if, exit_start=False, start_before=False):
    """c may send a bus-clearing STOP before the first START, in a lead block whose successor is
    the followed START; abort_pc is that START, or the block's own ROLE_IF."""
    d = Description("i2c", parties=("c", "t"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="c", idle=1)
    d.bus("i2c", sda, scl)
    P = SimpleNamespace(d=d, sda=sda, scl=scl, L=d.lane("L", sda, clk=scl, cpol=1, cpha=1),
                        bit=d.profile("B", nom=H, min=0, max=INF), wait=d.profile("W", nom=H, min=0, max=INF),
                        hold=d.profile("Ho", nom=H))
    d.abort_to("top")
    d.li("r1", ADDR)
    d.li("r5", 1)
    if at_role_if:
        d.label("top")
    if start_before:            # abort_pc runs another START before reaching S
        d.cond("start", owner="c", prof=P.wait)
    with d.lead("c"):
        d.br("go", cond="z", reg="r5")
        d.li("r5", 0)
        if exit_start:          # the exit's bus COND is a (repeated) START: it completes S
            d.cond("start", owner="c", prof=P.wait)
        else:
            stop_seq(P)
        d.br("top")
    d.label("go" if at_role_if else "top")
    if not at_role_if:
        d.label("go")
    d.cond("start", owner="c", prof=P.wait, abort=True)
    d.xfer(8, P.L, owner="c", prof=P.bit, src="r1", dst="r2", msb_first=True)
    ack_bit(P)
    stop_and_halt(P)
    return d


@pytest.mark.parametrize("at_role_if", [False, True], ids=["abort_pc=S", "abort_pc=ROLE_IF"])
def test_a_followed_start_successor_with_the_exit_back_to_it(at_role_if):
    """A followed START waits through the leader's STOP, and the leader's next timed step from
    abort_pc is that START: R3-lead accepts it (the path walk checks the exit to abort_pc first).
    With abort_pc at the START it compiles and runs clean. At the block's ROLE_IF, T5-abort still
    rejects it: from there a restarted controller's first timed step may be the STOP sequence's
    SCL fall, not a COND START/STOP."""
    d = i2c_start_successor(at_role_if)
    assert "R3-lead" not in rule_diags(d)
    if at_role_if:
        with pytest.raises(CompileError, match="T5-abort"):
            compile_desc(d)
        return
    t = run(d)
    assert not t.engines[0].violations and not t.engines[1].violations
    assert xfer_values(t, 0)[1] == 0                                     # t ACKed


def test_nothing_timed_after_the_exit_paths_bus_cond():
    def blk(P):
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("ack", cond="z", reg="r3")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.xfer(1, P.L, owner="t", prof=P.bit, src="ones")   # the leader's own, but after the exit's STOP
            P.d.br("top")
    assert "R3-lead" in rule_diags(i2c(target=blk))


def test_a_pop_in_a_lead_block_needs_a_successor_that_waits_for_the_leader():
    """T5-stall: a step in a lead block makes the others wait only if its successor S does (an
    unbounded COND the leader does, or a clocked XFER it clocks). Here t leads and POPs, but S, the
    ACK bit, is clocked by c, which never waits for t. The POP is on the exit path only, so the
    next timed step after it is the in-block STOP."""
    def blk(P):
        with P.d.lead("t"):
            P.d.alu("ne", "r3", "r2", ADDR)
            P.d.br("ack", cond="z", reg="r3")
            P.d.pop("r6")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")
    with pytest.raises(CompileError, match="T5-stall"):
        compile_desc(i2c(target=blk))


def test_only_a_lead_block_may_branch_to_abort_pc():
    """R3-escape: an only() block that branches to abort_pc restarts its roles on a different
    timed step from the others, and no rule checks that; only a lead block's exit (R3-lead) may."""
    d = Description("x", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.abort_to("top")
    d.label("top")
    d.cond("fall", ln, owner="a", prof=d.profile("P", nom=10, min=0, max=INF))
    with d.only("a"):
        d.br("top")
    d.halt()
    assert "R3-escape" in {dg.rule for dg in lower(d).diags}


@pytest.mark.parametrize("variant", ["exit_start", "start_before"])
def test_a_start_successor_needs_a_stop_exit_and_a_way_back_to_it(variant):
    """A START exit completes the others' followed START; a way back from abort_pc that runs
    another START first completes it there. Either way the others run on past S."""
    # abort_pc at S itself for the START exit, so only the exit check can catch it
    d = i2c_start_successor(at_role_if=variant == "start_before", **{variant: True})
    assert "R3-lead" in rule_diags(d)


def test_a_cond_successor_on_the_bus_line_is_completed_by_the_exits_own_bus_cond():
    """S is c's COND FALL on SDA; the exit is a bare STOP, whose own SDA edge would complete S."""
    def after(P):
        d = P.d
        with d.lead("c"):
            d.br("data", cond="z", reg="r5")
            d.cond("stop", owner="c", prof=P.wait)
            d.br("top")
        d.label("data")
        d.cond("fall", P.sda, owner="c", prof=P.wait, abort=True)
        stop_and_halt(P)
    assert "R3-lead" in rule_diags(i2c(after=after))


# ---------------------------------------------------------------- a step on the other party's clock
def spi_lead(k: int = 0, mx: int = 20, s_nom: int = 20, pre_hold: bool = False):
    """SELECT: a's START, a HOLD of 12 (a's), then a's k NOPs. b leads a block: its bounded XFER on
    a's clock (half period 20, max `mx`), a's STOP, back to top. The block's successor S is the same
    XFER, unbounded, with half period `s_nom`. With `pre_hold`, b HOLDs its line y first in the block.
    a skips the block and clocks S meanwhile: those are the edges b's XFER gets."""
    d = Description("s", parties=("a", "b"), tolerance_ppm=0)
    cs = d.line("cs", owner="a", idle=1)
    clk = d.line("clk", owner="a", idle=0)
    miso = d.line("miso", owner="b", idle=0)
    x = d.line("x", owner="a", idle=1)
    y = d.line("y", owner="b", idle=1)
    d.bus("select", cs)
    L = d.lane("L", miso, clk=clk)
    W = d.profile("W", nom=20, min=0, max=INF)
    d.abort_to("top")
    d.li("r2", 1)
    d.label("top")
    d.cond("start", owner="a", prof=W)
    d.hold(x, 0, owner="a", prof=d.profile("X", nom=12, min=11, max=13))
    if k:
        with d.only("a"):
            for _ in range(k):
                d.nop()
    with d.lead("b"):
        d.br("go", cond="z", reg="r2")
        if pre_hold:
            d.hold(y, 0, owner="b", prof=d.profile("Y", nom=12, min=11, max=13))
        d.xfer(8, L, owner="b", prof=d.profile("Bb", nom=20, min=0, max=mx), src="ones")
        d.cond("stop", owner="a", prof=W)
        d.br("top")
    d.label("go")
    d.xfer(8, L, owner="b", prof=d.profile("B", nom=s_nom, min=0, max=INF), src="zero", abort=True)
    d.cond("stop", owner="a", prof=W)
    d.halt()
    return d


def t3(d) -> list[str]:
    try:
        compile_desc(d)
    except CompileError as e:
        return [x.msg for x in e.diags if x.rule == "T3-latency"]
    return []


@pytest.mark.parametrize("k,bad", [(0, False), (10, True)])
def test_a_lead_block_step_on_the_other_party_s_clock_times_it_from_the_successor(k, bad):
    """isa §6 T3: a skips b's block and clocks its successor S, so b's bounded XFER gets S's edges,
    timed from a's latest start of S. After a's HOLD (a completes by 2 − 12 = −10, b at −12 + 6 +
    EDGE_LAT = −3) a starts S by −10 + ISSUE + 2 (the lead ROLE_IF) = −6, which re-bases to 0: the
    first edge is seen at 20 + EDGE_LAT = 23. b starts at the earliest at −3 + 1 + 2 + 2 = 2 (the
    ROLE_IF and its BR): 23 − 2 = 21 ≤ max 20 + QUANT. With a's 10 NOPs (and their ROLE_IF, 22
    cycles) a starts S by 16 and b at 4: 16 + 23 − 4 = 35 > 21, and on the model b times out."""
    want = ["pc 16: P1 may start at 4 and time out: the first clock edge may be visible only at 39 (drift 0), "
            "past max 20 + QUANT after it"]
    assert t3(spi_lead(k)) == (want if bad else [])


def test_the_rejected_lead_block_step_times_out_on_the_model():
    img = compile_desc(spi_lead(10), check_timing=False)
    t = run_sys(img, roles=(Role.P0, Role.P1), max_ticks=3000)
    assert [(e.kind, e.pc) for e in t.engines[1].events] == [("TIMEOUT", 16)]


NO_CLOCK = ("P1 may time out: the clock owner P0 doesn't run this step; it clocks it only as a lead "
            "block's successor on the same clock with the same half period, after a step before the block")


@pytest.mark.parametrize("variant", ["other half period", "after a step in the block", "clock owner halted"])
def test_a_bounded_clock_follower_whose_clock_owner_never_clocks_the_step_fails_t3(variant):
    """Where the clock owner can't run the step and S gives no start for it, its edges may never
    come: S on another half period, a step after another in the block (a's start of S isn't
    tracked across it), or a clock owner that HALTed."""
    if variant == "clock owner halted":
        d = Description("h", parties=("a", "b"), tolerance_ppm=0)
        clk = d.line("clk", owner="a", idle=0)
        dat = d.line("dat", owner="b", idle=0)
        cs = d.line("cs", owner="a", idle=1)
        lane = d.lane("L", data=dat, clk=clk)
        d.cond("fall", cs, owner="a", prof=d.profile("SU", nom=40, min=0, max=INF))
        with d.only("a"):
            d.halt()
        d.xfer(8, lane, owner="b", prof=d.profile("HALF", nom=10, min=0, max=14), src="ones")
        d.halt()
    else:
        d = spi_lead(s_nom=30) if variant == "other half period" else spi_lead(pre_hold=True)
    (m,) = t3(d)
    assert m.split(": ", 1)[1] == NO_CLOCK
