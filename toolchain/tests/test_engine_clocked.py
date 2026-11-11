"""Clocked XFER on one data line (isa §4.1, §4.3): the clock owner's edges, launch and capture in
both clock phases, every role, CLK_PHASE, TIMEOUT, UNDERRUN and the FIFO wait.

Times are worked out by hand from isa §4.3. The program is `LI r0` (now 2-3), then the XFER,
which starts at now = 4 with tb = 0. A drive in cycle t is on the wire from t + 1 and seen at
t + EDGE_LAT (isa §1)."""
from dataclasses import replace

import pytest

from emu.analysis import _steps, completion
from emu.compiler import CompileError, compile_desc, lower
from emu.desc import Description
from emu.isa import EDGE_LAT, INF, QUANT, Role
from emu.model.system import EngineSpec, Glitch, System

from simutil import kinds, run_sys

H = 4
N = 8
VALUE = 0x4B  # 0100_1011: MSB first, bit 0 differs from the data line's idle 1, so every bit shows
START = 4     # the XFER's start cycle
XFER_PC = 1   # LI, then the XFER (when there is no head)


def spi_desc(cpol=0, cpha=0, data_owner="a", src="r0", dst="r1", n=N, msb=True, check=False,
             abort=False, lo=H - 1, hi=INF, head=None, h=H, ppm=20000):
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
    return d


def spi(timing=False, **kw):
    return compile_desc(spi_desc(**kw), check_timing=timing)


def bits(v=VALUE, n=N):
    return [v >> (n - 1 - k) & 1 for k in range(n)]  # MSB first


def drive_cycles(wire_edges):
    """A drive in cycle t is on the wire from t + 1. The start-up idle drive (s0, on the wire at
    tick 1: an idle-0 clock pulls the line down from the pull-up's 1) is left out."""
    return [t - 1 for t in wire_edges if t > 1]


def data_drives(launch, v=VALUE):
    """The drive cycles of the data line's edges, for bit k launched at launch[k] (idle 1)."""
    level, out = 1, []
    for at, b in zip(launch, bits(v)):
        if b != level:
            out.append(at)
            level = b
    return out


def got(t, i):
    """Engine i's XFER value: dst, the wire value in every role (isa §4.3); None if it timed out."""
    (rec,) = [r for r in t.engines[i].records if r.op == "XFER"]
    return rec.value


def clock_drives(tb, n=N):
    """isa §4.3: leading edge k at tb + (2k+1)H, trailing at tb + (2k+2)H."""
    return [tb + j * H for j in range(1, 2 * n + 1)]


def tb_of(cpha, start=START, tb=0):
    """The clock owner's tb at the start: max(tb, start) for cpha 0, max(tb, start - H) for 1."""
    return max(tb, start) if cpha == 0 else max(tb, start - H)


# ---------------------------------------------------------------- edges, launch, capture
@pytest.mark.parametrize("cpol", [0, 1])
@pytest.mark.parametrize("cpha", [0, 1])
def test_clock_owner_edges_and_data(cpol, cpha):
    """The clock owner (also the data owner) makes 2n edges from its re-based tb; the data
    launches at bit 0 then on the launching edges; every role gets the value; tb ends at
    tb + 2nH in every role."""
    t = run_sys(spi(cpol=cpol, cpha=cpha))
    tb = tb_of(cpha)
    sck, dat = t.wire_edges
    assert drive_cycles(sck) == clock_drives(tb)
    # launch: cpha 0 bit 0 at tb, bit k on trailing edge k-1; cpha 1 bit k on leading edge k
    launch = [tb + 2 * k * H for k in range(N)] if cpha == 0 else [tb + (2 * k + 1) * H for k in range(N)]
    assert drive_cycles(dat) == data_drives(launch)
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
    assert drive_cycles(t.wire_edges[1]) == data_drives(launch)


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_followers_ignore_the_clock_settling_to_cpol(cpha, data_owner):
    """The idle-0 clock's start-up drive (isa §2) pulls it from the pull-up's 1 to 0. With the
    clock owner one tick behind, P1 and MON see that edge in their XFER's start cycle. It goes
    to cpol, so it isn't a leading edge, and a clock follower counts from its first leading edge
    (isa §4.3)."""
    t = run_sys(spi(cpha=cpha, data_owner=data_owner), phases={0: 1})
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_completion_matches_the_compiler(cpha, data_owner):
    """Each role completes where analysis.completion() says, relative to the step's end tb, for
    the step the compiler extracts from the image, so the compiler's L and the model agree (the
    rule cross-check, verification.md)."""
    d = spi_desc(cpha=cpha, data_owner=data_owner)
    step = _steps(lower(d))[XFER_PC]
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = [x for x in t.engines[i].records if x.op == "XFER"]
        assert rec.cycle - rec.tb == completion(step, r)[0], r


def test_lsb_first():
    t = run_sys(spi(msb=False))
    assert all(got(t, i) == VALUE for i in range(3))
    assert t.wire_edges[1] != run_sys(spi(msb=True)).wire_edges[1]


@pytest.mark.parametrize("cpha", [0, 1])
def test_late_clock_owner_rebases_and_never_overruns(cpha):
    """isa §4.1: a clock owner that starts late re-bases (cpha 0: tb := start; cpha 1:
    start - H, so its first edge is in its start cycle) instead of logging OVERRUN, and the
    followers follow its edges."""
    def head(d):
        with d.only("a"):
            d.raw_delay(20)
    t = run_sys(spi(cpha=cpha, head=head))
    start = START + 2 + 20  # ROLE_IF, then RAW DELAY 20
    assert drive_cycles(t.wire_edges[0]) == clock_drives(tb_of(cpha, start))
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_clock_owner_rebase_ignores_a_pop_stall(cpha, data_owner):
    """isa §4.1: a clock owner's clocked XFER re-bases by its own rule, also after a POP stall,
    whoever owns the data. With cpha 1 the first edge is then in the start cycle."""
    def head(d):
        with d.only("a"):
            d.pop("r7")
    t = run_sys(spi(cpha=cpha, data_owner=data_owner, head=head), tx={0: [1]}, push_at=30)
    start = next(now for pc, now in t.engines[0].starts if pc == 3)  # LI, ROLE_IF, POP, XFER
    assert start > 30 and drive_cycles(t.wire_edges[0]) == clock_drives(tb_of(cpha, start))
    assert all(not t.engines[i].violations and got(t, i) == VALUE for i in range(3))


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
def test_ldelay_delays_the_edge_launches(cpha, data_owner):
    """A launch triggered by an edge waits ldelay: the clock owner's at edge + ldelay, the clock
    follower's at max(detect, e + ldelay), e = detect - EDGE_LAT = the edge's drive cycle; with
    ldelay 4 > EDGE_LAT both are at edge + 4. cpha 0 bit 0 has no ldelay. The bits still reach
    P0's captures in time."""
    img = spi(cpha=cpha, data_owner=data_owner)
    img = replace(img, lanes=[replace(img.lanes[0], ldelay=4)])
    t = run_sys(img)
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))
    tb = tb_of(cpha)
    if cpha == 0:
        bit0 = tb if data_owner == "a" else START
        launch = [bit0] + [tb + 2 * k * H + 4 for k in range(1, N)]
    else:
        launch = [tb + (2 * k + 1) * H + 4 for k in range(N)]
    assert drive_cycles(t.wire_edges[1]) == data_drives(launch)


# ---------------------------------------------------------------- CLK_PHASE and TIMEOUT
def test_clock_follower_clk_phase():
    """CHK: a phase shorter than min - QUANT logs CLK_PHASE; the first edge isn't checked. The
    clock owner runs at half the period, so all 2n - 1 later phases are short."""
    img = spi(check=True, lo=H, hi=H + 2)
    t = run_sys(img, images={0: img.with_profiles({"H": 0.5})})
    assert kinds(t, 1).count("CLK_PHASE") == 2 * N - 1 and kinds(t, 2).count("CLK_PHASE") == 2 * N - 1
    assert "CLK_PHASE" not in kinds(t, 0)  # the clock owner doesn't check its own clock


def test_clock_follower_clk_phase_boundary():
    """A phase of exactly min - QUANT is in the window: the clock owner's half period is 3 and
    the followers' min is 4."""
    img = spi(check=True, lo=H, hi=H + 2)
    fast = replace(img, profiles=[img.profiles[0], replace(img.profiles[1], nom=3)])
    t = run_sys(img, images={0: fast})
    assert not any(t.engines[i].events for i in range(3))


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
    its start. Without ABORT: to := 1, tb := now, a re-anchor at now, no dst, and the step
    ends."""
    s = System([EngineSpec(spi(hi=H + 2), Role.P1)])
    t = s.run(1000)
    et = t.engines[0]
    (ev,) = et.events
    assert ev.kind == "TIMEOUT" and ev.cycle == START + (H + 2) + QUANT + 1
    (rec,) = [r for r in et.records if r.op == "XFER"]
    assert rec.tb == ev.cycle and rec.value is None and et.halted
    assert s.engines[0].anchor == ev.cycle and s.engines[0].acc == (0, 0, 0)


def test_clock_follower_timeout_after_an_edge():
    """After an edge the count restarts from that edge's detection. The clock stops (a glitch
    holds it) after the first leading edge."""
    img = spi(hi=H + 2)
    lead0 = tb_of(0) + H          # drive cycle; seen at + EDGE_LAT
    t = run_sys(img, roles=(Role.P0, Role.P1), glitches=[Glitch(0, lead0 + 1 + H, 400, 1)])
    ev = next(e for e in t.engines[1].events if e.kind == "TIMEOUT")
    # the last edge P1 sees is the first leading edge (the trailing edge is held off)
    assert ev.cycle == lead0 + EDGE_LAT + (H + 2) + QUANT + 1


def test_clock_follower_edge_wins_over_timeout():
    """isa §4.1: an edge and a timeout in the same cycle, the edge wins. P1 starts at 4 and sees
    the first edge at 4 + H + EDGE_LAT = 11, the cycle its max of 5 runs out (11 - 4 > 5 + 1)."""
    t = run_sys(spi(hi=5))
    assert not any(t.engines[i].events for i in range(3)) and got(t, 1) == VALUE


def test_clock_follower_timeout_with_abort():
    t = run_sys(spi(hi=H + 2, abort=True), roles=(Role.P1,))
    assert kinds(t, 0) == ["TIMEOUT", "ABORT"]


# ---------------------------------------------------------------- FIFO
def test_underrun_sends_ones_and_keeps_the_fifo():
    """isa §4.3: a data owner that doesn't own the clock, with src = FIFO empty at its start,
    sends ONES, logs UNDERRUN and consumes nothing: a word that arrives during the step stays."""
    t = run_sys(spi(data_owner="b", src="fifo"), tx={1: [VALUE]}, push_at=6)
    assert kinds(t, 1) == ["UNDERRUN"] and got(t, 0) == 0xFF and t.engines[1].tx_left == 1
    t2 = run_sys(spi(data_owner="b", src="fifo"), tx={1: [VALUE]})
    assert not t2.engines[1].events and got(t2, 0) == VALUE and t2.engines[1].tx_left == 0


def test_clock_owner_waits_for_fifo_data():
    """A clock owner with src = FIFO empty waits, checking every cycle, then starts in the cycle
    it takes the word (cpha 0: tb := that cycle). Nobody logs anything."""
    t = run_sys(spi(src="fifo"), tx={0: [VALUE]}, push_at=30)
    took = 30  # the host's word is in TX from tick 30, and P0 runs at phase 0
    assert drive_cycles(t.wire_edges[0]) == clock_drives(took)
    assert all(not t.engines[i].events and got(t, i) == VALUE for i in range(3))


# ---------------------------------------------------------------- compiler rules
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


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("data_owner", ["a", "b"])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("slow,phase", [(None, 0), (0, 0), (0, 1), (1, 0), (1, 3)])
def test_accepted_spi_runs_clean_at_2pct(cpha, data_owner, bounded, slow, phase):
    """The rule cross-check (verification.md): a clocked XFER the compiler accepts at 2% (T2, T3,
    T4, T5-watch) runs clean on the model with the clock owner or the others 2% slow and out of
    phase. Bounded: CHK and max = 12, near T3's first-edge limit."""
    kw = dict(check=True, lo=6, hi=12) if bounded else dict(lo=7)
    img = spi(cpha=cpha, data_owner=data_owner, h=8, timing=True, **kw)
    on = {None: (), 0: (0,), 1: (1, 2)}[slow]
    t = run_sys(img, clocks={i: (49, 50) for i in on}, phases={i: phase for i in on})
    for i in range(3):
        assert not t.engines[i].violations and got(t, i) == VALUE, (i, t.engines[i].events)


@pytest.mark.parametrize("cpha", [0, 1])
def test_t3_rejects_a_follower_that_times_out_before_the_first_edge(cpha):
    """T3: a bounded clock follower counts from its own start, so the first edge must come within
    max + QUANT of it. P0 starts 12 cycles late and re-bases; P1 times out on the model, and the
    compiler rejects the program."""
    def head(d):
        with d.only("a"):
            d.raw_delay(12)
    kw = dict(cpha=cpha, h=8, lo=6, hi=10, head=head)
    assert "TIMEOUT" in kinds(run_sys(spi(**kw)), 1)
    with pytest.raises(CompileError, match="T3"):
        spi(timing=True, **kw)


def test_t3_rejects_a_max_below_the_phase():
    """A bounded clock follower without CHK still needs every phase within max + QUANT."""
    with pytest.raises(CompileError, match="T3"):
        spi(h=8, lo=6, hi=6, timing=True)


@pytest.mark.parametrize("h", [2, 4])
def test_t3_rejects_a_short_half_period(h):
    """T3: a data owner that isn't the clock owner needs H >= EDGE_LAT + 1 + ldelay + ceil(d*2H),
    so 5 at 2%. It launches when it sees the launching edge, EDGE_LAT after it, and the clock
    owner's capture sees the wire 1 cycle after the capture edge. So H >= EDGE_LAT is enough at
    equal clocks: H = 2 misreads, while H = 4 still reads right with P1 2% slow (T3 keeps a
    cycle plus the drift as margin)."""
    with pytest.raises(CompileError, match="T3"):
        spi(data_owner="b", h=h, lo=h - 1, timing=True)
    img = spi(data_owner="b", h=h, lo=h - 1)
    assert (got(run_sys(img), 0) == VALUE) == (h >= EDGE_LAT)
    if h == 4:
        for phase in range(4):
            t = run_sys(img, clocks={1: (49, 50), 2: (49, 50)}, phases={1: phase, 2: phase})
            assert got(t, 0) == VALUE


def test_stretch_fails_loudly():
    img = spi()
    img = replace(img, lanes=[replace(img.lanes[0], stretch=1)])
    with pytest.raises(NotImplementedError, match="part 2c"):
        run_sys(img)


# ---------------------------------------------------------------- duplex (isa §4.3)
A, B = 0x4B, 0xC6  # P0's MOSI word, P1's MISO word


def duplex_desc(cpha=0, src="r0", dst="r1", n=N, h=H, lo=H - 1, hi=INF, ppm=20000, ldelay=0,
                miso_owner="b", swap=False, miso_kind="pp", tail=None):
    """P0 owns the clock and MOSI; MISO belongs to `miso_owner`. MOSI is the lane's `data` and
    MISO its `data2`, or the other way round with `swap` (the step is then owned by MISO's owner).
    Each party loads its own word into r0, and the one XFER sends r0 on the line it owns."""
    d = Description("spi2", parties=("a", "b"), tolerance_ppm=ppm)
    sck = d.line("sck", owner="a", idle=0)
    mosi = d.line("mosi", owner="a", idle=1)
    miso = d.line("miso", miso_kind, owner=miso_owner, idle=1)
    first, second, owner = (miso, mosi, miso_owner) if swap else (mosi, miso, "a")
    lane = d.lane("L", first, clk=sck, cpha=cpha, data2=second, ldelay=ldelay)
    half = d.profile("H", nom=h, min=lo, max=hi)
    with d.only("a"):
        d.li("r0", A)
    with d.only("b"):
        d.li("r0", B)
    d.xfer(n, lane, owner=owner, prof=half, src=src, dst=dst, msb_first=True)
    if tail:
        tail(d)
    d.halt()
    return d


def packed(swap=False):
    """dst := (v2 << n) | v1: data2's word on top."""
    return A << N | B if swap else B << N | A


def duplex(timing=False, **kw):
    return compile_desc(duplex_desc(**kw), check_timing=timing)


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("swap", [False, True])
def test_duplex_exchanges_both_words(cpha, swap):
    """Each data line follows the clocked rules with its own owner, and dst := (v2 << n) | v1
    in every role: P0 sends A on MOSI and samples MISO, P1 sends B on MISO and samples MOSI,
    whichever of the two is the lane's data2."""
    t = run_sys(duplex(cpha=cpha, swap=swap))
    for i in range(3):
        assert not t.engines[i].events and got(t, i) == packed(swap), (i, hex(got(t, i) or 0))
    # P0 samples MISO only; P1 samples MOSI only; MON samples both
    assert {s.line for s in t.engines[0].samples} == {2} and {s.line for s in t.engines[1].samples} == {1}
    assert len(t.engines[2].samples) == 2 * N


def test_duplex_pushes_in_every_engine():
    """dst = FIFO: every engine that drives fewer than all of the step's data lines pushes the
    packed value, here all three."""
    t = run_sys(duplex(dst="fifo"))
    assert [t.engines[i].rx for i in range(3)] == [[B << N | A]] * 3


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("h", [2, 4])
@pytest.mark.parametrize("swap", [False, True])
def test_duplex_completion_matches_the_compiler(cpha, h, swap):
    """With duplex the clock owner also follows a data line (MISO), so it completes at the
    follow offset, as analysis.completion() says. At H = 2 that is 1 cycle after its last edge
    with cpha 0 too."""
    d = duplex_desc(cpha=cpha, h=h, lo=h - 1, swap=swap)
    step = next(s for s in _steps(lower(d)).values() if s.clocked)
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = [x for x in t.engines[i].records if x.op == "XFER"]
        assert rec.cycle - rec.tb == completion(step, r)[0], r


@pytest.mark.parametrize("swap", [False, True])
def test_duplex_underrun_on_miso(swap):
    """src = FIFO: P0 owns the clock and waits for its word, which comes at tick 30, then starts
    (cpha 0: tb := 30); P1 doesn't own the clock, finds its FIFO empty at its start, sends ONES
    on MISO and logs UNDERRUN."""
    t = run_sys(duplex(src="fifo", swap=swap), tx={0: [A]}, push_at=30)
    assert kinds(t, 1) == ["UNDERRUN"] and not t.engines[0].events
    assert drive_cycles(t.wire_edges[0]) == clock_drives(30)
    assert got(t, 0) == (A << N | 0xFF if swap else 0xFF << N | A)


def test_duplex_t3_covers_the_miso_owner():
    """T3's data-owner bound applies to data2's owner too: at H = 4 it isn't the clock owner."""
    with pytest.raises(CompileError, match="T3"):
        duplex(timing=True)


@pytest.mark.parametrize("cpha", [0, 1])
@pytest.mark.parametrize("slow,phase", [(None, 0), (0, 1), (1, 0), (1, 3)])
def test_accepted_duplex_runs_clean_at_2pct(cpha, slow, phase):
    """The rule cross-check for duplex. P1 reaches the XFER 8 cycles after tb (two ROLE_IF blocks
    load the words), so with cpha 0 T3 wants H >= 10 for its bit 0 (the test uses 12); cpha 1
    takes H = 8."""
    h = 12 if cpha == 0 else 8
    img = duplex(cpha=cpha, h=h, lo=h - 1, timing=True)
    on = {None: (), 0: (0,), 1: (1, 2)}[slow]
    t = run_sys(img, clocks={i: (49, 50) for i in on}, phases={i: phase for i in on})
    for i in range(3):
        assert not t.engines[i].violations and got(t, i) == B << N | A, (i, t.engines[i].events)


# ---------------------------------------------------------------- lane config rules
def test_lane_carries_data2_and_ldelay_into_the_image():
    img = duplex(ldelay=2)
    (lane,) = img.lanes
    assert (lane.data, lane.clk, lane.data2, lane.ldelay) == (1, 0, 2, 2)


def test_data2_needs_a_clocked_lane():
    d = Description("t", parties=("a", "b"))
    lane = d.lane("L", d.line("d", owner="a"), data2=d.line("d2", owner="b"))
    d.xfer(8, lane, owner="a", prof=d.profile("P", nom=16))
    d.halt()
    with pytest.raises(CompileError, match="S-lane"):
        compile_desc(d, check_timing=False)


def test_data2_owner_differs_from_data_owner():
    with pytest.raises(CompileError, match="R1-owner"):
        duplex(miso_owner="a")


def test_duplex_needs_a_party_owner():
    """A SHARED step on an OD data line would let one party drive both lines, and analysis
    skips SHARED steps, so a duplex step has a party owner and data2 belongs to the other."""
    d = Description("t", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=0)
    sda = d.line("sda", "od", owner="shared")
    lane = d.lane("L", sda, clk=sck, data2=d.line("d2", owner="a"))
    d.xfer(8, lane, owner="shared", prof=d.profile("H", nom=8))
    d.halt()
    with pytest.raises(CompileError, match="R1-owner"):
        compile_desc(d, check_timing=False)


@pytest.mark.parametrize("which", ["data2", "data"])
def test_lane_lines_are_distinct(which):
    """A data line that is also the clock: the followers never see the edges they count."""
    d = Description("t", parties=("a", "b"))
    sck = d.line("sck", owner="a", idle=0)
    other = d.line("d", owner="b")
    lane = (d.lane("L", other, clk=sck, data2=sck) if which == "data2" else d.lane("L", sck, clk=sck))
    d.xfer(8, lane, owner="b" if which == "data2" else "a", prof=d.profile("H", nom=8))
    d.halt()
    with pytest.raises(CompileError, match="S-lane"):
        compile_desc(d, check_timing=False)


def test_t5_stall_covers_a_clock_owner_on_data2():
    """T5-stall: a src = FIFO XFER waits in the clock owner whichever data line it owns, so a
    bounded one is rejected also when the clock owner owns data2."""
    with pytest.raises(CompileError, match="T5-stall"):
        duplex(swap=True, src="fifo", h=12, lo=11, hi=60, timing=True)
    with pytest.raises(CompileError, match="T5-stall"):
        duplex(src="fifo", h=12, lo=11, hi=60, timing=True)


def test_duplex_n_at_most_8():
    with pytest.raises(CompileError, match="S-range"):
        duplex(n=9)


@pytest.mark.parametrize("bad", [-1, 16])
def test_ldelay_is_4_bits(bad):
    with pytest.raises(ValueError, match="ldelay"):
        duplex_desc(ldelay=bad)


def test_t4_bounds_ldelay():
    """T4: ldelay <= H - EDGE_LAT - 2 (when there is an ldelay): 3 is the most at H = 8."""
    duplex(cpha=1, h=8, lo=7, ldelay=3, timing=True)
    with pytest.raises(CompileError, match="T4"):
        duplex(cpha=1, h=8, lo=7, ldelay=4, timing=True)


def test_t3_counts_ldelay():
    """T3's data-owner bound has ldelay in it: H >= EDGE_LAT + 1 + ldelay + ceil(d*2H). At H = 12
    and 5% that allows ldelay 6, while T4 (ldelay <= H - EDGE_LAT - 2) would allow 7."""
    duplex(h=12, lo=11, ldelay=6, ppm=50000, timing=True)
    with pytest.raises(CompileError, match="T3") as e:
        duplex(h=12, lo=11, ldelay=7, ppm=50000, timing=True)
    assert "T4" not in str(e.value)


# ---------------------------------------------------------------- OD data (isa §4.3, part 2b)
HI2C = 8
LD = 2


def i2c_desc(steps, ldelay=LD, h=HI2C):
    """SCL (push-pull here; stretching is part 2c) owned by a, cpol 1 / cpha 1 as I2C; SDA a
    shared OD line. `steps` is a list of (owner, n, value) XFERs, each party loading its own
    values into r0-r3 first; every XFER writes its wire value to r4 + i."""
    d = Description("i2c", parties=("a", "b"))
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=ldelay)
    half = d.profile("H", nom=h, min=h - 1)
    for party in ("a", "b"):
        with d.only(party):
            for i, (owner, _, value) in enumerate(steps):
                if owner == party:
                    d.li(f"r{i}", value)
    for i, (owner, n, _) in enumerate(steps):
        d.xfer(n, lane, owner=owner, prof=half, src=f"r{i}", dst=f"r{4 + i}", msb_first=True)
    d.halt()
    return d


def i2c(steps, **kw):
    return compile_desc(i2c_desc(steps, **kw), check_timing=False)


def xfers(t, i):
    return [r for r in t.engines[i].records if r.op == "XFER"]


def edges_of(t, line, lo=0, hi=10 ** 9):
    return [x for x in t.wire_edges[line] if lo <= x < hi]


@pytest.mark.parametrize("sender", ["a", "b"])
def test_od_owner_reads_back_the_wire(sender):
    """dst is the wire value in every role: a glitch that pulls a released 1 of the byte low
    reaches the sender's dst too (the clock owner reads back at capture + EDGE_LAT, a clock
    follower when it sees the capture edge)."""
    value = 0xFF
    clean = run_sys(i2c([(sender, 8, value)]))
    (rec,) = xfers(clean, 0)
    assert all(xfers(clean, i)[0].value == value for i in range(3))
    # pull SDA low for the one tick the third capture reads: trailing edge 2 is driven at cap2,
    # on the wire from cap2 + 1, and every role reads the wire as of cap2 + 1
    cap2 = rec.tb - (2 * 8 - 6) * HI2C   # tb + 6H, from the end tb = tb0 + 16H
    t = run_sys(i2c([(sender, 8, value)]), glitches=[Glitch(1, cap2 + 1, 1, 0)])
    assert {xfers(t, i)[0].value for i in range(3)} == {0xDF}, [hex(xfers(t, i)[0].value) for i in range(3)]


@pytest.mark.parametrize("sender", ["a", "b"])
def test_od_owner_completes_on_its_last_readback(sender):
    """isa §6: OD owners complete at the follow offsets (clocked: the last capture + EDGE_LAT),
    as analysis.completion() says, in every role."""
    d = i2c_desc([(sender, 8, 0x5A)])
    step = next(s for s in _steps(lower(d)).values() if s.clocked)
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = xfers(t, i)
        assert rec.cycle - rec.tb == completion(step, r)[0], r


@pytest.mark.parametrize("h,ld", [(HI2C, LD), (12, 5)])
def test_ack_is_held_until_the_next_falling_scl(h, ld):
    """OD let-go: the slave's ACK (0) stays on SDA after its XFER, through the high SCL, until
    it sees the next leading edge (SCL falling); it lets go at max(detect, e + ldelay), so
    ldelay 5 > EDGE_LAT waits past the detect. The master's next byte starts with a 1, so SDA
    rises when the slave lets go. Without the let-go SDA would rise while SCL is high: a STOP."""
    t = run_sys(i2c([("a", 8, 0xA4), ("b", 1, 0), ("a", 8, 0x80)], h=h, ldelay=ld))
    assert [r.value for r in xfers(t, 1)] == [0xA4, 0, 0x80] and not t.engines[1].events
    ack_end = xfers(t, 0)[1].tb               # the ACK's last (trailing) edge, drive cycle
    lead = ack_end + h                         # the next byte's first leading edge (SCL falls)
    sda_rise = [x for x in t.wire_edges[1] if x > ack_end]
    # SCL is high from ack_end + 1 to lead; the slave sees the fall at lead + EDGE_LAT
    assert sda_rise and sda_rise[0] == max(lead + EDGE_LAT, lead + ld) + 1, (lead, sda_rise)


def test_a_new_launch_cancels_the_let_go():
    """Any drive of the line cancels a pending release: two 0 bits in a row from the slave make
    no SDA edge between them."""
    t = run_sys(i2c([("a", 8, 0xA4), ("b", 1, 0), ("b", 1, 0), ("a", 8, 0x80)]))
    first_ack_end, second_ack_end = xfers(t, 0)[1].tb, xfers(t, 0)[2].tb
    assert not edges_of(t, 1, first_ack_end, second_ack_end + 1)
    assert [r.value for r in xfers(t, 0)] == [0xA4, 0, 0, 0x80]


def test_the_clock_owner_lets_go_at_its_own_edge():
    """The master's byte ends with a 0; it keeps it until it drives the next leading edge (the
    ACK clock's SCL fall) and lets go ldelay later. The slave pulls SDA low for the ACK when it
    sees that edge, so SDA goes high for a moment while SCL is low."""
    t = run_sys(i2c([("a", 8, 0xA4), ("b", 1, 0)]))
    byte_end = xfers(t, 0)[0].tb
    lead = byte_end + HI2C                     # ACK clock: leading edge driven by the master
    assert edges_of(t, 1, byte_end + 1, lead + 2 * HI2C) == [lead + LD + 1, lead + EDGE_LAT + 1]
    assert [r.value for r in xfers(t, 1)] == [0xA4, 0]


def test_let_go_runs_during_untimed_instructions():
    """The tracker watches the clock every cycle, whatever runs: after its ACK the slave is in a
    RAW DELAY when the master's next byte starts, and still lets go at max(detect, e + ldelay).
    Program: XFER 1 (ACK, b) · [b: RAW DELAY 60 · HALT] · XFER 8 (a, ONES) · HALT."""
    d = Description("i2c", parties=("a", "b"))
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=LD)
    half = d.profile("H", nom=HI2C, min=HI2C - 1)
    d.xfer(1, lane, owner="b", prof=half, src="zero")
    with d.only("b"):
        d.raw_delay(60)
        d.halt()
    d.xfer(8, lane, owner="a", prof=half, src="ones")
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1))
    delay_start = dict(t.engines[1].starts)[2]
    ack_end = xfers(t, 0)[0].tb
    lead = ack_end + HI2C
    assert delay_start < lead + EDGE_LAT < delay_start + 60  # the slave is in its RAW DELAY
    assert edges_of(t, 1, ack_end + 1)[0] == lead + EDGE_LAT + 1


def test_let_go_also_after_a_timeout():
    """A timed-out XFER drove its OD line too, so the let-go applies: the slave sends 0 with
    cpha 0 (bit 0 at its start), the clock stops (a glitch holds SCL high), the slave times out
    and keeps SDA low through its RAW DELAY, and lets go at the next launching edge it sees: with
    cpha 0 a trailing edge, the first SCL rise after the glitch."""
    d = Description("i2c", parties=("a", "b"))
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=0, ldelay=LD)
    with d.only("b"):
        d.li("r0", 0)
    d.xfer(8, lane, owner="b", prof=d.profile("H", nom=HI2C, min=HI2C - 1, max=HI2C + 2), src="r0")
    with d.only("b"):
        d.raw_delay(200)
    d.halt()
    g_end = 13 + 60
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1),
                glitches=[Glitch(0, 13, 60, 1)])
    assert "TIMEOUT" in kinds(t, 1)
    # after the glitch the master's SCL falls (leading) and then rises (trailing, launching)
    scl_rise = sorted(x for x in t.wire_edges[0] if x >= g_end)[1]
    halt = dict(t.engines[1].starts)[5]  # ROLE_IF, LI, XFER, ROLE_IF, RAW DELAY, HALT
    rise = [x for x in t.wire_edges[1] if x > 13]
    assert rise and rise[0] == scl_rise + EDGE_LAT < halt, (scl_rise, rise, halt)


def i2c_cpha_desc(steps, cpha, ldelay=LD, h=16):
    d = Description("od", parties=("a", "b"))
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=cpha, ldelay=ldelay)
    half = d.profile("H", nom=h, min=h - 1, max=INF)
    for i, (owner, n, src) in enumerate(steps):
        d.xfer(n, lane, owner=owner, prof=half, src=src, dst=f"r{4 + i}", msb_first=True)
    d.halt()
    return d


@pytest.mark.parametrize("first,second", [("a", "b"), ("b", "a")])
@pytest.mark.parametrize("cpha", [0, 1])
def test_let_go_before_the_next_owner_bit_0(first, second, cpha):
    """With cpha 0 the next leading edge captures the next step's bit 0, so the let-go is at the
    next launching edge: the step's own last trailing edge (cpha 0), or the next leading edge
    (cpha 1). A 0 from one party must not leak into the other party's first bit."""
    steps = [(first, 8, "ones"), (second, 1, "zero"), (first, 8, "ones")]
    d = i2c_cpha_desc(steps, cpha)
    compile_desc(d)  # accepted with the timing rules
    t = run_sys(compile_desc(d, check_timing=False))
    for i in range(3):
        assert [r.value for r in xfers(t, i)] == [0xFF, 0, 0xFF], (i, [hex(r.value) for r in xfers(t, i)])


def test_no_let_go_without_a_drive():
    """A cpha 1 XFER that times out before its first edge drove nothing, so it lets nothing go:
    the slave's earlier HOLD 0 stays on SDA when a leading edge comes later."""
    d = Description("od", parties=("a", "b"))
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=LD)
    p = d.profile("H", nom=HI2C, min=HI2C - 1, max=HI2C + 2)
    d.hold(sda, 0, owner="b", prof=p)
    d.xfer(1, lane, owner="b", prof=p, src="ones")
    with d.only("b"):
        d.raw_delay(100)
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P1,), glitches=[Glitch(0, 60, 4, 0)])
    assert "TIMEOUT" in kinds(t, 0)
    halt = dict(t.engines[0].starts)[4]  # HOLD, XFER, ROLE_IF, RAW DELAY, HALT
    assert not [x for x in t.wire_edges[1] if 3 < x <= halt]


@pytest.mark.parametrize("h,rejected", [(3, True), (8, False)])
def test_t3_covers_a_let_go_on_a_shared_od_lane(h, rejected):
    """A clock follower that holds a let-go releases with a launch's latency, also inside the
    other party's step: on a lane with a shared OD data line T3's launch bound applies to both
    parties, so the master's byte at H = 3 after the slave's ACK is rejected at 2%."""
    d = Description("od", parties=("a", "b"), tolerance_ppm=20000)
    scl = d.line("scl", owner="a", idle=1)
    sda = d.line("sda", "od", owner="shared", idle=1)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    ack = d.profile("A", nom=8, min=7, max=INF)
    byte = d.profile("B", nom=h, min=h - 1, max=INF)
    d.xfer(1, lane, owner="b", prof=ack, src="zero")
    d.xfer(8, lane, owner="a", prof=byte, src="ones")
    d.halt()
    if rejected:
        with pytest.raises(CompileError, match="T3"):
            compile_desc(d)
    else:
        compile_desc(d)


def test_od_data2_taints_dst():
    """R2: dst of an XFER with an OD data line is tainted (the wire can differ from what was
    sent), data2 included."""
    def tail(d):
        d.br("end", cond="z", reg="r1")
        d.label("end")
    d = duplex_desc(miso_kind="od", tail=tail)
    with pytest.raises(CompileError, match="R2-branch"):
        compile_desc(d, check_timing=False)
