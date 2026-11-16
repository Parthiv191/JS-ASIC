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
