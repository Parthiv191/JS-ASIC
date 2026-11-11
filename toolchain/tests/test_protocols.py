"""Protocol library: each description runs in every role assignment (plan Task 10).

UART half, pulled forward from Task 10; the SPI half follows Task 8.
"""
import itertools
import random

import pytest

from emu.compiler import CompileError, compile_desc
from emu.isa import PROG_DEPTH, Role
from emu.model.system import Glitch
from emu.protocols import PROTOCOLS, get, run_roles

PERMS = list(itertools.permutations([Role.P0, Role.P1, Role.MON]))
BASE, SLOW = (2500, 10000), (2451, 10000)  # 1/4 of the tick rate, and 1.96% slower (ratio 1.01999)


def uart_data(n=8, seed=1, **params):
    return get("uart").payload(random.Random(seed), n, **params)["tx"]


def assert_clean(trace, perm, data):
    """Every engine: no events, the same transfer values; followers received the data."""
    for i, role in enumerate(perm):
        et = trace.engines[i]
        assert not et.events, f"engine {i} ({role.name}): {et.events}"
        assert [r.value for r in et.records if r.op == "XFER"] == data, f"engine {i} ({role.name})"
        assert et.rx == ([] if role == Role.P0 else data), f"engine {i} ({role.name})"


@pytest.mark.parametrize("perm", PERMS, ids=lambda p: "-".join(r.name for r in p))
def test_uart_all_role_permutations(perm):
    data = uart_data()
    t = run_roles(get("uart").build(), perm, {"tx": data})
    assert t.quiescent
    assert_clean(t, perm, data)


@pytest.mark.parametrize("phase", range(4))
@pytest.mark.parametrize("tx_fast", [True, False], ids=["tx-fast", "tx-slow"])
def test_uart_drift_within_tolerance(tx_fast, phase):
    """Transmitter 2% faster or slower than both followers, at sub-cycle phase offsets."""
    perm = (Role.P0, Role.P1, Role.MON)
    clocks = {0: BASE, 1: SLOW, 2: SLOW} if tx_fast else {0: SLOW, 1: BASE, 2: BASE}
    data = uart_data(n=12, seed=phase)
    t = run_roles(get("uart").build(), perm, {"tx": data}, clocks=clocks, phases={1: phase, 2: phase})
    assert_clean(t, perm, data)


def shortest_bit(ppm):
    for P in range(2, 64):
        try:
            compile_desc(get("uart").build(bit_cycles=P, tolerance_ppm=ppm))
            return P
        except CompileError:
            pass
    raise AssertionError("no bit period compiles")


@pytest.mark.parametrize("perm", PERMS, ids=lambda p: "-".join(r.name for r in p))
@pytest.mark.parametrize("tx_fast", [True, False], ids=["tx-fast", "tx-slow"])
def test_uart_at_tolerance_edge_min_period(tx_fast, perm):
    """Rule cross-check: the shortest bit the compiler accepts at 2% is 19 cycles (2.1 Mbaud at
    40 MHz; T5-watch, test_analysis.test_t1_uart_period_boundary), and it runs clean with the full
    2% drift at every sub-cycle phase, in every role assignment."""
    P = shortest_bit(20000)
    assert P == 19
    tx = perm.index(Role.P0)
    fast, slow = (BASE, SLOW) if tx_fast else (SLOW, BASE)
    clocks = {i: fast if i == tx else slow for i in range(3)}
    for phase in range(4):
        data = uart_data(n=12, seed=phase)
        t = run_roles(get("uart").build(bit_cycles=P), perm, {"tx": data}, clocks=clocks,
                      phases={i: phase for i in range(3) if i != tx})
        assert_clean(t, perm, data)


ON_CHIP = [(Role.P0, Role.P1), (Role.P1, Role.P0), (Role.P0, Role.MON), (Role.MON, Role.P0)]


@pytest.mark.parametrize("perm", ON_CHIP, ids=lambda p: "-".join(r.name for r in p))
def test_uart_on_chip_engine_1_one_clk_behind(perm):
    """The chip's own arrangement (isa §4.0): both engines on clk, engine 1 one clk behind engine 0.
    The shortest accepted UART runs clean whichever engine transmits."""
    data = uart_data(n=6)
    t = run_roles(get("uart").build(bit_cycles=19), perm, {"tx": data}, phases={1: 1})
    assert t.quiescent
    assert_clean(t, perm, data)


@pytest.mark.parametrize("tx_fast", [True, False], ids=["tx-fast", "tx-slow"])
def test_uart_115200_at_40mhz_runs_clean_at_2pct(tx_fast):
    """Rule cross-check at the real operating point: P = 347 (115200 baud at 40 MHz) compiles and runs clean with the full
    2% drift."""
    perm = (Role.P0, Role.P1, Role.MON)
    clocks = {0: BASE, 1: SLOW, 2: SLOW} if tx_fast else {0: SLOW, 1: BASE, 2: BASE}
    data = uart_data(n=3)
    t = run_roles(get("uart").build(bit_cycles=347), perm, {"tx": data}, clocks=clocks,
                  phases={1: 1, 2: 2}, max_ticks=200_000)
    assert_clean(t, perm, data)


def test_uart_back_to_back_frames():
    """With data waiting, each start edge comes right at the end of the previous stop bit:
    one frame every 10 bit times, with the POP and the loop branch hidden inside the stop bit."""
    P = 20
    data = [0x00, 0xFF, 0x55, 0xAA] + uart_data(n=12)  # no data edges, all edges, and random
    perm = (Role.P0, Role.P1, Role.MON)
    t = run_roles(get("uart").build(bit_cycles=P), perm, {"tx": data})
    starts = [r.cycle for r in t.engines[0].records if r.op == "COND"]
    assert [b - a for a, b in zip(starts, starts[1:])] == [10 * P] * (len(data) - 1)
    assert_clean(t, perm, data)


def test_uart_framing_error_aborts_and_resyncs():
    """A low stop bit is a framing error: the followers log LEVEL, abort to the frame start and
    pick up the next start bit. The byte itself still arrives, as a UART reports it."""
    P = 20
    data = [0x11, 0x22, 0x33, 0x44]
    # frame k's start edge is on the wire at tick 7 + 10Pk (ROLE_IF at 2, POP at 4, then the COND
    # acts at 6, isa §4.0); its stop bit is slot 9. Pull the middle of frame 1's stop bit low.
    stop1 = 7 + 10 * P + 9 * P
    perm = (Role.P0, Role.P1, Role.MON)
    t = run_roles(get("uart").build(bit_cycles=P), perm, {"tx": data},
                  glitches=[Glitch(0, stop1 + 2, P - 4, 0)])
    assert not t.engines[0].events
    for i in (1, 2):
        assert [e.kind for e in t.engines[i].events] == ["LEVEL", "ABORT"]
        assert t.engines[i].rx == data


def test_every_uart_space_point_compiles():
    spec = get("uart")
    keys = sorted(spec.space)
    for values in itertools.product(*(spec.space[k] for k in keys)):
        compile_desc(spec.build(**dict(zip(keys, values))))


def test_description_size():
    """Gate G0 input: the instruction count of each description (printed with -s)."""
    for name, spec in sorted(PROTOCOLS.items()):
        n = len(compile_desc(spec.build(**spec.defaults)).words)
        print(f"{name}: {n} words")
        assert n <= PROG_DEPTH
    assert len(compile_desc(get("uart").build()).words) <= 8


def test_get_unknown_protocol_names_the_known_ones():
    with pytest.raises(KeyError, match="uart"):
        get("nope")
