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
def spi_lead(k: int = 0, mx: int = 20, s_nom: int = 20, pre_hold: bool = False, s_n: int = 8,
             s_cpha: int = 0, pop: bool = False, fifo: bool = False, stop_max: int = INF):
    """SELECT: a's START, a HOLD of 12 (a's), then a's k NOPs (and with `pop` a POP). b leads a
    block: its bounded 8-bit XFER on a's clock (half period 20, max `mx`, cpha 0), a's STOP (max
    `stop_max`), back to top. The block's successor S is an unbounded XFER on a's clock, b's (or
    with `fifo` a's, from TX, on mosi), of `s_n` bits, half period `s_nom`, cpha `s_cpha`. With
    `pre_hold`, b HOLDs its line y first in the block. a skips the block and clocks S meanwhile:
    those are the edges b's XFER gets."""
    d = Description("s", parties=("a", "b"), tolerance_ppm=0)
    cs = d.line("cs", owner="a", idle=1)
    clk = d.line("clk", owner="a", idle=0)
    miso = d.line("miso", owner="b", idle=0)
    mosi = d.line("mosi", owner="a", idle=0)
    x = d.line("x", owner="a", idle=1)
    y = d.line("y", owner="b", idle=1)
    d.bus("select", cs)
    L = d.lane("L", miso, clk=clk)
    Ls = d.lane("Ls", mosi if fifo else miso, clk=clk, cpha=s_cpha) if fifo or s_cpha else L
    W = d.profile("W", nom=20, min=0, max=INF)
    d.abort_to("top")
    d.li("r2", 1)
    d.label("top")
    d.cond("start", owner="a", prof=W)
    d.hold(x, 0, owner="a", prof=d.profile("X", nom=12, min=11, max=13))
    if k or pop:
        with d.only("a"):
            for _ in range(k):
                d.nop()
            if pop:
                d.pop("r3")
    with d.lead("b"):
        d.br("go", cond="z", reg="r2")
        if pre_hold:
            d.hold(y, 0, owner="b", prof=d.profile("Y", nom=12, min=11, max=13))
        d.xfer(8, L, owner="b", prof=d.profile("Bb", nom=20, min=0, max=mx), src="ones")
        d.cond("stop", owner="a", prof=W if stop_max == INF else d.profile("SB", nom=20, min=0, max=stop_max))
        d.br("top")
    d.label("go")
    B = d.profile("B", nom=s_nom, min=0, max=INF)
    if fifo:
        d.xfer(s_n, Ls, owner="a", prof=B, src="fifo", abort=True)
    else:
        d.xfer(s_n, Ls, owner="b", prof=B, src="zero", abort=True)
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


NO_CLOCK_VARIANTS = {
    "other half period": dict(s_nom=30),
    "S on another cpha": dict(s_cpha=1),
    "after a step in the block": dict(pre_hold=True),
}


@pytest.mark.parametrize("variant", [*NO_CLOCK_VARIANTS, "clock owner halted"])
def test_a_bounded_clock_follower_whose_clock_owner_never_clocks_the_step_fails_t3(variant):
    """Where the clock owner can't run the step and S gives no start for it, its edges may never
    come: S on another half period or cpha, or with fewer bits (a stops clocking after S's), a step
    after another in the block (a's start of S isn't tracked across it), or a HALTed clock owner."""
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
        d = spi_lead(**NO_CLOCK_VARIANTS[variant])
    (m,) = t3(d)
    assert m.split(": ", 1)[1] == NO_CLOCK


@pytest.mark.parametrize("variant", ["pop", "fifo"])
def test_a_stall_on_the_way_to_the_successor_times_out_a_lead_block_step_on_its_clock(variant):
    """T5-stall: a POP before the block, or S taking src = FIFO, lets a stall on its way to S, and
    b's bounded step in the block gets S's edges, so S doesn't make b wait: b times out (with the
    host late, on the model too)."""
    d = spi_lead(**{variant: True})
    with pytest.raises(CompileError, match="T5-stall"):
        compile_desc(d)
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1), max_ticks=4000,
                tx={0: [0x55]}, push_at=400)
    assert "TIMEOUT" in [e.kind for e in t.engines[1].events]


def test_the_exit_s_bus_cond_the_leader_follows_has_max_inf():
    """R3-lead: a makes the exit's STOP from S, whenever its own path gets there, so b, which
    follows it in the block, can't bound it."""
    assert "R3-lead" in rule_diags(spi_lead(stop_max=25))


# ---------------------------------------------------------------- the others wait in S (11-17)
def t_leads_then(succ):
    """t leads after the address byte: on r5 != 0 it follows c's STOP and restarts at top, else it
    falls through to S, `succ(P)`, which c follows speculatively; then c's STOP sequence."""
    def target(P):
        with P.d.lead("t"):
            P.d.br("ack", cond="z", reg="r5")
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")
    return i2c(target=target, ack=succ, pre=lambda P: P.d.li("r5", 1))


def s_cond_on_t_line(P):
    P.d.cond("fall", P.d.line("x", owner="t", idle=1), owner="t", prof=P.wait, abort=True)


def s_on_t_clock(P):
    lane = P.d.lane("Lt", P.d.line("y", owner="t", idle=0), clk=P.d.line("z", owner="t", idle=0))
    P.d.xfer(8, lane, owner="t", prof=P.bit, src="ones", abort=True)


DEADLOCK = ("the leader follows the exit's bus COND, but its owner waits for the leader in the block's "
            "successor and runs no bus detector to leave it (isa §4.7): neither moves")


@pytest.mark.parametrize("succ", [s_cond_on_t_line, s_on_t_clock])
def test_a_followed_exit_cond_whose_owner_waits_in_the_successor_deadlocks(succ):
    """R3-lead: S waits for t (t owns it, or clocks it), so c waits in S; t's exit follows c's
    STOP, and c, the master line's owner, runs no detector, so it never leaves S to make it. On
    the model nobody halts and nothing is logged."""
    d = t_leads_then(succ)
    assert DEADLOCK in {dg.msg for dg in lower(d).diags if dg.rule == "R3-lead"}
    t = run_sys(lower(d).image, roles=(Role.P0, Role.P1), max_ticks=20_000)
    assert not any(e.halted for e in t.engines)
    assert t.engines[0].records[-1].op == "XFER"                         # c's address byte, then S
    assert not t.engines[0].events and not t.engines[1].events


def test_a_followed_exit_cond_is_fine_where_its_owner_runs_the_successor():
    """The same block before the ACK bit, which c clocks: c runs S on its own and makes its STOP."""
    assert "R3-lead" not in rule_diags(t_leads_then(ack_bit))


LAUNCH = ("this exit path's edge on the successor's clock launches its first bit at the others, who "
          "send it on 'sda': a 0 there holds the line, so the exit's START or STOP makes no edge. An "
          "exit may make it only where a register the leader read in the XFER just before the block, "
          "sent from ZERO (an ACK), is not 0: the others, if in S, ran that XFER")


def c_ends_before_t_sends(reg="r5", s_src="r6", before_br=None, ack=None, exit_seq=stop_seq, pre=None,
                          target=None):
    """t ACKs its address (c reads it into r4); c leads: on `reg` != 0 it runs `exit_seq` (its
    STOP sequence, SCL low first) and restarts at top, clearing r5; else S, a byte t sends from
    `s_src`, followed speculatively by t."""
    def after(P):
        d = P.d
        with d.lead("c"):
            if before_br:
                before_br(P)
            d.br("data", cond="z", reg=reg)
            d.li("r5", 0)
            exit_seq(P)
            d.br("top")
        d.label("data")
        d.xfer(8, P.L, owner="t", prof=P.bit, src=s_src, dst="r7", msb_first=True, abort=True)
        stop_and_halt(P)

    def regs(P):
        P.d.li("r5", 1)
        P.d.li("r6", 0x3C)  # bit 7, sent first, is 0
        if pre:
            pre(P)
    return i2c(after=after, ack=ack, pre=regs, target=target)


def launch_diags(d):
    return [dg.msg for dg in lower(d).diags if dg.rule == "R3-lead"]


def test_an_exit_edge_on_the_successors_clock_launches_the_others_bit():
    """R3-lead: c's exit pulses SCL before its STOP while t, which ACKed, waits in S, a byte it
    sends: SCL's fall launches its bit 7, a 0, which holds SDA, so neither c's STOP nor its next
    START makes an edge. On the model t takes c's next address byte as S's last bits, and c reads
    its own address back wrong and gets a NACK, with no event or violation anywhere."""
    d = c_ends_before_t_sends()
    assert launch_diags(d) == [LAUNCH]
    t = run_sys(lower(d).image, roles=(Role.P0, Role.P1))
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP"]  # the exit's STOP, the next START: gone
    assert xfer_values(t, 0)[2:4] == [ADDR & ~0x02, 1]                   # c's 2nd address byte read back, NACK
    assert not t.engines[0].events and not t.engines[1].events


def test_an_exit_on_the_targets_nack_is_fine():
    """The library's shape (i2c_reg): c leaves only on a NACK, r4 = 1 from t's ACK bit, sent from
    ZERO: had t run it, SDA would have read 0, so t isn't in S. Addressed, t ACKs and sends."""
    d = c_ends_before_t_sends(reg="r4")
    assert launch_diags(d) == []
    t = run(d)
    assert xfer_values(t, 0)[1:3] == [0, 0x3C] and not t.engines[0].events


def ack_into(dst, src="zero"):
    return lambda P: P.d.xfer(1, P.L, owner="t", prof=P.bit, src=src, dst=dst, abort=True)


def past_the_ack(P):
    """Both parties may branch past the ACK bit (r3 is 1, so they don't), straight to c's block
    with r4 from before."""
    P.d.br("blk", cond="z", reg="r3")
    ack_bit(P)
    P.d.label("blk")


@pytest.mark.parametrize("variant", ["r4 rewritten first", "ACK from ONES", "ACK into r3", "ACK discarded",
                                     "a branch past the ACK"])
def test_the_nack_exit_needs_the_ack_bit_from_zero_into_the_register_it_tests(variant):
    """Each breaks the inference: r4 isn't what t's ACK bit read, or t's ACK may read 1 though t
    sent it, or c may reach its block without the ACK bit."""
    kw = {"r4 rewritten first": dict(before_br=lambda P: P.d.li("r4", 1)),
          "ACK from ONES": dict(ack=ack_into("r4", src="ones")),
          "ACK into r3": dict(ack=ack_into("r3")),
          "ACK discarded": dict(ack=ack_into("discard")),
          "a branch past the ACK": dict(ack=past_the_ack, target=lambda P: None,
                                        pre=lambda P: P.d.li("r3", 1))}[variant]
    assert launch_diags(c_ends_before_t_sends(reg="r4", **kw)) == [LAUNCH]


def test_the_nack_exit_may_test_r0():
    assert launch_diags(c_ends_before_t_sends(reg="r0", ack=ack_into("r0"))) == []


def test_an_exit_with_no_edge_on_the_successors_clock_or_a_byte_from_ones_is_fine():
    """The others drive S's data only from its first edge, and a byte from ONES never holds SDA."""
    def stop_from_own_zero(P):
        P.d.cond("stop", owner="c", prof=P.wait)
    own_zero = lambda P: P.d.xfer(1, P.L, owner="c", prof=P.bit, src="zero", dst="r4", abort=True)  # noqa: E731
    assert launch_diags(c_ends_before_t_sends(ack=own_zero, exit_seq=stop_from_own_zero)) == []
    assert launch_diags(c_ends_before_t_sends(s_src="ones")) == []


RUNS_ON = ("the exit's bus COND is the leader's own, so the others must wait for the leader in the block's "
           "successor: this one completes without it, and they run on past it into steps the exit's bus "
           "COND doesn't take them out of")


def test_an_own_exit_cond_needs_a_successor_that_waits_for_the_leader():
    """R3-lead: S is t's own COND on x, which t completes while c is still in its block; t runs on
    into c's byte, which has no ABORT, so c's exit STOP doesn't take it out. On the model t takes c's
    STOP sequence and next frame as that byte and halts, while c and MON hang."""
    def after(P):
        d = P.d
        x = d.line("x", owner="t", idle=1)
        with d.lead("c"):
            d.br("data", cond="z", reg="r5")
            d.li("r5", 0)
            stop_seq(P)
            d.br("top")
        d.label("data")
        d.cond("fall", x, owner="t", prof=P.wait, abort=True)
        d.xfer(8, P.L, owner="c", prof=P.bit, src="r6", dst="r7", msb_first=True)
        d.cond("rise", x, owner="t", prof=P.wait)
        stop_and_halt(P)
    d = i2c(after=after, target=lambda P: None, pre=lambda P: (P.d.li("r5", 1), P.d.li("r6", 0x3C)))
    assert launch_diags(d) == [RUNS_ON]
    t = run_sys(lower(d).image)
    assert [e.halted for e in t.engines] == [False, True, False]
    assert not any(e.events for e in t.engines)


def ack_then(more):
    def succ(P):
        ack_bit(P)
        more(P)
    return succ


def t_cond_on_x(P):
    x = P.d.line("x", owner="t", idle=1)
    P.d.cond("fall", x, owner="t", prof=P.wait)
    P.d.cond("rise", x, owner="t", prof=P.wait)


WAITS = ("the leader follows the exit's bus COND, which its owner makes from the block's successor on its "
         "own path; before it that path may not reach a timed step that waits for the leader (not its "
         "own, nor on a lane it clocks): the leader isn't there, and neither moves")


def test_a_followed_exit_cond_needs_its_owner_to_reach_it_without_the_leader():
    """R3-lead: t's exit follows c's STOP; c runs S, the ACK bit, on its own, then t's COND on x,
    which waits for t, before any STOP of its own. On the model c waits for t and t for c."""
    d = t_leads_then(ack_then(t_cond_on_x))
    assert launch_diags(d) == [WAITS]
    t = run_sys(lower(d).image, max_ticks=20_000)
    assert not any(e.halted for e in t.engines) and not any(e.events for e in t.engines)


def test_a_followed_exit_cond_whose_owner_halts_first_leaves_the_leader_waiting():
    """c halts after S, in its own block: t waits in its exit for a STOP that never comes, as a
    target waits for a controller that is done (or, at abort_pc, for its next START). Nobody waits
    for t, so R3-lead accepts it. (S is a NACK from ONES, so t holds SDA nowhere.)"""
    def nack_and_halt(P):
        P.d.xfer(1, P.L, owner="t", prof=P.bit, src="ones", dst="r4", abort=True)
        with P.d.only("c"):
            P.d.halt()
    d = t_leads_then(nack_and_halt)
    assert launch_diags(d) == []
    t = run_sys(compile_desc(d), max_ticks=20_000)
    assert [e.halted for e in t.engines] == [True, False, False] and t.quiescent


def test_a_followed_exit_cond_whose_owner_runs_its_own_steps_first_is_fine():
    """From S, c runs a byte on its clock (t's, so with t absent it reads 0xFF) and its own SCL and
    SDA steps up to its STOP: none waits for t."""
    def more(P):
        P.d.xfer(8, P.L, owner="t", prof=P.bit, src="ones", dst="r6", msb_first=True)
    assert launch_diags(t_leads_then(ack_then(more))) == []


GOES_ON = ("the leader follows the exit's bus COND and goes on at abort_pc: its owner, after making it on "
           "its path from the block's successor, must go on there too (nothing timed first), or halt")


def stop_then_new_frame(P):
    """c's STOP, then a new frame that isn't abort_pc's: START and a 4-bit byte of its own."""
    stop_seq(P)
    P.d.cond("start", owner="c", prof=P.wait)
    P.d.xfer(4, P.L, owner="c", prof=P.bit, src="ones", msb_first=True)


def test_a_followed_exit_conds_owner_goes_on_where_the_leader_does():
    """R3-lead: t's exit follows c's STOP and goes on at top; c goes on after it with a frame of
    its own, so t takes c's START there as top's and reads c's 4 bits and its STOP sequence as
    the 8-bit address."""
    d = t_leads_then(ack_then(stop_then_new_frame))
    assert launch_diags(d) == [GOES_ON]
    t = run_sys(lower(d).image, max_ticks=20_000)
    top = lower(d).image.abort_pc
    assert not t.engines[1].halted and t.engines[1].records[-1].pc == top   # in top's byte, for good
    assert not any(e.events for e in t.engines)


def repeated_start(P):
    P.d.cond("fall", P.scl, owner="c", prof=P.wait)
    P.d.cond("rise", P.scl, owner="c", prof=P.wait)
    P.d.cond("start", owner="c", prof=P.wait)
    P.d.xfer(8, P.L, owner="c", prof=P.bit, src="ones", msb_first=True)


def test_a_followed_stop_exit_waits_through_its_owners_repeated_start():
    """t's exit follows c's STOP, and c's next is a repeated START and a byte: t, without ABORT,
    waits through them for the STOP (isa §4.2) and goes on at top as c halts. It sits that frame
    out, which MON takes: nothing waits for t, and nobody reads wrong."""
    d = t_leads_then(ack_then(repeated_start))
    assert launch_diags(d) == []
    t = run_sys(compile_desc(d), max_ticks=20_000)
    assert [k for _, k in t.engines[1].bus_events] == ["START", "START", "STOP"]
    assert xfer_values(t, 1) == [ADDR] and xfer_values(t, 2)[-1] == 0xFF
    assert [e.halted for e in t.engines] == [True, False, True] and not any(e.events for e in t.engines)


def test_with_abort_the_followed_stop_leaves_at_the_start():
    """With ABORT on t's followed STOP, c's repeated START is t's bus exit to top, but c goes on
    with its byte there."""
    def target(P):
        with P.d.lead("t"):
            P.d.br("ack", cond="z", reg="r5")
            P.d.cond("stop", owner="c", prof=P.wait, abort=True)
            P.d.br("top")
    d = i2c(target=target, ack=ack_then(repeated_start), pre=lambda P: P.d.li("r5", 1))
    assert launch_diags(d) == [GOES_ON]


def test_the_owner_of_a_followed_exit_cond_skips_its_ack_branch_when_the_leader_is_away():
    """The library's pairing (i2c_reg): t's exit leaves S, its ACK from ZERO into r4, unsent, so c
    reads 1 there and takes its own NACK exit (a STOP, then abort_pc); the branch where c reads 0
    and runs on into a repeated START isn't c's while t is away. Into r3 (or from ONES), c might
    read 0 and make that START first."""
    def c_nack_block(reg):
        def more(P):
            d = P.d
            with d.lead("c"):
                d.br("go", cond="z", reg=reg)
                stop_seq(P)
                d.br("top")
            d.label("go")
            d.xfer(8, P.L, owner="c", prof=P.bit, src="ones", abort=True, msb_first=True)
            repeated = lambda P: (P.d.cond("fall", P.scl, owner="c", prof=P.wait),  # noqa: E731
                                  P.d.cond("rise", P.scl, owner="c", prof=P.wait),
                                  P.d.cond("start", owner="c", prof=P.wait))
            repeated(P)
            stop_seq(P)
        return more
    assert launch_diags(t_leads_then(ack_then(c_nack_block("r4")))) == []
    assert launch_diags(t_leads_then(ack_then(c_nack_block("r3")))) == [GOES_ON]


# ---------------------------------------------------------------- review of the exit rules
def nack_pairing(ack=ack_bit, before=None, t_body=None, c_first=None, nack_tail=None, go=None, reg="r4",
                 nack_head=None):
    """The library's pairing, varied. t leads (exit on r5, cleared, follows c's STOP); S is `ack`
    (t's ACK bit from ZERO into r4); then c leads: `c_first` before its BR on r4, which goes to
    `go` (default: a byte of c's and its STOP sequence) on 0; on a NACK `nack_head`, its STOP
    sequence and `nack_tail` (default: none), then top. `before` runs before t's block, `t_body` in it."""
    def target(P):
        if before:
            before(P)
        with P.d.lead("t"):
            P.d.br("ack", cond="z", reg="r5")
            P.d.li("r5", 0)
            if t_body:
                t_body(P)
            P.d.cond("stop", owner="c", prof=P.wait)
            P.d.br("top")

    def after(P):
        d = P.d
        with d.lead("c"):
            if c_first:
                c_first(P)
            d.br("go", cond="z", reg=reg)
            if nack_head:
                nack_head(P)
            stop_seq(P)
            if nack_tail:
                nack_tail(P)
            d.br("top")
        d.label("go")
        (go or (lambda P: d.xfer(8, P.L, owner="c", prof=P.bit, src="ones", abort=True, msb_first=True)))(P)
        stop_and_halt(P)
    return i2c(target=target, ack=ack, after=after, pre=lambda P: P.d.li("r5", 1))


def go_waits_for_t(P):
    P.d.xfer(8, P.L, owner="c", prof=P.bit, src="ones", abort=True, msb_first=True)
    t_cond_on_x(P)


def test_the_nack_pairing_compiles_and_runs():
    """Round 1: t leaves (r5), c reads 1 and takes its NACK exit; round 2: t ACKs, c sends."""
    d = nack_pairing()
    assert launch_diags(d) == []
    t = run(d)
    assert xfer_values(t, 0) == [ADDR, 1, ADDR, 0, 0xFF] and all(e.halted for e in t.engines)


def test_the_ack_branch_is_walked_where_the_owner_may_hold_the_line_itself():
    """c pulls SDA low before t's block, and still holds it in S: it reads 0 with t away, takes
    `go` and waits there in t's COND on x while t waits for the STOP (found by review)."""
    def c_holds_sda(P):
        P.d.cond("fall", P.scl, owner="c", prof=P.wait)
        P.d.hold(P.sda, 0, owner="c", prof=P.hold)
        P.d.cond("rise", P.scl, owner="c", prof=P.wait)
    d = nack_pairing(before=c_holds_sda, go=go_waits_for_t)
    assert launch_diags(d) == [WAITS]
    t = run_sys(lower(d).image, max_ticks=20_000)
    assert not any(e.halted for e in t.engines) and not any(e.events for e in t.engines)
    assert launch_diags(nack_pairing(go=go_waits_for_t)) == []      # c reads 1: `go` isn't c's then


def test_the_ack_branch_is_walked_where_the_leader_may_pull_the_line_low_in_its_block():
    """t's block sends a bit from ZERO on c's clock, which S's edges clock: c reads 0."""
    zero_bit = lambda P: P.d.xfer(1, P.L, owner="t", prof=P.bit, src="zero")  # noqa: E731
    assert WAITS in launch_diags(nack_pairing(t_body=zero_bit, go=go_waits_for_t))


def test_the_ack_branch_is_walked_where_r4_is_rewritten_first():
    assert launch_diags(nack_pairing(c_first=lambda P: P.d.li("r4", 0), go=go_waits_for_t)) == [WAITS]


def test_the_ack_may_be_read_into_r0():
    def ack_r0(P):
        P.d.xfer(1, P.L, owner="t", prof=P.bit, src="zero", dst="r0", abort=True)

    assert launch_diags(nack_pairing(ack=ack_r0, reg="r0", go=go_waits_for_t)) == []


EDGES = ("the leader's XFERs on another party's clock in this exit take that party's edges from the block's "
         "successor, which it clocks meanwhile: before the bus COND it follows they may take no more than S "
         "makes (2 per bit), or the leader is still in one when it comes")
ONES = ("the leader's XFER on another party's clock in this exit sends while that party runs the block's "
        "successor: on an OD line it sends from ONES, as a 0 would hold the line (on SDA, the bus COND would "
        "make no edge)")


@pytest.mark.parametrize("n,want", [(1, []), (8, [EDGES])])
def test_the_leaders_xfer_on_the_owners_clock_takes_no_more_edges_than_s(n, want):
    """t's in-block XFER takes c's edges from S (the 1-bit ACK, 2 edges): an 8-bit one is still
    running at c's STOP, and t completes it a frame later (found by review)."""
    body = lambda P: P.d.xfer(n, P.L, owner="t", prof=P.bit, src="ones", msb_first=True)  # noqa: E731
    d = nack_pairing(t_body=body)
    assert launch_diags(d) == want
    if not want:
        t = run(d)
        assert xfer_values(t, 0) == [ADDR, 1, ADDR, 0, 0xFF] and all(e.halted for e in t.engines)


def test_the_leaders_xfer_on_the_owners_clock_sends_from_ones():
    """A 0 from t's block on SDA during S: there, c reads it as the ACK (its bit holds SDA too)."""
    body = lambda P: P.d.xfer(1, P.L, owner="t", prof=P.bit, src="zero")  # noqa: E731
    assert ONES in launch_diags(nack_pairing(t_body=body))


def test_the_spi_leaders_byte_longer_than_s():
    """spi_lead: b's 8-bit XFER on a's clock, S only 4 bits: b is still in it at a's STOP."""
    assert EDGES in launch_diags(spi_lead(s_n=4))


def test_after_the_ack_branch_the_exit_path_is_still_checked():
    """c's NACK exit (past the BR on the ACK) runs a step after its STOP."""
    def exit_seq(P):
        stop_seq(P)
        P.d.xfer(1, P.L, owner="c", prof=P.bit, src="ones")
    assert "nothing timed between the exit's bus COND and its branch to abort_pc: the others have already left" \
        in launch_diags(c_ends_before_t_sends(reg="r4", exit_seq=exit_seq))


def test_an_unrelated_instruction_before_the_ack_branch():
    assert launch_diags(c_ends_before_t_sends(reg="r4", before_br=lambda P: P.d.li("r6", 1))) == []


@pytest.mark.parametrize("timed_after", [False, True])
def test_the_followed_conds_owner_goes_on_at_abort_pc_past_untimed_steps(timed_after):
    """c's NACK exit: its STOP, an LI, then top; or a timed step before top, which t's walk finds
    on c's NACK branch (past the BR on the ACK) as well as c's own check."""
    def tail(P):
        P.d.li("r6", 0)
        if timed_after:
            P.d.cond("fall", P.scl, owner="c", prof=P.wait)
    want = ["nothing timed between the exit's bus COND and its branch to abort_pc: the others have already left",
            GOES_ON] if timed_after else []
    assert sorted(launch_diags(nack_pairing(nack_tail=tail))) == sorted(want)
