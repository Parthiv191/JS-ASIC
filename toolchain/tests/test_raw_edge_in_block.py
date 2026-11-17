"""A RAW SET/REL is in a role block (R3-raw-drive), which MON and any party not in it skip, so
they already wait in the next timed step when it acts: an edge it makes that the step waits for
is taken as the step's own (R2-edge, isa §4.2, §4.3), even a pulse whose level is back before the
step's own edge. Found by the random I2C programs: the controller pulsed SCL low and up in its
block before its COND FALL on SCL, and the followers logged EARLY at the COND RISE after it."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.engine import VIOLATIONS
from emu.model.system import EngineSpec, System

from test_edge_rule import runs_clean

SAYS = "while the others already wait in the"


def diags(d):
    try:
        compile_desc(d)
    except CompileError as e:
        return [(x.rule, x.msg) for x in e.diags]
    return []


def run(d):
    img = compile_desc(d, check_levels=False, check_timing=False)
    return System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)


def cond_after(raws, edge):
    """a's push-pull line x (idle 1): `raws` in a's block, then a's COND `edge` on x and one back,
    each 40 with an EARLY window from 30."""
    d = Description("rc", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    w = d.profile("W", nom=40, min=30, max=INF)
    d.nop()
    with d.only("a"):
        for v in raws:
            d.raw_set(x, v)
    d.cond(edge, x, owner="a", prof=w)
    d.cond("rise" if edge == "fall" else "fall", x, owner="a", prof=w)
    d.halt()
    return d


def test_a_pulse_before_a_cond_on_its_line_is_rejected():
    """x low and up again, then a COND FALL: the others take the pulse's fall as the COND's edge,
    10 or so early."""
    d = cond_after((0, 1), "fall")
    assert any(r == "R2-edge" and f"RAW SET 0 on x by P0 may take it to 0 {SAYS} COND FALL" in m for r, m in diags(d))
    assert "EARLY" in [e.kind for et in run(d).engines for e in et.events if e.kind in VIOLATIONS]


def test_a_raw_edge_the_other_way_is_legal():
    """x low by a RAW, then a COND RISE: a follower waits for a rise and lets a fall go by."""
    d = cond_after((0,), "rise")
    assert not any(SAYS in m for _, m in diags(d))
    runs_clean(d)


def clocked_after(pulse):
    """a pulses its clock c (cpol 0) by RAWs, then sends 4 ONES on lane (s, c) to b."""
    d = Description("rx", parties=("a", "b"))
    s = d.line("s", owner="a", idle=0)
    c = d.line("c", owner="a", idle=0)
    lane = d.lane("L", s, clk=c, cpol=0, cpha=1)
    d.nop()
    with d.only("a"):
        if pulse:
            d.raw_set(c, 1)
            d.raw_set(c, 0)
        else:
            d.nop()
    d.xfer(4, lane, owner="a", prof=d.profile("HALF", nom=20, min=0, max=INF), src="ones")
    d.halt()
    return d


def test_a_pulse_on_a_clocked_xfers_clock_is_rejected():
    """The followers count the pulse's rise as the XFER's first leading edge."""
    assert any(r == "R2-edge" and f"RAW SET 1 on c by P0 may take it to 1 {SAYS} clocked XFER" in m
               for r, m in diags(clocked_after(True)))
    t = run(clocked_after(True))
    assert [r.value for r in t.engines[2].records if r.op == "XFER"] != [0xF]
    assert diags(clocked_after(False)) == []
    runs_clean(clocked_after(False))


# ---- the mutation run of this rule
def pulse_then(step, idle=1, spare_lines=2):
    """`spare_lines` lines first (so x is past index 1), then a's line x (idle `idle`): a pulse
    away from idle and back in a's block, then `step(d, x, w)`."""
    d = Description("rp", parties=("a", "b"))
    for i in range(spare_lines):
        d.line(f"s{i}", owner="a", idle=1)
    x = d.line("x", owner="a", idle=idle)
    w = d.profile("W", nom=40, min=30, max=INF)
    d.nop()
    with d.only("a"):
        d.raw_set(x, 1 - idle)
        d.raw_set(x, idle)
    step(d, x, w)
    d.halt()
    return d


def test_a_pulse_before_a_cond_on_a_later_line_is_rejected():
    d = pulse_then(lambda d, x, w: (d.cond("fall", x, owner="a", prof=w), d.cond("rise", x, owner="a", prof=w)))
    assert any(r == "R2-edge" and SAYS in m for r, m in diags(d))


def test_an_up_pulse_before_a_cond_rise_is_rejected():
    d = pulse_then(lambda d, x, w: (d.cond("rise", x, owner="a", prof=w), d.cond("fall", x, owner="a", prof=w)),
                   idle=0)
    assert any(r == "R2-edge" and f"RAW SET 1 on x by P0 may take it to 1 {SAYS} COND RISE" in m for r, m in diags(d))
    assert "EARLY" in [e.kind for et in run(d).engines for e in et.events if e.kind in VIOLATIONS]


def test_a_low_pulse_on_a_cpol_1_clock_is_rejected():
    """With cpol 1 the leading edge is a fall."""
    def step(d, x, w):
        s = d.line("s", owner="a", idle=1)
        d.xfer(4, d.lane("L", s, clk=x, cpol=1, cpha=1), owner="a", prof=d.profile("HALF", nom=20, min=0, max=INF),
               src="ones")
    d = pulse_then(step)
    assert any(r == "R2-edge" and f"RAW SET 0 on x by P0 may take it to 0 {SAYS} clocked XFER" in m for r, m in diags(d))


def test_a_pulse_on_line_0_before_a_cond_start_is_not_this_rule():
    """A COND START's line field is 0, but it waits on SDA: a pulse on line 0 is no edge it takes."""
    d = Description("r0", parties=("ctl", "tgt"))
    y = d.line("y", owner="ctl", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    d.nop()
    with d.only("ctl"):
        d.raw_set(y, 0)
        d.raw_set(y, 1)
    d.cond("start", owner="ctl", prof=d.profile("W", nom=40, min=0, max=INF))
    d.halt()
    assert not any(SAYS in m for _, m in diags(d))


def test_a_raw_in_the_last_block_is_an_s_end_error_not_a_crash():
    d = Description("re", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    d.cond("fall", x, owner="a", prof=d.profile("W", nom=40, min=0, max=INF))
    with d.only("a"):
        d.raw_set(x, 1)
    assert any(r == "S-end" for r, _ in diags(d))


def test_an_untimed_loop_after_a_raw_ends_the_walk():
    import threading
    d = Description("rl", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    d.nop()
    with d.only("a"):
        d.raw_set(x, 0)
    d.label("L")
    d.nop()
    d.br("L")
    done = []
    th = threading.Thread(target=lambda: done.append(diags(d)), daemon=True)
    th.start()
    th.join(30)
    assert done, "the walk to the next timed step didn't end"


# ---- the re-review
def ahead(nom):
    """a's pulse on x in its block, then b's own COND on y (`nom`), then a's COND FALL on x: b does
    its COND at once, without a, so b and MON may already wait in a's COND FALL on x."""
    d = Description("ah", parties=("a", "b"), tolerance_ppm=0)
    x = d.line("x", owner="a", idle=1)
    y = d.line("y", owner="b", idle=1)
    w = d.profile("W", nom=40, min=30, max=INF)
    d.nop()
    d.cond("fall", x, owner="a", prof=d.profile("W0", nom=40, min=0, max=INF))
    d.cond("rise", x, owner="a", prof=d.profile("W1", nom=40, min=0, max=INF))
    with d.only("a"):
        d.raw_delay(1)
        d.raw_set(x, 0)
        d.raw_set(x, 1)
    d.cond("fall", y, owner="b", prof=d.profile("S1", nom=nom, min=0, max=INF))
    d.cond("fall", x, owner="a", prof=w)
    d.cond("rise", x, owner="a", prof=w)
    d.halt()
    return d


def test_the_others_may_be_past_a_step_that_completes_without_the_doer():
    assert any(r == "R2-edge" and f"RAW SET 0 on x by P0 may take it to 0 {SAYS} COND FALL at pc 8" in m
               for r, m in diags(ahead(7)))
    assert "EARLY" in [e.kind for et in run(ahead(7)).engines for e in et.events if e.kind in VIOLATIONS]


def unclocked_check(pulse):
    """b's pulse on a's OD line x, then a's unclocked XFER on x with CHK: its followers check every
    edge of x from its start."""
    d = Description("ux", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=1)
    y = d.line("y", owner="a", idle=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.nop()
    d.cond("fall", y, owner="a", prof=w)
    d.hold(y, 1, owner="a", prof=d.profile("HB", nom=40, min=36, max=44))
    with d.only("b"):
        if pulse:
            d.raw_set(x, 0)
            d.raw_rel(x)
        else:
            d.nop()
    d.xfer(4, x, owner="a", prof=d.profile("B", nom=40, min=36, max=44), src="zero", check=True)
    d.halt()
    return d


def test_an_edge_on_an_unclocked_xfers_checked_line_is_rejected():
    assert any(r == "R2-edge" and f"RAW SET 0 on x by P1 may make an edge {SAYS} XFER with CHK" in m
               for r, m in diags(unclocked_check(True)))
    assert "EDGE" in [e.kind for et in run(unclocked_check(True)).engines for e in et.events if e.kind in VIOLATIONS]
    assert diags(unclocked_check(False)) == []


def test_a_raw_on_a_lead_exit_path_may_make_the_successors_edge():
    """The others wait in S, the block's successor, while the controller runs its exit path."""
    d = Description("lr", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    x = d.line("x", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    with d.only("ctl"):
        d.li("r5", 1)
    d.abort_to("retry")
    d.cond("start", owner="ctl", prof=w)
    with d.lead("ctl"):
        d.br("go", cond="z", reg="r5")
        d.raw_delay(60)
        d.raw_set(x, 0)
        d.raw_delay(6)
        d.raw_set(x, 1)
        d.cond("stop", owner="ctl", prof=w)
        d.br("retry")
    d.label("go")
    d.cond("fall", x, owner="ctl", prof=w, abort=True)
    d.cond("rise", x, owner="ctl", prof=w)
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    d.label("retry")
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=d.profile("H30", nom=30, min=0, max=INF), src="ones")
    d.xfer(1, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=w)
    d.halt()
    assert any(r == "R2-edge" and f"RAW SET 0 on x by P0 may take it to 0 {SAYS} COND FALL" in m for r, m in diags(d))
