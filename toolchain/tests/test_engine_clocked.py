"""Clocked XFER on one data line (isa §4.1, §4.3): the clock owner's edges, launch and capture in
both clock phases, every role, CLK_PHASE, TIMEOUT, UNDERRUN and the FIFO wait.

Times are worked out by hand from isa §4.3. The program is `LI r0` (now 2-3), then the XFER,
which starts at now = 4 with tb = 0. A drive in cycle t is on the wire from t + 1 and seen at
t + EDGE_LAT (isa §1)."""
from dataclasses import replace

import pytest

from emu.analysis import _Step, completion
from emu.compiler import CompileError, compile_desc
from emu.image import Profile
from emu.desc import Description
from emu.isa import EDGE_LAT, INF, QUANT, Op, Owner, Role
from emu.model.system import Glitch

from simutil import kinds, run_sys

H = 4
N = 8
VALUE = 0xB4  # 1011_0100: MSB first, the wire shows every bit
START = 4     # the XFER's start cycle


def spi(cpol=0, cpha=0, data_owner="a", src="r0", dst="r1", n=N, msb=True, check=False, abort=False,
        lo=H - 1, hi=INF, head=None, h=H, timing=False, ppm=20000):
    """P0 (party a) owns the clock; the data line belongs to `data_owner`."""
    d = Description("spi", parties=("a", "b"), tolerance_ppm=ppm)
    sck = d.line("sck", owner="a", idle=cpol)
    dat = d.line("d", owner=data_owner, idle=1)
    lane = d.lane("L", dat, clk=sck, cpol=cpol, cpha=cpha)
    half = d.profile("H", nom=h, min=lo, max=hi)
    d.abort_to("end")
    d.li("r0", VALUE)
    if head:
        head(d)
    d.xfer(n, lane, owner=data_owner, prof=half, src=src, dst=dst, msb_first=msb, check=check, abort=abort)
    d.label("end")
    d.halt()
    return compile_desc(d, check_timing=timing)


def bits(v=VALUE, n=N):
    return [v >> (n - 1 - k) & 1 for k in range(n)]  # MSB first


def drive_cycles(wire_edges):
    """A drive in cycle t is on the wire from t + 1. The start-up idle drive (s0, on the wire at
    tick 1: an idle-0 clock pulls the line down from the pull-up's 1) is left out."""
    return [t - 1 for t in wire_edges if t > 1]


def got(t, i):
    """Engine i's XFER value: dst, the wire value in every role (isa §4.3); None if it timed out."""
    (rec,) = [r for r in t.engines[i].records if r.op == "XFER"]
    return rec.value


def clock_drives(tb, cpha, n=N):
    """isa §4.3: leading edge k at tb + (2k+1)H, trailing at tb + (2k+2)H."""
    return [tb + j * H for j in range(1, 2 * n + 1)]


def tb_of(cpha):
    """The clock owner's tb at the start: max(tb, start) for cpha 0, max(tb, start - H) for 1."""
    return max(0, START) if cpha == 0 else max(0, START - H)


@pytest.mark.parametrize("cpol", [0, 1])
@pytest.mark.parametrize("cpha", [0, 1])
def test_clock_owner_edges_and_data(cpol, cpha):
    """The clock owner (also the data owner) makes 2n edges from its re-based tb; the data
    launches at bit 0 then on the launching edges; every role gets the value; tb ends at
    tb + 2nH in every role."""
    t = run_sys(spi(cpol, cpha))
    tb = tb_of(cpha)
    sck, dat = t.wire_edges
    assert drive_cycles(sck) == clock_drives(tb, cpha)
    # launch: cpha 0 bit 0 at tb, bit k on trailing edge k-1; cpha 1 bit k on leading edge k
    launch = [tb + 2 * k * H for k in range(N)] if cpha == 0 else [tb + (2 * k + 1) * H for k in range(N)]
    level, want = 1, []  # the data line idles at 1
    for at, b in zip(launch, bits()):
        if b != level:
            want.append(at)
            level = b
    assert drive_cycles(dat) == want
    for i in range(3):
        et = t.engines[i]
        assert not et.events, (i, et.events)
        (rec,) = [r for r in et.records if r.op == "XFER"]
        assert rec.tb == tb + 2 * N * H and rec.value == VALUE


@pytest.mark.parametrize("cpha", [0, 1])
def test_data_from_the_clock_follower(cpha):
    """P1 owns the data (MISO): it launches on the launching edges it detects, and P0, the clock
    owner, samples at each capture edge + EDGE_LAT."""
    t = run_sys(spi(cpha=cpha, data_owner="b"))
    tb = tb_of(cpha)
    for i in range(3):
        assert not t.engines[i].events and got(t, i) == VALUE, i
    lead = [tb + (2 * k + 1) * H for k in range(N)]
    trail = [tb + (2 * k + 2) * H for k in range(N)]
    capture = lead if cpha == 0 else trail
    # P0 samples at capture + EDGE_LAT; the follower P1 drives, so it adds no samples
    assert [s.cycle for s in t.engines[0].samples] == [c + EDGE_LAT for c in capture]
    assert not t.engines[1].samples
    # MON samples in the cycle it detects each capture edge
    assert [s.cycle for s in t.engines[2].samples] == [c + EDGE_LAT for c in capture]
    # P1 launches when it detects the launching edge: cpha 0 bit 0 at its start
    launch = ([START] + [x + EDGE_LAT for x in trail[:-1]]) if cpha == 0 else [x + EDGE_LAT for x in lead]
    level, want = 1, []
    for at, b in zip(launch, bits()):
        if b != level:
            want.append(at)
            level = b
    assert drive_cycles(t.wire_edges[1]) == want


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_completion_matches_the_compiler(cpha, data_owner):
    """Each role completes where analysis.completion() says, relative to the step's end tb, so
    the compiler's L and the model agree (the rule cross-check, verification.md)."""
    t = run_sys(spi(cpha=cpha, data_owner=data_owner))
    owner = Owner.P0 if data_owner == "a" else Owner.P1
    step = _Step(1, Op.XFER, owner, Profile("H", H - 1, H, INF), n=N, clocked=True, clk_owner=Role.P0, cpha=cpha)
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = [x for x in t.engines[i].records if x.op == "XFER"]
        assert rec.cycle - rec.tb == completion(step, r)[0], r


def test_lsb_first():
    t = run_sys(spi(msb=False))
    assert all(got(t, i) == VALUE for i in range(3))
    assert t.wire_edges[1] != run_sys(spi(msb=True)).wire_edges[1]


def test_late_clock_owner_rebases_and_never_overruns():
    """isa §4.1: a clock owner that starts late re-bases tb to its start (cpha 0) instead of
    logging OVERRUN, and the followers follow its edges."""
    def head(d):
        with d.only("a"):
            d.raw_delay(20)
    t = run_sys(spi(head=head))
    start = START + 2 + 20  # ROLE_IF, then RAW DELAY 20
    assert drive_cycles(t.wire_edges[0]) == clock_drives(start, 0)
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))


@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_ldelay_delays_the_edge_launches(data_owner):
    """A launch triggered by an edge waits ldelay: the clock owner's at edge + ldelay, the clock
    follower's at max(detect, e + ldelay), e = detect - EDGE_LAT = the edge's drive cycle. cpha 0
    bit 0 has no ldelay. With ldelay 4 the clock follower's bit is still on the wire one cycle
    before P0's capture sees it."""
    img = spi(data_owner=data_owner)
    img = replace(img, lanes=[replace(img.lanes[0], ldelay=4)])
    t = run_sys(img)
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))
    tb = tb_of(0)
    trail = [tb + (2 * k + 2) * H for k in range(N)]
    launch = [START] + [x + 4 for x in trail[:-1]]
    level, want = 1, []
    for at, b in zip(launch, bits()):
        if b != level:
            want.append(at)
            level = b
    assert drive_cycles(t.wire_edges[1]) == want


def test_clock_follower_clk_phase():
    """CHK: a phase shorter than min - QUANT logs CLK_PHASE; the first edge isn't checked. The
    clock owner runs at half the period, so all 2n - 1 later phases are short."""
    img = spi(check=True, lo=H, hi=H + 2)
    fast = img.with_profiles({"H": 0.5})
    t = run_sys(img, images={0: fast})
    assert kinds(t, 1).count("CLK_PHASE") == 2 * N - 1 and kinds(t, 2).count("CLK_PHASE") == 2 * N - 1
    assert "CLK_PHASE" not in kinds(t, 0)  # the clock owner doesn't check its own clock


def test_clock_follower_first_phase_is_not_checked():
    """P1 starts 1 cycle before it detects the first edge. That phase isn't checked (it has no
    start edge), so CHK logs nothing. P0 starts at 6 (after ROLE_IF), so its first leading edge
    is driven at 10 and seen at 13; P1 starts at 6 + 6."""
    def head(d):
        with d.only("b"):
            d.raw_delay(6)
    t = run_sys(spi(check=True, head=head))
    assert [s for s in t.engines[1].starts if s[0] == 3] == [(3, 12)]  # the XFER, pc 3
    assert not t.engines[1].events and got(t, 1) == VALUE


def test_clock_follower_clk_phase_with_abort():
    img = spi(check=True, abort=True, lo=H, hi=H + 2)
    t = run_sys(img, images={0: img.with_profiles({"H": 0.5})})
    assert kinds(t, 1)[:2] == ["CLK_PHASE", "ABORT"]
    assert not [r for r in t.engines[1].records if r.op == "XFER"]  # aborted: no dst, no record


def test_clock_follower_timeout_from_the_step_start():
    """No clock: a bounded follower times out when no edge has come for max + QUANT cycles since
    its start. Without ABORT: to := 1, tb := now, no dst, and the step ends."""
    t = run_sys(spi(hi=H + 2), roles=(Role.P1,))
    et = t.engines[0]
    (ev,) = et.events
    assert ev.kind == "TIMEOUT" and ev.cycle == START + (H + 2) + QUANT + 1
    (rec,) = [r for r in et.records if r.op == "XFER"]
    assert rec.tb == ev.cycle and rec.value is None and et.halted


def test_clock_follower_timeout_after_an_edge():
    """After an edge the count restarts from that edge's detection. The clock stops (a glitch
    holds it) after the first leading edge."""
    img = spi(hi=H + 2)
    lead0 = tb_of(0) + H          # drive cycle; seen at + EDGE_LAT
    t = run_sys(img, roles=(Role.P0, Role.P1), glitches=[Glitch(0, lead0 + 1 + H, 400, 1)])
    ev = next(e for e in t.engines[1].events if e.kind == "TIMEOUT")
    # the last edge P1 sees is the first leading edge (the trailing edge is held off)
    assert ev.cycle == lead0 + EDGE_LAT + (H + 2) + QUANT + 1


def test_clock_follower_timeout_with_abort():
    t = run_sys(spi(hi=H + 2, abort=True), roles=(Role.P1,))
    assert kinds(t, 0) == ["TIMEOUT", "ABORT"]


def test_underrun_sends_ones_and_keeps_the_fifo():
    """isa §4.3: a data owner that doesn't own the clock, with src = FIFO empty, sends ONES, logs
    UNDERRUN and consumes nothing."""
    t = run_sys(spi(data_owner="b", src="fifo"))
    assert kinds(t, 1) == ["UNDERRUN"] and got(t, 0) == 0xFF
    assert t.engines[1].tx_left == 0
    t2 = run_sys(spi(data_owner="b", src="fifo"), tx={1: [VALUE]})
    assert not t2.engines[1].events and got(t2, 0) == VALUE


def test_clock_owner_waits_for_fifo_data():
    """A clock owner with src = FIFO empty waits, checking every cycle, then starts in the cycle
    it takes the word (cpha 0: tb := that cycle). Nobody logs anything."""
    t = run_sys(spi(src="fifo"), tx={0: [VALUE]}, push_at=30)
    took = 30  # the host's word is in TX from tick 30, and P0 runs at phase 0
    assert drive_cycles(t.wire_edges[0]) == clock_drives(took, 0)
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))


def test_clk_idle_must_equal_cpol():
    """S-lane (isa §5): the engines tell a leading edge by the level the clock goes to, so a
    clock that idles at the other level would shift every bit."""
    d = Description("spi", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=1)
    lane = d.lane("L", d.line("d", owner="a"), clk=sck, cpol=0)
    d.xfer(8, lane, owner="a", prof=d.profile("H", nom=H))
    d.halt()
    with pytest.raises(CompileError, match="S-lane"):
        compile_desc(d, check_timing=False)


@pytest.mark.parametrize("what", ["data2", "stretch", "od"])
def test_part_2_features_fail_loudly(what):
    img = spi()
    lane = img.lanes[0]
    if what == "data2":
        img = replace(img, lanes=[replace(lane, data2=0)])
    elif what == "stretch":
        img = replace(img, lanes=[replace(lane, stretch=1)])
    else:
        img = replace(img, lines=[img.lines[0], replace(img.lines[1], kind="od")])
    with pytest.raises(NotImplementedError):
        run_sys(img)


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
@pytest.mark.parametrize("clock,phase", [((1, 1), 0), ((49, 50), 0), ((49, 50), 1), ((49, 50), 3)])
def test_accepted_spi_runs_clean_at_2pct(cpha, data_owner, clock, phase):
    """The rule cross-check (verification.md): a clocked XFER the compiler accepts at 2% (T3,
    T4, T5-watch) runs clean on the model with the peers 2% slow and out of phase."""
    img = spi(cpha=cpha, data_owner=data_owner, h=8, lo=7, timing=True)
    t = run_sys(img, clocks={1: clock, 2: clock}, phases={1: phase, 2: phase})
    for i in range(3):
        assert not t.engines[i].violations and got(t, i) == VALUE, (i, t.engines[i].events)


@pytest.mark.parametrize("h", [2, 4])
def test_t3_rejects_a_short_half_period(h):
    """T3: a data owner that isn't the clock owner needs H >= EDGE_LAT + 1 + ldelay + ceil(d*2H),
    so 5 at 2%. It launches when it sees the launching edge, EDGE_LAT after it, and the clock
    owner's capture sees the wire 1 cycle after the capture edge. So H >= EDGE_LAT is enough at
    equal clocks: H = 2 misreads, and H = 3 and 4 still read right at 2% (T3 keeps 1 cycle plus
    the drift as margin)."""
    with pytest.raises(CompileError, match="T3"):
        spi(data_owner="b", h=h, lo=h - 1, timing=True)
    t = run_sys(spi(data_owner="b", h=h, lo=h - 1))
    assert (got(t, 0) == VALUE) == (h >= EDGE_LAT)
