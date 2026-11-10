from emu.compiler import compile_desc
from emu.desc import Description
from emu.isa import Role
from emu.model.system import EngineSpec, Glitch, System



def test_edge_latency_between_engines():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.raw_delay(3)   # starts at 4 (ROLE_IF at 2), takes 4 (3 rounded up to U)
        d.raw_set(ln, 0)  # now 8
    with d.only("b"):
        d.nop()
        d.label("wait")            # inside the block, so the poll is one check per U
        d.raw_jpin(ln, 1, "wait")
        d.mark(1)
    d.halt()
    image = compile_desc(d, check_timing=False)
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P1)]).run(100)
    marks = [e for e in trace.engines[1].events if e.kind == "MARK"]
    # the low is on the wire from 9 and seen from 9 + SYNC_LAT = 11 = 8 + EDGE_LAT, so the first
    # JPIN to see it is the one at 12 (own cycles are even); MARK follows at 14
    assert [m.cycle for m in marks] == [14]
    assert trace.wire_edges[0] == [9]  # driven in tick 8, visible on the wire from tick 9


def test_idle_0_line_reads_the_pin_until_its_owner_drives_it():
    """isa §2: the owner's idle drive is made in s0 and is on the wire from tick 1, so tick 0
    reads the pull-up. Every engine of the same RUN, the owner too, sees that 1 at now = 2 and
    the idle level from now = 3."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=0)
    d.raw_get("r0", ln)  # now 2
    d.raw_get("r1", ln)  # now 4
    d.halt()
    image = compile_desc(d, check_timing=False)
    s = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P1)])
    trace = s.run(100)
    assert trace.wire_edges[0] == [1]  # 1 at tick 0, the owner's 0 from tick 1
    assert [e.regs[:2] for e in s.engines] == [[1, 0], [1, 0]]


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
    assert trace.wire_edges[0] == [53]  # pop at 50 (an own cycle), drive at 52, visible at 53


def test_glitch_overrides_wire():
    image = raw_image(lambda d, ln: d.raw_delay(100))
    trace = System([EngineSpec(image, Role.P1)], glitches=[Glitch(0, 10, 3, 0)]).run(40)
    assert trace.wire_edges[0] == [10, 13]


def test_contention_reported():
    image = raw_image(lambda d, ln: d.raw_delay(100))
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P0)]).run(5)
    assert trace.contention and trace.contention[0] == (1, 0)  # the s0 drives land at tick 1


def test_quiescence_waits_for_an_edge_in_flight():
    """A follower waiting on an unbounded COND counts as idle. The run must still not stop
    while the owner's last edge is crossing the follower's synchronizer."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")  # owner: COND at cycle 0, ROLE_IF at 1, HALT at 2
    with d.only("b"):
        d.mark(1)
    d.halt()
    image = compile_desc(d)
    trace = System([EngineSpec(image, Role.P0), EngineSpec(image, Role.P1)]).run(10_000)
    assert trace.quiescent
    assert [e.kind for e in trace.engines[1].events] == ["MARK"]


def test_sub_cycle_phase_follower_sees_owner_edge_at_cycle_0():
    """A follower whose clock is a fraction of a cycle behind sees the owner's edge a cycle sooner.
    The owner's COND acts at its now = 2 (tick 8), on the wire from tick 9; the follower samples it
    at its now = 2 (tick 9) and detects it at 4, so e = 4 - EDGE_LAT = 1, one cycle before the
    owner's tb = 2: the QUANT the windows allow."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a")
    with d.only("b"):
        d.mark(1)
    d.halt()
    image = compile_desc(d)
    trace = System([EngineSpec(image, Role.P0, clock=(1, 4)),
                    EngineSpec(image, Role.P1, clock=(1, 4), phase=1)]).run(1000)
    assert [e.kind for e in trace.engines[1].events] == ["MARK"]
    (rec,) = trace.engines[1].records
    assert (rec.cycle, rec.tb) == (4, 1)
    assert trace.engines[0].records[0].tb == 2


def _pop_then_halt():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.pop("r0")
    d.halt()
    return compile_desc(d, check_timing=False)


def test_pop_stall_forever_is_quiescent():
    """A POP that never gets data waits on the host: the run must still end as quiescent, so the
    idle flag holds across the odd cycles POP skips (isa §4.0 stalls). Two engines one clk apart,
    as on chip, so that on every tick one of them is in a skipped cycle."""
    img = _pop_then_halt()
    trace = System([EngineSpec(img, Role.P0), EngineSpec(img, Role.P1, phase=1)]).run(1000)
    assert trace.quiescent and trace.ticks < 100
    assert [et.starts for et in trace.engines] == [[(0, 2)], [(0, 2)]]


def test_phase_1_engine_is_shifted_one_tick():
    """Engine 1 runs one clk behind engine 0 (isa §4.0). Parity is per-engine `now`, so its trace in
    engine time is identical and its cycles land one tick later."""
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.li("r0", 1)
    d.raw_delay(3)
    d.halt()
    image = compile_desc(d, check_timing=False)
    t0 = System([EngineSpec(image, Role.P0, phase=0)]).run(100, stop_when_quiescent=False)
    t1 = System([EngineSpec(image, Role.P0, phase=1)]).run(100, stop_when_quiescent=False)
    assert t0.engines[0].starts == t1.engines[0].starts == [(0, 2), (1, 4), (2, 8)]
    assert t1.engines[0].cycle_tick[:9] == [c + 1 for c in t0.engines[0].cycle_tick[:9]]
