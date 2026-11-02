from emu.compiler import compile_desc
from emu.desc import Description
from emu.isa import EDGE_LAT, Role
from emu.model.system import EngineSpec, Glitch, System



def test_edge_latency_between_engines():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.raw_delay(3)   # cycles 1-3 (ROLE_IF is cycle 0)
        d.raw_set(ln, 0)  # cycle 4
    with d.only("b"):
        d.nop()
        d.label("wait")            # inside the block, so the poll is one cycle per check
        d.raw_jpin(ln, 1, "wait")
        d.mark(1)
    d.halt()
    image = compile_desc(d, check_timing=False)
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P1)]).run(100)
    marks = [e for e in trace.engines[1].events if e.kind == "MARK"]
    # the JPIN that first sees the low runs at cycle 4 + EDGE_LAT, and MARK follows it
    assert [m.cycle for m in marks] == [4 + EDGE_LAT + 1]
    assert trace.wire_edges[0] == [5]  # driven in tick 4, visible on the wire from tick 5


def test_clock_enable_ratio():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a")
    d.raw_delay(4000)
    image = compile_desc(d, check_timing=False)
    trace = System([EngineSpec(image, Role.P0, clock=(49, 50), phase=3)]).run(53, stop_when_quiescent=False)
    ticks = trace.engines[0].cycle_tick
    assert len(ticks) == 49 and ticks[0] == 3


def raw_image(build):
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    build(d, ln)
    return compile_desc(d, check_timing=False)


def watcher(d, ln):
    """b and mon MARK if they ever see the line low."""
    with d.only("b", "mon"):
        d.label("wait")
        d.raw_jpin(ln, 1, "wait")
        d.mark(1)
    d.halt()


def test_phase_offsets_no_spurious_edges():
    image = raw_image(watcher)
    roles = [Role.P0, Role.P1, Role.MON, Role.P1, Role.MON, Role.P1, Role.MON, Role.P1]
    trace = System([EngineSpec(image, r, phase=i) for i, r in enumerate(roles)]).run(200)
    assert all(not et.events for et in trace.engines)
    assert trace.wire_edges == [[]] and trace.contention == []


def test_quiescence_stops_run():
    image = raw_image(lambda d, ln: (d.pop("r0"), d.halt()))
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P1)]).run(10_000)
    assert trace.quiescent and trace.ticks < 5


def test_not_quiescent_while_host_push_pending():
    image = raw_image(lambda d, ln: (d.pop("r0"), d.halt()))
    sys_ = System([EngineSpec(image, Role.P1)])
    sys_.host_push(0, [7], at_tick=30)
    trace = sys_.run(10_000)
    assert trace.quiescent and trace.engines[0].halted and trace.ticks > 30


def test_host_push_is_delivered_at_tick():
    def b(d, ln):
        with d.only("a"):
            d.pop("r0")
            d.raw_set(ln, 0)
        d.halt()
    sys_ = System([EngineSpec(raw_image(b), Role.P0)])
    sys_.host_push(0, [0x41], at_tick=50)
    trace = sys_.run(10_000)
    assert trace.wire_edges[0] == [52]  # pop in tick 50, drive in tick 51, visible at 52


def test_glitch_overrides_wire():
    image = raw_image(lambda d, ln: d.raw_delay(100))
    trace = System([EngineSpec(image, Role.P1)], glitches=[Glitch(0, 10, 3, 0)]).run(40)
    assert trace.wire_edges[0] == [10, 13]


def test_contention_reported():
    image = raw_image(lambda d, ln: d.raw_delay(100))
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P0)]).run(5)
    assert trace.contention and trace.contention[0] == (0, 0)
