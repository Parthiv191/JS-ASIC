"""The SPI library protocol (emu/protocols/spi.py) in every role, every mode, and at 2% drift.

The controller's host pushes a frame token, then the frame's `words` bytes. A frame: the
controller takes the token (CS high), CS falls (a SELECT START), `words` duplex bytes (MOSI from
the controller's TX FIFO, MISO from the peripheral's), CS rises (STOP). Every engine pushes
(MISO << 8) | MOSI per byte."""
import itertools
import random

import pytest

from emu.compiler import CompileError, compile_desc
from emu.isa import Role
from emu.model.system import Glitch
from emu.protocols import get, run_roles

PERMS = list(itertools.permutations([Role.P0, Role.P1, Role.MON]))
BASE, SLOW = (2500, 10000), (2451, 10000)  # 1/4 of the tick rate, and 1.96% slower


def data(frames=3, seed=1, **params):
    return get("spi").payload(random.Random(seed), frames, **{**get("spi").defaults, **params})


def ctl_bytes(p, k=None):
    k = k or get("spi").defaults["words"]
    return [w for i, w in enumerate(p["ctl"]) if i % (k + 1)]   # every frame starts with a token


def words(p, k=None):
    return [(m << 8) | c for c, m in zip(ctl_bytes(p, k), p["per"])]


def assert_clean(t, perm, p, k=None):
    want = words(p, k)
    for i, role in enumerate(perm):
        et = t.engines[i]
        assert not et.events, f"engine {i} ({role.name}): {et.events}"
        assert [r.value for r in et.records if r.op == "XFER"] == want, f"engine {i} ({role.name})"
        assert et.rx == want, f"engine {i} ({role.name})"


@pytest.mark.parametrize("perm", PERMS, ids=lambda p: "-".join(r.name for r in p))
def test_spi_all_role_permutations(perm):
    p = data()
    t = run_roles(get("spi").build(), perm, p)
    assert t.quiescent
    assert_clean(t, perm, p)


@pytest.mark.parametrize("cpol,cpha", [(0, 0), (0, 1), (1, 0), (1, 1)])
@pytest.mark.parametrize("ctl_fast", [True, False], ids=["ctl-fast", "ctl-slow"])
def test_spi_every_mode_at_2pct(cpol, cpha, ctl_fast):
    perm = (Role.P0, Role.P1, Role.MON)
    p = data(frames=2, words=2, cpol=cpol, cpha=cpha)
    clocks = {0: BASE, 1: SLOW, 2: SLOW} if ctl_fast else {0: SLOW, 1: BASE, 2: BASE}
    t = run_roles(get("spi").build(words=2, cpol=cpol, cpha=cpha), perm, p, clocks=clocks,
                  phases={1: 1, 2: 3})
    assert_clean(t, perm, p, k=2)


def shortest_half_period(ppm, **kw):
    for H in range(2, 64):
        try:
            compile_desc(get("spi").build(half_cycles=H, tolerance_ppm=ppm, **kw))
            return H
        except CompileError:
            pass
    raise AssertionError("no half period compiles")


@pytest.mark.parametrize("cpha", [0, 1])
def test_spi_at_the_shortest_half_period_runs_clean_at_2pct(cpha):
    """The rule cross-check (verification.md): the shortest H the compiler accepts at 2% runs
    clean with the peripheral 1.96% slow and out of phase."""
    H = shortest_half_period(20000, cpha=cpha)
    perm = (Role.P0, Role.P1, Role.MON)
    p = data(frames=2, cpha=cpha)
    t = run_roles(get("spi").build(half_cycles=H, cpha=cpha), perm, p, clocks={0: BASE, 1: SLOW, 2: SLOW},
                  phases={1: 2, 2: 1})
    assert_clean(t, perm, p)


def test_an_empty_peripheral_sends_ones():
    """The peripheral's TX FIFO runs dry: it sends ONES (0xFF on MISO) and logs UNDERRUN."""
    p = data(frames=2)
    p["per"] = p["per"][:1]
    t = run_roles(get("spi").build(), (Role.P0, Role.P1), p)
    c = ctl_bytes(p)
    assert t.engines[0].rx == [(p["per"][0] << 8) | c[0], (0xFF << 8) | c[1]]
    assert [e.kind for e in t.engines[1].events] == ["UNDERRUN"]


def test_cs_rising_mid_frame_ends_it_and_the_next_frame_is_clean():
    """A glitch lifts CS mid-byte: the peripheral sees STOP, its XFER is a bus exit, MISO is
    released (gated), and it re-syncs on the next START; the controller doesn't notice."""
    p = data(frames=2)
    t0 = run_roles(get("spi").build(), (Role.P0, Role.P1), p)
    first = next(r for r in t0.engines[1].records if r.op == "XFER")
    stop = next(r for r in t0.engines[0].records if r.op == "COND" and r.cycle > first.cycle)
    mid = first.tb - 8 * get("spi").defaults["half_cycles"]          # mid-frame 1
    # CS held high from mid-byte until the controller's own STOP drives it high
    t = run_roles(get("spi").build(), (Role.P0, Role.P1), p, glitches=[Glitch(0, mid, stop.cycle - mid + 2, 1)])
    assert [e.kind for e in t.engines[1].events] == ["BUS"]
    # frame 1's partial byte is dropped (its TX word too); frame 2 pairs the next words
    assert t.engines[1].rx == [(p["per"][1] << 8) | ctl_bytes(p)[1]]
    assert not t.engines[0].events


def test_every_spi_space_point_compiles():
    spec = get("spi")
    keys = sorted(spec.space)
    for values in itertools.product(*(spec.space[k] for k in keys)):
        compile_desc(spec.build(**dict(zip(keys, values))))


def test_the_wire_carries_msb_first_and_releases_miso_after_cs_rises():
    """Against a real device the bit order shows: MOSI 0x7F idles high, so its MSB (0) is the
    first change, one launch after CS falls. MISO (gated) is driven only during the frame and
    reads the pull-up (1) once CS is high again."""
    p = {"ctl": [0, 0x7F], "per": [0x00]}
    t = run_roles(get("spi").build(), (Role.P0, Role.P1), p)
    H = get("spi").defaults["half_cycles"]
    cs, sck, mosi, miso = t.wire_edges[:4]
    assert len(mosi) == 2 and mosi[1] - mosi[0] == 2 * H     # low for exactly one bit: the MSB
    assert sck[0] == 1 and sck[1] > cs[0]                    # the start-up drive to idle 0, then the clock
    assert miso[0] > cs[0]                                    # MISO driven only after CS fell
    assert len(miso) == 2 and miso[1] > cs[1]                 # released after CS rose
