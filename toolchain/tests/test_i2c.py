"""The I2C library protocol (emu/protocols/i2c.py): a controller write in every role, at 2% drift.

A transaction: START, the address byte (the target ACKs only its own address, in a lead block),
then `data_bytes` bytes each ACKed by the target, then STOP. On a NACK the controller STOPs and
starts over; a target that isn't addressed waits for the STOP and restarts."""
import itertools
import random

import pytest

from emu.compiler import CompileError, compile_desc
from emu.isa import Role
from emu.protocols import get, run_roles

PERMS = list(itertools.permutations([Role.P0, Role.P1, Role.MON]))
BASE, SLOW = (2500, 10000), (2451, 10000)  # 1/4 of the tick rate, and 1.96% slower
SDA, SCL = 0, 1


def data(n=2, seed=1, **params):
    return get("i2c").payload(random.Random(seed), n, **{**get("i2c").defaults, **params})


def sent(p, k=None):
    k = k or get("i2c").defaults["data_bytes"]
    return [w for i, w in enumerate(p["ctl"]) if i % (k + 1)]   # each transaction: address, then k bytes


def assert_clean(t, perm, p, k=None):
    want = sent(p, k)
    for i, role in enumerate(perm):
        et = t.engines[i]
        assert not et.events, f"engine {i} ({role.name}): {et.events}"
        # the target and the monitor receive the bytes; the controller drives SDA, the lane's only
        # data line, so it pushes nothing (isa §4.3)
        assert et.rx == ([] if role == Role.P0 else want), f"engine {i} ({role.name})"


@pytest.mark.parametrize("perm", PERMS, ids=lambda p: "-".join(r.name for r in p))
def test_i2c_all_role_permutations(perm):
    p = data()
    t = run_roles(get("i2c").build(), perm, p)
    assert t.quiescent
    assert_clean(t, perm, p)


@pytest.mark.parametrize("phase", range(4))
@pytest.mark.parametrize("ctl_fast", [True, False], ids=["ctl-fast", "ctl-slow"])
def test_i2c_drift_within_tolerance(ctl_fast, phase):
    perm = (Role.P0, Role.P1, Role.MON)
    p = data(n=2, seed=phase, data_bytes=2)
    clocks = {0: BASE, 1: SLOW, 2: SLOW} if ctl_fast else {0: SLOW, 1: BASE, 2: BASE}
    t = run_roles(get("i2c").build(data_bytes=2), perm, p, clocks=clocks, phases={1: phase, 2: 3 - phase})
    assert_clean(t, perm, p, k=2)


def shortest_half_period(ppm):
    for H in range(2, 80):
        try:
            compile_desc(get("i2c").build(half_cycles=H, tolerance_ppm=ppm))
            return H
        except CompileError:
            pass
    raise AssertionError("no half period compiles")


def test_i2c_at_the_shortest_half_period_runs_clean_at_2pct():
    """The rule cross-check (verification.md): the shortest H the compiler accepts at 2% runs
    clean with the target 1.96% slow and out of phase."""
    H = shortest_half_period(20000)
    perm = (Role.P0, Role.P1, Role.MON)
    p = data(n=2)
    t = run_roles(get("i2c").build(half_cycles=H), perm, p, clocks={0: BASE, 1: SLOW, 2: SLOW}, phases={1: 2, 2: 1})
    assert_clean(t, perm, p)


def test_a_wrong_address_is_nacked_and_the_next_transaction_goes_through():
    """The first transaction names another address: the target doesn't ACK, the controller
    reads NACK and STOPs (its data byte is never sent), the target waits for that STOP; the second
    transaction, to the target's address, is received."""
    A = get("i2c").defaults["address"]
    p = {"ctl": [(A ^ 1) << 1, (A << 1), 0x3C], "tgt": []}
    t = run_roles(get("i2c").build(), (Role.P0, Role.P1, Role.MON), p)
    for i in range(3):
        assert t.engines[i].rx == ([] if i == 0 else [0x3C]), i
    assert not t.engines[0].events and not t.engines[1].events
    # the monitor skips both lead blocks: it follows the first data byte speculatively, and the
    # controller's STOP after the NACK makes that a bus exit (a normal event, isa §4.7)
    assert [e.kind for e in t.engines[2].events] == ["BUS"]
    assert [k for _, k in t.engines[1].bus_events] == ["START", "STOP", "START", "STOP"]


def test_start_and_stop_on_the_wire():
    """START: SDA falls while SCL is high; STOP: SDA rises while SCL is high; the address goes
    MSB first (0x5A << 1 = 0xB4: SDA high for the first bit)."""
    t = run_roles(get("i2c").build(), (Role.P0, Role.P1), data(n=1))
    sda, scl = t.wire_edges[SDA], t.wire_edges[SCL]
    assert sda[0] < scl[0]                        # SDA falls first, SCL still high: START
    assert sda[-1] > scl[-1]                      # SDA rises last, SCL already high: STOP
    assert sda[1] > scl[0]                        # SDA moves only after SCL fell (ldelay)
    # the address on the wire, sampled at each SCL rise: 0x5A << 1 = 0xB4, MSB first
    level = lambda edges, tick: 1 ^ (sum(e <= tick for e in edges) & 1)   # both lines idle at 1
    bits = [level(sda, scl[2 * k + 1]) for k in range(8)]
    assert bits == [1, 0, 1, 1, 0, 1, 0, 0]


def test_every_i2c_space_point_compiles():
    spec = get("i2c")
    keys = sorted(spec.space)
    for values in itertools.product(*(spec.space[k] for k in keys)):
        compile_desc(spec.build(**dict(zip(keys, values))))
