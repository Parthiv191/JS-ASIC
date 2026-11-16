"""The I2C register read (emu/protocols/i2c_reg.py) in every role, at 2% drift.

A transaction: START, the address with W, the register number, a repeated START, the address with
R, then `data_bytes` bytes from the target, the controller ACKing each but the last, then STOP.
The target serves `values` (compiled in) and tells its host which register was asked for. On a
NACKed address the controller pushes NACK (0x100) and STOPs."""
import itertools
import random

import pytest

from emu.compiler import compile_desc
from emu.isa import Role
from emu.protocols import get, run_roles
from emu.protocols.i2c_reg import NACK, values_for

PERMS = list(itertools.permutations([Role.P0, Role.P1, Role.MON]))
BASE, SLOW = (2500, 10000), (2451, 10000)  # 1/4 of the tick rate, and 1.96% slower
SDA, SCL = 0, 1
SPEC = get("i2c_reg")


def params(**kw):
    return {**SPEC.defaults, **kw}


def data(n=2, seed=1, **kw):
    return SPEC.payload(random.Random(seed), n, **params(**kw))


def regs(p):
    return p["ctl"][1::3]                      # each transaction: address W, the register, address R


def want(role, p, **kw):
    vals = values_for(**params(**kw))
    if role == Role.P0:
        return vals * len(regs(p))
    if role == Role.P1:
        return regs(p)
    return [w for r in regs(p) for w in [r] + vals]


def assert_clean(t, perm, p, **kw):
    assert t.quiescent
    for i, role in enumerate(perm):
        et = t.engines[i]
        assert not et.events, f"engine {i} ({role.name}): {et.events}"
        assert et.rx == want(role, p, **kw), f"engine {i} ({role.name})"


@pytest.mark.parametrize("perm", PERMS, ids=lambda p: "-".join(r.name for r in p))
def test_i2c_reg_all_role_permutations(perm):
    p = data()
    assert_clean(run_roles(SPEC.build(), perm, p), perm, p)


@pytest.mark.parametrize("phase", range(4))
@pytest.mark.parametrize("ctl_fast", [True, False], ids=["ctl-fast", "ctl-slow"])
def test_i2c_reg_drift_within_tolerance(ctl_fast, phase):
    perm = (Role.P0, Role.P1, Role.MON)
    p = data(n=2, seed=phase, data_bytes=2)
    clocks = {0: BASE, 1: SLOW, 2: SLOW} if ctl_fast else {0: SLOW, 1: BASE, 2: BASE}
    t = run_roles(SPEC.build(data_bytes=2), perm, p, clocks=clocks, phases={1: phase, 2: 3 - phase})
    assert_clean(t, perm, p, data_bytes=2)


def shortest_half_period(ppm):
    for H in range(2, 120):
        try:
            compile_desc(SPEC.build(half_cycles=H, tolerance_ppm=ppm))
            return H
        except Exception:
            continue
    raise AssertionError("nothing compiles")


def test_i2c_reg_at_the_shortest_half_period_runs_clean_at_2pct():
    """The rule cross-check: the shortest H the compiler accepts runs clean at 2% drift."""
    H = shortest_half_period(20000)
    perm = (Role.P0, Role.P1, Role.MON)
    for clocks in ({0: BASE, 1: SLOW, 2: SLOW}, {0: SLOW, 1: BASE, 2: BASE}):
        p = data(n=2)
        t = run_roles(SPEC.build(half_cycles=H), perm, p, clocks=clocks)
        assert_clean(t, perm, p)


@pytest.mark.parametrize("which", ["write", "read"])
def test_a_nacked_address_ends_the_transaction_and_the_next_goes_through(which):
    """The target ACKs only its own address. If it NACKs the first address byte (the host asked for
    another device), the controller drops the rest of the transaction, pushes NACK and STOPs. A
    read address for another device is NACKed the same way, after the target has the register."""
    A, B = SPEC.defaults["address"], 0x11
    vals = values_for(**params())
    if which == "write":
        ctl = [B << 1, 0x05, B << 1 | 1, A << 1, 0x06, A << 1 | 1]
        tgt_rx = [0x06]
    else:
        ctl = [A << 1, 0x05, B << 1 | 1, A << 1, 0x06, A << 1 | 1]
        tgt_rx = [0x05, 0x06]
    perm = (Role.P0, Role.P1, Role.MON)
    t = run_roles(SPEC.build(), perm, {"ctl": ctl, "tgt": []})
    assert t.quiescent
    assert t.engines[0].rx == [NACK] + vals
    assert t.engines[1].rx == tgt_rx
    assert not t.engines[0].events and not t.engines[1].events


def test_a_repeated_start_and_the_last_byte_nacked_on_the_wire():
    """Between the register number and the read address SDA falls while SCL is high with no STOP
    before it (a repeated START); the controller NACKs the last byte (SDA high at its 9th SCL rise)."""
    p = data(n=1)
    t = run_roles(SPEC.build(), (Role.P0, Role.P1), p)
    sda, scl = t.wire_edges[SDA], t.wire_edges[SCL]
    level = lambda edges, tick: 1 ^ (sum(e <= tick for e in edges) & 1)   # both lines idle at 1
    starts = [e for e in sda if level(sda, e) == 0 and level(scl, e) == 1]
    stops = [e for e in sda if level(sda, e) == 1 and level(scl, e) == 1]
    assert len(starts) == 2 and len(stops) == 1 and starts[1] < stops[0]
    rises = scl[1::2]                                       # SCL idles high: odd edges rise
    bits = "".join(str(level(sda, r)) for r in rises)
    byte = lambda v: f"{v:08b}"
    A, reg = SPEC.defaults["address"], p["ctl"][1]
    # address W, ACK; the register, ACK; SCL rising with SDA high for the repeated START; address
    # R, ACK; the value, NACK; SCL rising with SDA low for the STOP
    assert bits == (byte(A << 1) + "0" + byte(reg) + "0" + "1" + byte(A << 1 | 1) + "0"
                    + byte(values_for(**params())[0]) + "1" + "0")


def test_every_i2c_reg_space_point_compiles():
    keys = sorted(SPEC.space)
    for values in itertools.product(*(SPEC.space[k] for k in keys)):
        compile_desc(SPEC.build(**dict(zip(keys, values))))


# ---- the review of the register read: what a real device on the other end needs
A = SPEC.defaults["address"]
LD = SPEC.defaults["ldelay"]
HOSTS = {   # one transaction then a good one: a NACK at the write address, at the read address
    "ack": [A << 1, 0x05, A << 1 | 1, A << 1, 0x06, A << 1 | 1],
    "nack_w": [0x11 << 1, 0x05, 0x11 << 1 | 1, A << 1, 0x06, A << 1 | 1],
    "nack_r": [A << 1, 0x05, 0x11 << 1 | 1, A << 1, 0x06, A << 1 | 1],
}


def level(edges, tick):
    return 1 ^ (sum(e <= tick for e in edges) & 1)        # both lines idle at 1


@pytest.mark.parametrize("case", HOSTS)
def test_sda_moves_only_ldelay_after_scl_falls(case):
    """I2C's data hold: with SCL low, SDA moves at least `ldelay` (300 ns by default) after SCL fell,
    on every path, the STOP's SDA fall included (it came 2 cycles after SCL fell)."""
    t = run_roles(SPEC.build(), (Role.P0, Role.P1), {"ctl": HOSTS[case], "tgt": []})
    sda, scl = t.wire_edges[SDA], t.wire_edges[SCL]
    early = [(e, e - max(f for f in scl if f <= e)) for e in sda if level(scl, e) == 0]
    assert [x for x in early if x[1] < LD] == []


@pytest.mark.parametrize("case", HOSTS)
def test_every_role_on_a_nack_and_the_next_transaction(case):
    """The controller pushes NACK for the failed transaction; the target has the register numbers
    it got (both, on a NACKed read address); MON pushes what it saw, its failed transaction marked
    by a BUS event."""
    vals = values_for(**params())
    perm = (Role.P0, Role.P1, Role.MON)
    t = run_roles(SPEC.build(), perm, {"ctl": HOSTS[case], "tgt": []})
    assert t.quiescent
    want = {
        "ack": ([*vals, *vals], [0x05, 0x06], [0x05, *vals, 0x06, *vals], set()),
        "nack_w": ([NACK, *vals], [0x06], [0x06, *vals], {"BUS"}),
        "nack_r": ([NACK, *vals], [0x05, 0x06], [0x05, 0x06, *vals], {"BUS"}),
    }[case]
    assert [t.engines[i].rx for i in range(3)] == list(want[:3])
    assert not t.engines[0].events and not t.engines[1].events
    assert {e.kind for e in t.engines[2].events} == want[3]


def test_a_controller_reading_fewer_bytes_than_the_target_serves():
    """An MCU reading one byte from a target that serves two: the target sees the NACK and waits for
    the STOP instead of sending its second byte (0x25: its first bit, 0, would hold SDA low through
    the STOP), so the next transaction goes through."""
    from emu.model.system import EngineSpec, System
    ctl = compile_desc(SPEC.build(data_bytes=1))
    tgt = compile_desc(SPEC.build(data_bytes=2, values=(0x68, 0x25)))
    s = System([EngineSpec(ctl, Role.P0), EngineSpec(tgt, Role.P1)])
    s.host_push(0, HOSTS["ack"])
    t = s.run(500_000)
    assert t.quiescent and t.engines[0].rx == [0x68, 0x68] and t.engines[1].rx == [0x05, 0x06]
    assert not t.engines[0].events and not t.engines[1].events


def scl_fall(build, k):
    """The tick of SCL's k-th fall in a clean run."""
    t = run_roles(build, (Role.P0, Role.P1), {"ctl": HOSTS["ack"][:3], "tgt": []})
    return t.wire_edges[SCL][0::2][k]


@pytest.mark.parametrize("stretch", [True, False])
def test_a_target_that_stretches_scl(stretch):
    """A real target may hold SCL low (clock stretching): here for 200 cycles, in the data byte's
    third bit. With `stretch` (the default) the controller waits for SCL to rise; without it, it
    clocks on and reads a wrong byte, with no event."""
    from emu.model.system import EngineSpec, Glitch, System
    b = SPEC.build(stretch=stretch)
    at = scl_fall(b, 31) + 1                       # 9 + 9 + 1 + 9 falls, then 3 bits in
    s = System([EngineSpec(compile_desc(b), r) for r in (Role.P0, Role.P1)], glitches=[Glitch(SCL, at, 200, 0)])
    s.host_push(0, HOSTS["ack"][:3])
    t = s.run(500_000)
    assert (t.engines[0].rx == values_for(**params())) == stretch


def test_a_glitch_on_the_idle_bus_costs_no_transaction():
    """As for the write: an SDA low pulse on the idle bus is a START and a STOP no controller made;
    the address XFER bus-exits on the flags and the target re-syncs on the real START."""
    from emu.model.system import EngineSpec, Glitch, System
    img = compile_desc(SPEC.build())
    s = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)], glitches=[Glitch(SDA, 500, 30, 0)])
    s.host_push(0, HOSTS["ack"], at_tick=2000)
    t = s.run(500_000)
    vals = values_for(**params())
    assert t.engines[0].rx == vals * 2 and t.engines[1].rx == [0x05, 0x06]


def test_a_late_host_one_word_at_a_time():
    """The register and the read address come from TX inside XFERs the controller clocks, so a POP
    stall there holds SCL low, which I2C allows; the target follows it."""
    from emu.model.system import EngineSpec, System
    img = compile_desc(SPEC.build())
    s = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)])
    for k, w in enumerate(HOSTS["ack"]):
        s.host_push(0, [w], at_tick=1000 + 1500 * k)
    t = s.run(500_000)
    vals = values_for(**params())
    assert t.engines[0].rx == vals * 2 and t.engines[1].rx == [0x05, 0x06]
    assert all(not et.events for et in t.engines)
