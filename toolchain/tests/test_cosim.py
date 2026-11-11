"""Co-simulation's model side (test/cosim/README.md): pulses, the comparator and the replay.

A comparator that can't fail is worse than none, so each kind of difference is planted and
must be reported at the right index."""
from dataclasses import replace

import pytest

from emu.compiler import compile_desc
from emu.cosim import Kind, Pulse, compare, compare_pins, expected, pins, replay
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


def test_replay_host_reads_rx_only_after_the_run():
    """The harness reads RX after the run, so the replay's model host must too: a peer that pushes
    9 words overflows (isa §4.9), as the chip would."""
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    with d.only("b"):
        for i in range(9):
            d.li("r0", i)
            d.push("r0")
    d.halt()
    img = compile_desc(d, check_timing=False)
    rep = replay([EngineSpec(img, Role.P0), EngineSpec(img, Role.P1)], 0)
    peer = rep.trace.engines[1]
    assert peer.rx == list(range(8)) and [e.kind for e in peer.events] == ["OVERFLOW"]


# ---------------------------------------------------------------- pins
def _holds_od():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", "od", owner="a", idle=1)
    p = d.profile("H", nom=4)
    d.hold(ln, 0, owner="a", prof=p)
    d.hold(ln, 1, owner="a", prof=p)
    d.halt()
    return compile_desc(d, check_timing=False)


def test_trace_keeps_the_drive_after_the_run():
    """drives has one entry per tick plus the drive after the run, which holds from then on."""
    t = System([EngineSpec(_holds(), Role.P0)]).run(100)
    assert len(t.engines[0].drives) == t.ticks + 1 and t.engines[0].drives[-1] == (1,)


def test_pins_follow_the_drives():
    """PP line l on pin 0: released before RUN and in s0, idle 1 from now = 1 (isa §2), HOLD 0 on
    the wire from 3, HOLD 1 from 5 (isa §4.2), still driven after HALT and after the run."""
    t = System([EngineSpec(_holds(), Role.P0)]).run(100)
    assert pins(t, 0, range(-1, 7)) == [(0, 0), (0, 0), (1, 1), (1, 1), (1, 0), (1, 0), (1, 1), (1, 1)]
    assert pins(t, 0, range(t.ticks - 1, t.ticks + 3)) == [(1, 1)] * 4


def test_pins_od_releases_its_1s():
    """An OD line is driven only to 0; its idle 1 and its 1s are released (isa §4.3)."""
    t = System([EngineSpec(_holds_od(), Role.P0)]).run(100)
    assert pins(t, 0, range(0, 7)) == [(0, 0), (0, 0), (0, 0), (1, 0), (1, 0), (0, 0), (0, 0)]


def test_pins_map_lines_to_pins():
    img = _holds()
    img = replace(img, lines=[replace(img.lines[0], pin=5)] + img.lines[1:])
    t = System([EngineSpec(img, Role.P0)]).run(100)
    assert pins(t, 0, range(3, 6)) == [(0x20, 0), (0x20, 0), (0x20, 0x20)]


def test_pins_of_a_follower_are_released():
    img = _holds()
    t = System([EngineSpec(img, Role.P0), EngineSpec(img, Role.P1)]).run(100)
    assert set(pins(t, 1, range(-2, t.ticks + 2))) == {(0, 0)}


def test_compare_pins_equal_is_none():
    run = [(0, 0), (1, 1), (1, 0)]
    assert compare_pins(run, list(run), first=-1) is None


def test_compare_pins_reports_the_first_tick_and_pin():
    m = compare_pins([(0, 0), (0x09, 0x08), (0x09, 0)], [(0, 0), (0x01, 0), (0x08, 0)], first=10)
    assert (m.tick, m.pins, m.expected, m.observed) == (11, [3], (0x09, 0x08), (0x01, 0))
    assert "pin 3: expected driven 1, observed released" in str(m)


def test_compare_pins_reports_a_wrong_level():
    m = compare_pins([(0x04, 0)], [(0x04, 0x04)], first=0)
    assert m.pins == [2] and "pin 2: expected driven 0, observed driven 1" in str(m)


def test_compare_pins_ignores_out_on_released_pins():
    """uio_out of a pin with uio_oe = 0 never reaches the wire."""
    assert compare_pins([(0x01, 0x01)], [(0x01, 0xFF)], first=0) is None
