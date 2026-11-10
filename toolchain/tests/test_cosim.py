"""Co-simulation's model side (test/cosim/README.md): pulses, the comparator and the replay.

A comparator that can't fail is worse than none, so each kind of difference is planted and
must be reported at the right index."""
from dataclasses import replace

import pytest

from emu.compiler import compile_desc
from emu.cosim import Kind, Pulse, compare, expected, replay
from emu.desc import Description
from emu.isa import Role
from emu.model.engine import Event
from emu.model.system import EngineSpec, Glitch, System


def P(now, kind, pc, *fields):
    return Pulse(now, kind, pc, tuple(fields))


ISSUE, STEP, EVT, MARK, BR = Kind.ISSUE, Kind.STEP, Kind.EVT, Kind.MARK, Kind.BR


def test_compare_equal_is_none():
    run = [P(2, ISSUE, 0), P(2, STEP, 0, 2, 1, 0, 3), P(4, ISSUE, 1)]
    assert compare(run, list(run)) is None


def test_compare_reports_a_one_cycle_shift():
    m = compare([P(2, ISSUE, 0), P(4, ISSUE, 1)], [P(2, ISSUE, 0), P(5, ISSUE, 1)])
    assert m.index == 1 and m.expected.now == 4 and m.observed.now == 5


def test_compare_reports_a_changed_field():
    m = compare([P(9, STEP, 3, 3, 0, 0xA5, 40)], [P(9, STEP, 3, 3, 0, 0xA4, 40)])
    assert m.index == 0 and m.expected.fields[2] == 0xA5 and m.observed.fields[2] == 0xA4


def test_compare_reports_a_dropped_pulse():
    m = compare([P(2, ISSUE, 0), P(4, ISSUE, 1)], [P(2, ISSUE, 0)])
    assert m.index == 1 and m.expected == P(4, ISSUE, 1) and m.observed is None


def test_compare_reports_an_extra_pulse():
    m = compare([P(2, ISSUE, 0)], [P(2, ISSUE, 0), P(4, ISSUE, 1)])
    assert m.index == 1 and m.expected is None and m.observed == P(4, ISSUE, 1)


def test_compare_ignores_order_within_a_cycle():
    """The trace port pulses in parallel, so signal order inside one cycle means nothing."""
    assert compare([P(6, EVT, 1, 0), P(6, BR, 1, 1)], [P(6, BR, 1, 1), P(6, EVT, 1, 0)]) is None


def test_compare_context_is_the_last_5_matched():
    same = [P(2 * i, ISSUE, i) for i in range(8)]
    m = compare(same + [P(16, ISSUE, 8)], same + [P(17, ISSUE, 8)])
    assert m.index == 8 and m.matched == same[3:]
    assert "expected" in str(m) and "observed" in str(m)


def _one_engine(build):
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    build(d, ln)
    img = compile_desc(d, check_timing=False)
    return System([EngineSpec(img, Role.P0)]).run(100)


def test_expected_from_hold_and_mark():
    """HOLD at pc 0 starts at 2 and ends at tb = 3 (isa §4.0, §4.2); MARK at 4; HALT at 6."""
    def b(d, ln):
        d.hold(ln, 0, owner="a", prof=d.profile("A", nom=3))
        d.mark(7)
        d.halt()
    assert expected(_one_engine(b), 0) == [
        P(2, ISSUE, 0), P(2, STEP, 0, 2, 1, 0, 3), P(4, ISSUE, 1), P(4, MARK, 1, 7),
        P(6, ISSUE, 2), P(6, Kind.HALT, 2)]


def test_expected_rejects_an_unknown_event():
    t = _one_engine(lambda d, ln: d.halt())
    t.engines[0].events.append(Event("NOT_A_KIND", 0, 2))
    with pytest.raises(ValueError, match="NOT_A_KIND"):
        expected(t, 0)


# ---------------------------------------------------------------- replay and stimulus
def _holds():
    """P0 owns PP line l (idle 1) and holds it 0 then 1, nom 4 each; P1 follows."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    p = d.profile("H", nom=4)
    d.hold(ln, 0, owner="a", prof=p)
    d.hold(ln, 1, owner="a", prof=p)
    d.halt()
    return compile_desc(d, check_timing=False)


def test_stimulus_is_the_peer_wire():
    """The peer P0 drives 0 at now = 2 (on the wire from 3) and 1 at 4 (from 5): isa §4.0, §4.2."""
    img = _holds()
    rep = replay([EngineSpec(img, Role.P1), EngineSpec(img, Role.P0)], 0)
    assert rep.stimulus[:7] == [0xFF] * 3 + [0xFE] * 2 + [0xFF] * 2
    assert rep.expected == expected(rep.trace, 0) and rep.engine == 0 and rep.image is img


def test_stimulus_excludes_the_rtl_engine():
    img = _holds()
    rep = replay([EngineSpec(img, Role.P0), EngineSpec(img, Role.P1)], 0)
    assert set(rep.stimulus) == {0xFF} and len(rep.stimulus) == rep.trace.ticks


def test_stimulus_maps_lines_to_pins():
    img = _holds()
    img = replace(img, lines=[replace(img.lines[0], pin=5)] + img.lines[1:])
    rep = replay([EngineSpec(img, Role.P1), EngineSpec(img, Role.P0)], 0)
    assert rep.stimulus[:7] == [0xFF] * 3 + [0xDF] * 2 + [0xFF] * 2


def test_stimulus_applies_glitches():
    img = _holds()
    rep = replay([EngineSpec(img, Role.P1), EngineSpec(img, Role.P0)], 0, glitches=[Glitch(0, 20, 2, 0)])
    assert rep.stimulus[19:23] == [0xFF, 0xFE, 0xFE, 0xFF]


def test_replay_rejects_a_glitch_over_the_rtl_drive():
    """The chip reads its own drive inside, so a glitch over it would reach only the model."""
    img = _holds()
    with pytest.raises(ValueError, match="glitch"):
        replay([EngineSpec(img, Role.P0), EngineSpec(img, Role.P1)], 0, glitches=[Glitch(0, 20, 2, 0)])


@pytest.mark.parametrize("clock,phase", [((49, 50), 0), ((1, 1), 1)])
def test_replay_rejects_a_slow_or_late_rtl_engine(clock, phase):
    img = _holds()
    with pytest.raises(ValueError, match="clock"):
        replay([EngineSpec(img, Role.P1, clock=clock, phase=phase), EngineSpec(img, Role.P0)], 0)


def test_replay_rejects_host_words_for_the_rtl_engine():
    img = _holds()
    with pytest.raises(ValueError, match="FIFO"):
        replay([EngineSpec(img, Role.P1), EngineSpec(img, Role.P0)], 0, tx={0: [1]})


def test_replay_rejects_two_images():
    with pytest.raises(ValueError, match="image"):
        replay([EngineSpec(_holds(), Role.P1), EngineSpec(replace(_holds(), name="other"), Role.P0)], 0)


def test_trace_keeps_the_final_registers():
    """The trace port has no register writes, so the harness compares r0-r7 after the run."""
    def b(d, ln):
        d.li("r3", 0x1234)
        d.alu("add", "r4", "r3", 1)
        d.halt()
    assert _one_engine(b).engines[0].regs == [0, 0, 0, 0x1234, 0x1235, 0, 0, 0]
