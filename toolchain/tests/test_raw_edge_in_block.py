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
