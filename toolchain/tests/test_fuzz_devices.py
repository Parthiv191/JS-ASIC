"""Devices under fuzz on the engine model (bus-fuzzer.md §6 stage 1)."""
import random

import pytest

from emu.fuzz.devices import DEVICES, Target
from emu.isa import PROG_DEPTH

TRIGGERS = {
    "unlock": bytes([0xA5, 0x3C, 0x7E, 0x00]),
    "leak": bytes([0x00, 0xA5, 0x3C, 0x7E]),
    "hang": bytes([0x5A, 0xC3, 0x00, 0x00]),
    "magic": bytes([0x4B, 0x1D, 0x00, 0x00]),
}


@pytest.fixture(scope="module")
def targets():
    return {name: Target(f()) for name, f in DEVICES.items()}


def messages(rng, n):
    """Random messages biased to the devices' magic bytes."""
    pool = [0x00, 0xA5, 0x3C, 0x7E, 0x5A, 0xC3, 0x4B, 0x1D]
    return [bytes(rng.choice(pool + [rng.randrange(256)]) for _ in range(4)) for _ in range(n)]


@pytest.mark.parametrize("name", DEVICES)
def test_device_compiles_within_prog_depth(targets, name):
    assert len(targets[name].image.words) <= PROG_DEPTH


@pytest.mark.parametrize("name", DEVICES)
def test_planted_behaviour_reachable_by_hand(targets, name):
    t = targets[name]
    zero = t.simulate(bytes(4))
    hit = t.simulate(TRIGGERS[name])
    assert t.dev.reached(TRIGGERS[name]) and not t.dev.reached(bytes(4))
    assert not zero.hang and not zero.events
    assert hit != zero
    assert hit.hang == (name == "hang")
    if name == "hang":
        assert hit.events == ("TIMEOUT", "ABORT")


def test_progress_is_visible_in_data_or_only_in_timing(targets):
    """unlock shows its state in the reply bytes; leak only in RDY latency; magic not at all."""
    part = bytes([0xA5, 0x3C, 0x00, 0x00])
    lat = lambda o: [t for t in o.tokens if t & 0xF0 == 0x40]  # noqa: E731
    dat = lambda o: [t for t in o.tokens if t & 0xF0 != 0x40]  # noqa: E731
    u0, u1 = targets["unlock"].simulate(bytes(4)), targets["unlock"].simulate(part)
    assert dat(u0) != dat(u1) and lat(u0) == lat(u1)
    l0, l1 = targets["leak"].simulate(bytes(4)), targets["leak"].simulate(part)
    assert dat(l0) == dat(l1) and lat(l0) != lat(l1)
    m = targets["magic"]
    assert m.simulate(bytes(4)) == m.simulate(bytes([0x4B, 0x00, 0x00, 0x00]))


@pytest.mark.parametrize("name", DEVICES)
def test_cached_observation_equals_a_fresh_run(name):
    """The per-path cache is exact: a fresh model run gives the same observation."""
    t = Target(DEVICES[name]())
    for m in messages(random.Random(name), 25):
        cached = t.observe(m)
        assert t.simulate(m) == cached, m.hex()


@pytest.mark.parametrize("name", DEVICES)
def test_fingerprint_stable_under_in_tolerance_jitter(targets, name):
    """Clock ratios inside tolerance_ppm (2%) and phase offsets give the same tokens."""
    t = targets[name]
    for m in messages(random.Random(7), 4) + [TRIGGERS[name]]:
        ref = t.simulate(m)
        for clocks, phases in ((((98, 100), (1, 1)), (0, 3)), (((1, 1), (98, 100)), (5, 0)),
                               (((99, 100), (1, 1)), (0, 7))):
            assert t.simulate(m, clocks, phases) == ref, (m.hex(), clocks, phases)
