"""Co-simulation's model side (test/cosim/README.md): pulses, the comparator and the replay.

A comparator that can't fail is worse than none, so each kind of difference is planted and
must be reported at the right index."""
import pytest

from emu.compiler import compile_desc
from emu.cosim import Kind, Pulse, compare, expected
from emu.desc import Description
from emu.isa import Role
from emu.model.engine import Event
from emu.model.system import EngineSpec, System


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
