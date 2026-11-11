"""The bus (isa §4.7): the START/STOP detector, COND START/STOP (§4.2), bus exits, and COND MEAS.

I2C here: SDA (bus_a, shared OD) and SCL (bus_b, OD, owned by a), confirm 15 cycles. A drive in
cycle t is on the wire from t + 1 and seen at t + EDGE_LAT (isa §1)."""
import pytest

from emu.analysis import _steps, completion
from emu.compiler import CompileError, compile_desc, lower
from emu.desc import Description
from emu.isa import EDGE_LAT, INF, QUANT, Role
from emu.model.system import Glitch

from simutil import kinds, run_sys

NOM = 8
CONFIRM = 15


def i2c_desc(body, confirm=CONFIRM, abort_label=None):
    """`body(d, sda, scl, p)` builds the program. The master then waits before its HALT, because
    HALT releases its OD lines: SDA rising while SCL is high would be a STOP."""
    d = Description("bus", parties=("a", "b"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="a", idle=1)
    d.bus("i2c", sda, scl, confirm=confirm)
    if abort_label:
        d.abort_to(abort_label)
    body(d, sda, scl, d.profile("P", nom=NOM, min=0, max=INF))
    with d.only("a"):
        d.raw_delay(200)
    d.halt()
    return d


def i2c(body, **kw):
    return compile_desc(i2c_desc(body, **kw), check_timing=False)


def conds(t, i):
    return [r for r in t.engines[i].records if r.op == "COND"]


def start_body(d, sda, scl, p):
    d.cond("start", owner="a", prof=p)


SEEN = NOM + EDGE_LAT                 # the START's SDA fall, driven at tb + nom = 8, seen at 11
CONFIRMED = SEEN + CONFIRM            # SCL stayed high for the next 15 cycles: end of cycle 26


# ---------------------------------------------------------------- the detector, COND START/STOP
def test_start_is_confirmed_and_followed():
    """The followers confirm the START at the end of cycle 26 and complete their followed START
    at 27 (the flag is visible from the next cycle) with tb := start_e = 8."""
    t = run_sys(i2c(start_body))
    (do,) = conds(t, 0)
    assert do.mode == "do" and do.cycle == do.tb == NOM
    for i in (1, 2):
        (f,) = conds(t, i)
        assert (f.mode, f.cycle, f.tb) == ("follow", CONFIRMED + 1, NOM)
        assert t.engines[i].bus_events == [(CONFIRMED, "START")] and not t.engines[i].events
    assert not t.engines[0].bus_events  # the master line's owner runs no detector


def test_start_completion_matches_the_compiler():
    """isa §6: a followed START completes at E + bus_confirm + 1 after its tb."""
    d = i2c_desc(start_body)
    step = next(iter(_steps(lower(d)).values()))
    t = run_sys(compile_desc(d, check_timing=False))
    for i, r in enumerate((Role.P0, Role.P1, Role.MON)):
        (rec,) = conds(t, i)
        assert rec.cycle - rec.tb == completion(step, r)[0], r


def test_stop_after_start():
    """STOP: SDA rises while SCL is high. START cleared both flags, so the STOP is new, and the
    followed STOP clears both again."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=d.profile("W", nom=40, min=0, max=INF))
    t = run_sys(i2c(body))
    _, stop_do = conds(t, 0)
    for i in (1, 2):
        _, e = conds(t, i)
        assert e.tb == stop_do.cycle and e.cycle == stop_do.cycle + EDGE_LAT + CONFIRM + 1
        assert [k for _, k in t.engines[i].bus_events] == ["START", "STOP"]
        assert t.engines[i].evt & 3 == 0


def test_a_failed_confirm_drops_the_candidate():
    """SDA falls while SCL is high, but SCL falls 5 cycles later (inside the 15-cycle confirm):
    no START, so the follower's bounded START times out."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=60))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(1, NOM + 1 + 5, 3, 0)])
    assert not t.engines[1].bus_events and "TIMEOUT" in kinds(t, 1)


def test_followed_start_with_the_flag_already_set_completes_at_once():
    """isa §4.2: a followed START completes on a flag set before the step (a repeated START):
    P1 reaches its COND START long after the flag is set."""
    def body(d, sda, scl, p):
        with d.only("b"):
            d.raw_delay(40)
        d.cond("start", owner="a", prof=p)
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    (do,), (f,) = conds(t, 0), conds(t, 1)
    start = dict(t.engines[1].starts)[2]  # ROLE_IF, RAW DELAY, COND
    assert start > CONFIRMED and (f.cycle, f.tb) == (start, do.cycle)


def test_followed_start_logs_early():
    """EARLY: m = start_e - tb below min - QUANT. The followers' profile wants the START at least
    18 cycles after tb (nom 24, min 18); the master's comes at 8."""
    def body(nom, lo):
        return lambda d, sda, scl, p: d.cond("start", owner="a", prof=d.profile("S", nom=nom, min=lo, max=INF))
    img, late = i2c(body(NOM, 6)), i2c(body(24, 18))
    t = run_sys(img, images={1: late, 2: late})
    assert "EARLY" in kinds(t, 1) and "EARLY" in kinds(t, 2)


def test_followed_start_times_out_with_the_confirm_included():
    """The timeout uses now - EDGE_LAT - bus_confirm (isa §4.2): with max 30, no START, and
    tb = 0, TIMEOUT at 30 + QUANT + EDGE_LAT + bus_confirm + 1."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=30))
    t = run_sys(i2c(body), roles=(Role.P1,))
    (ev,) = t.engines[0].events
    assert ev.kind == "TIMEOUT" and ev.cycle == 30 + QUANT + EDGE_LAT + CONFIRM + 1


# ---------------------------------------------------------------- bus exits
def exit_desc(abort):
    """START, then a HOLD the slave follows (with or without ABORT), then HALT; abort_pc ("top") is
    an unbounded followed START. A glitch at 30 (after the first START is confirmed at 26) lifts
    SDA for one tick while SCL is high: P1 sees a rise and a fall, so a new START is confirmed at
    48, during its HOLD (it samples at 51)."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=p)
        d.hold(sda, 0, owner="a", prof=d.profile("L", nom=80, min=0, max=INF), abort=abort)
        with d.only("a"):
            d.raw_delay(200)  # a HOLD do completes when it drives: keep the bus until P1 is done
        d.halt()
        d.label("top")
        d.cond("start", owner="a", prof=p)
    return compile_desc(i2c_desc(body, abort_label="top"), check_timing=False)


def test_a_repeated_start_is_a_bus_exit_with_abort():
    """With ABORT a START during a timed step is a bus exit: normal (BUS, no ABORT and no
    violation), the flag stays set, and abort_pc's followed START completes at once."""
    t = run_sys(exit_desc(abort=True), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 30, 1, 1)])
    ks = kinds(t, 1)
    assert "BUS" in ks and "ABORT" not in ks and not t.engines[1].violations
    pcs = [pc for pc, _ in t.engines[1].starts]
    assert pcs[:2] == [0, 1] and pcs[2] == 5  # START, HOLD (cut), then abort_pc (after ROLE_IF,
    top = conds(t, 1)[-1]                      # RAW DELAY, HALT): the followed START
    assert top.cycle == dict(t.engines[1].starts)[5]  # completed in its start cycle


def test_without_abort_a_flag_does_nothing():
    t = run_sys(exit_desc(abort=False), roles=(Role.P0, Role.P1), glitches=[Glitch(0, 30, 1, 1)])
    assert "BUS" not in kinds(t, 1) and [pc for pc, _ in t.engines[1].starts][:3] == [0, 1, 2]
    assert 5 not in [pc for pc, _ in t.engines[1].starts]


# ---------------------------------------------------------------- SELECT
def test_select_start_and_stop():
    """SELECT: CS falling is START, rising is STOP, confirmed after 1 cycle (the default)."""
    d = Description("sel", parties=("a", "b"))
    cs = d.line("cs", owner="a", idle=1)
    d.line("miso", owner="b", idle=1)
    d.bus("select", cs)
    d.cond("start", owner="a", prof=d.profile("P", nom=NOM, min=0, max=INF))
    d.cond("stop", owner="a", prof=d.profile("W", nom=30, min=0, max=INF))
    d.halt()
    t = run_sys(compile_desc(d, check_timing=False), roles=(Role.P0, Role.P1))
    (s_do, e_do), (s_f, e_f) = conds(t, 0), conds(t, 1)
    assert (s_f.tb, s_f.cycle) == (s_do.cycle, s_do.cycle + EDGE_LAT + 1 + 1)
    assert (e_f.tb, e_f.cycle) == (e_do.cycle, e_do.cycle + EDGE_LAT + 1 + 1)


# ---------------------------------------------------------------- MEAS
def meas_desc(maxv):
    d = Description("m", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("P", nom=NOM, min=0, max=maxv), meas="r3")
    d.push("r3")
    d.halt()
    return compile_desc(d, check_timing=False)


def test_cond_meas_in_every_role():
    """MEAS (isa §4.2): rd := min(max(m, 0), 0xFFFF). The follower's m = e - tb, the owner's
    action - tb: 8 in every role."""
    t = run_sys(meas_desc(INF))
    assert [t.engines[i].rx for i in range(3)] == [[NOM]] * 3


def test_cond_meas_timeout_writes_ffff():
    t = run_sys(meas_desc(20), roles=(Role.P1,))
    assert "TIMEOUT" in kinds(t, 0) and t.engines[0].rx == [0xFFFF]


# ---------------------------------------------------------------- compiler rules
def test_start_is_owned_by_the_master_line_owner():
    with pytest.raises(CompileError, match="R1-bus"):
        i2c(lambda d, sda, scl, p: d.cond("start", owner="b", prof=p))


def test_start_needs_a_bus():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a")
    d.cond("start", owner="a", prof=d.profile("P", nom=NOM))
    d.halt()
    with pytest.raises(CompileError, match="S-bus"):
        compile_desc(d, check_timing=False)


def test_i2c_bus_lines_are_od():
    d = Description("t", parties=("a", "b"))
    sda = d.line("sda", owner="a")
    scl = d.line("scl", "od", owner="a")
    d.bus("i2c", sda, scl)
    d.halt()
    with pytest.raises(CompileError, match="S-bus"):
        compile_desc(d, check_timing=False)


def test_meas_taints_its_register():
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    d.cond("fall", ln, owner="a", prof=d.profile("P", nom=NOM, max=INF), meas="r3")
    d.br("x", cond="z", reg="r3")
    d.label("x")
    d.halt()
    with pytest.raises(CompileError, match="R2-branch"):
        compile_desc(d, check_timing=False)


def test_a_followed_start_clears_a_stale_stop():
    """START clears both flags (isa §4.2). P1 arrives late, after the master's START, STOP and
    START: both flags are set. Its COND START completes at once and clears the STOP flag too, so
    its COND STOP waits for the master's next real STOP (200 cycles on) instead of completing on
    the stale one."""
    def body(d, sda, scl, p):
        w = d.profile("W", nom=40, min=0, max=INF)
        with d.only("b"):
            d.raw_delay(150)
        d.cond("start", owner="a", prof=p)
        d.cond("stop", owner="a", prof=w)
        d.cond("start", owner="a", prof=w)
        d.cond("stop", owner="a", prof=d.profile("L", nom=200, min=0, max=INF))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1))
    master, late = conds(t, 0), conds(t, 1)
    assert [k for _, k in t.engines[1].bus_events][:3] == ["START", "STOP", "START"]
    assert late[0].tb == master[2].cycle       # its START is the second one, at once
    assert late[1].tb == master[3].cycle       # its STOP is the master's last one


def test_an_sda_edge_as_scl_rises_is_no_start():
    """I2C: the candidate needs SCL read 1 now and in the previous cycle. SCL is held low until
    the very tick SDA falls, so P1 sees both change in one cycle: no START."""
    def body(d, sda, scl, p):
        d.cond("start", owner="a", prof=d.profile("S", nom=NOM, min=0, max=60))
    t = run_sys(i2c(body), roles=(Role.P0, Role.P1), glitches=[Glitch(1, 3, NOM + 1 - 3, 0)])
    assert t.wire_edges[1] == [3, NOM + 1]   # SCL low from 3, back high with SDA's fall at 9
    assert not t.engines[1].bus_events and "TIMEOUT" in kinds(t, 1)
