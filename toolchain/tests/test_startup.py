"""T5-startup (isa §6): at RUN an owned idle-0 line reads its pull-up's 1, and every engine sees that
1 at now = 2 and idle from now = 3 (isa §2). Instruction 0 starts at now = 2, so a step there that
watches an idle-0 line takes the start-up 1 as a rise. Found by review of the random programs: the
compiler accepted both shapes below with every rule on."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.engine import VIOLATIONS
from emu.model.system import EngineSpec, System


def clocked(pad):
    """b clocks (cpol 0, an idle-0 clock); a sends 0b0110 from its FIFO."""
    d = Description("s", parties=("a", "b"))
    data = d.line("d", owner="a", idle=1)
    clk = d.line("c", owner="b", idle=0)
    lane = d.lane("L", data, clk=clk, cpol=0, cpha=0)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    if pad:
        d.nop()
    d.xfer(4, lane, owner="a", prof=half, src="fifo", dst="r1")
    d.halt()
    return d


def rise(pad):
    """a's COND RISE on its idle-0 line, then a HOLD."""
    d = Description("r", parties=("a", "b"))
    x = d.line("x", owner="a", idle=0)
    w = d.profile("W", nom=40, min=30, max=INF)
    if pad:
        d.nop()
    d.cond("rise", x, owner="a", prof=w)
    d.hold(x, 0, owner="a", prof=d.profile("B", nom=20, min=18, max=22))
    d.halt()
    return d


def run(img, words=(0b0110,)):
    s = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)])
    s.host_push(0, list(words))
    return s.run(50_000)


def xfer_values(t):
    return [[r.value for r in et.records if r.op == "XFER"] for et in t.engines]


@pytest.mark.parametrize("build", [clocked, rise])
def test_a_step_at_pc_0_watching_an_idle_0_line_is_rejected(build):
    with pytest.raises(CompileError) as e:
        compile_desc(build(pad=False))
    assert "T5-startup" in {x.rule for x in e.value.diags}


def test_the_rejected_clocked_xfer_really_misreads():
    """What the rule prevents: the clock followers count the start-up 1 as a leading edge."""
    t = run(compile_desc(clocked(pad=False), check_timing=False))
    assert xfer_values(t)[0] == [0b0110] and xfer_values(t)[1] != [0b0110]
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []  # silently


def test_the_rejected_rise_really_anchors_early():
    t = run(compile_desc(rise(pad=False), check_timing=False))
    assert "EARLY" in [e.kind for e in t.engines[1].events]


@pytest.mark.parametrize("build", [clocked, rise])
def test_one_untimed_instruction_first_makes_it_legal(build):
    t = run(compile_desc(build(pad=True)))
    assert len({tuple(v) for v in xfer_values(t)}) == 1
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []


def test_an_idle_1_line_at_pc_0_is_fine():
    d = Description("f", parties=("a", "b"))
    x = d.line("x", owner="a", idle=1)
    d.cond("fall", x, owner="a", prof=d.profile("W", nom=40, min=0, max=INF))
    d.hold(x, 1, owner="a", prof=d.profile("B", nom=20, min=18, max=22))
    d.halt()
    compile_desc(d)
