"""R2-edge (isa §5): a COND its owner does drives the post-edge level (isa §4.2), so the owner must
leave the line at the pre-edge level on every path, or the others wait for an edge that never comes.
Found by the random programs (emu/gen): a clocked XFER whose last bit is 0, then a UART-style
start bit on the same line, compiled with every rule on; the followers hung or misread silently."""
import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import INF, Role
from emu.model.system import EngineSpec, System
from emu.protocols import PROTOCOLS, slack

P = 20


def desc(between, src="r5"):
    """b sends a UART-style frame on d from `src` (start bit, data, then `between`), then a second
    frame's start bit on d."""
    d = Description("e", parties=("a", "b"))
    dl = d.line("d", owner="b", idle=1)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r5", 0)
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src=src, dst="r2")
    between(d, dl, bit)
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src="ones", dst="r1")
    d.hold(dl, 1, owner="b", prof=bit)
    d.halt()
    return d


def clocked():
    """The random programs' finding: b's clocked XFER from a register, then a start bit on its data line."""
    d = Description("e", parties=("a", "b"))
    dl = d.line("d", owner="b", idle=1)
    clk = d.line("clk", owner="a", idle=0)
    lane = d.lane("L", dl, clk=clk, cpol=0, cpha=1)
    half = d.profile("HALF", nom=16, min=0, max=INF)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    d.li("r5", 0)
    d.xfer(1, lane, owner="b", prof=half, src="r5")
    d.cond("fall", dl, owner="b")
    d.hold(dl, 0, owner="b", prof=bit)
    d.xfer(1, dl, owner="b", prof=bit, src="ones", dst="r1")
    d.hold(dl, 1, owner="b", prof=bit)
    d.halt()
    return d


def nothing(d, dl, bit):
    pass


def stop_bit(d, dl, bit):
    d.hold(dl, 1, owner="b", prof=bit)


def rules(d):
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    return {x.rule for x in e.value.diags}


def test_a_start_bit_after_data_of_unknown_level_is_rejected():
    assert "R2-edge" in rules(desc(nothing))


def test_a_start_bit_after_a_clocked_xfer_is_rejected():
    assert "R2-edge" in rules(clocked())


def test_the_rejected_program_really_hangs_the_followers():
    """What the rule prevents, on the model: b's last bit is 0, so its COND FALL makes no edge."""
    img = compile_desc(clocked(), check_levels=False)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(50_000)
    assert t.engines[1].halted and not t.engines[0].halted and not t.engines[2].halted


def test_a_stop_bit_first_makes_it_legal():
    compile_desc(desc(stop_bit))


@pytest.mark.parametrize("src, ok", [("ones", True), ("zero", False)])
def test_a_constant_source_leaves_a_known_level(src, ok):
    if ok:
        compile_desc(desc(nothing, src=src))
    else:
        assert "R2-edge" in rules(desc(nothing, src=src))


def test_a_path_that_skips_the_stop_bit_is_rejected():
    def branchy(d, dl, bit):
        d.br("skip", cond="nz", reg="r2")
        d.hold(dl, 1, owner="b", prof=bit)
        d.label("skip")
        d.nop()
    assert "R2-edge" in rules(desc(branchy))


def test_a_select_start_needs_cs_high():
    d = Description("s", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    sck = d.line("sck", owner="ctl", idle=0)
    mosi = d.line("mosi", owner="ctl", idle=1)
    d.bus("select", cs)
    lane = d.lane("L", mosi, clk=sck)
    half = d.profile("HALF", nom=10, min=0, max=INF)
    wait = d.profile("WAIT", nom=10, min=0, max=INF)
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones")
    d.cond("start", owner="ctl", prof=wait)  # CS is already low: no START edge
    d.halt()
    assert "R2-edge" in rules(d)


@pytest.mark.parametrize("name", sorted(PROTOCOLS))
def test_the_library_protocols_pass(name):
    compile_desc(PROTOCOLS[name].build())

# ---- every party's drive (review of the first R2-edge): each compiled with every rule on, and on
# the model a follower hung or logged a violation
def bitprof(d):
    s = slack(P, 20000)
    return d.profile("BIT", nom=P, min=P - s, max=P + s)


# A: shared OD line, b HOLD 0 then a COND FALL
def A():
    d = Description("A", parties=("a", "b"))
    s_ = d.line("s", "od", owner="shared", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.cond("fall", s_, owner="b", prof=w)
    d.hold(s_, 0, owner="b", prof=bit)
    d.cond("fall", s_, owner="a", prof=w)   # b still pulls low: no edge
    d.hold(s_, 1, owner="a", prof=bit)
    d.halt()
    return d

# B: step owner "shared" HOLD 0, then a's COND FALL
def B():
    d = Description("B", parties=("a", "b"))
    s_ = d.line("s", "od", owner="shared", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    d.cond("fall", s_, owner="a", prof=w)
    d.hold(s_, 1, owner="a", prof=bit)
    d.hold(s_, 0, owner="shared", prof=bit)  # both parties pull low
    d.cond("fall", s_, owner="a", prof=w)
    d.hold(s_, 1, owner="a", prof=bit)
    d.halt()
    return d

# C: non-owner RAW SET 0 on an OD line, then owner's COND FALL
def C():
    d = Description("C", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=1)
    bit = bitprof(d)
    w = d.profile("W", nom=P, min=0, max=INF)
    with d.only("b"):
        d.raw_set(x, 0)
    d.cond("fall", x, owner="a", prof=w)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    return d

# D: gated MISO: per HOLD 0, ctl STOP releases it, per COND RISE makes no edge
def D():
    d = Description("D", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("start", owner="ctl", prof=w)
    d.hold(miso, 0, owner="per", prof=bit)
    d.cond("stop", owner="ctl", prof=w)
    d.cond("rise", miso, owner="per", prof=w)
    d.halt()
    return d

# E: I2C START with SCL low
def E():
    d = Description("E", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    d.lane("L", sda, clk=scl, cpol=1, cpha=1)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.cond("fall", scl, owner="ctl", prof=w)
    d.cond("start", owner="ctl", prof=w)     # SDA falls with SCL low: no START
    d.cond("rise", scl, owner="ctl", prof=w)
    d.halt()
    return d

# F: I2C: target's ACK let-go holds SDA low; controller's START right after
def F():
    d = Description("F", parties=("ctl", "tgt"))
    sda = d.line("sda", "od", owner="shared", idle=1)
    scl = d.line("scl", "od", owner="ctl", idle=1)
    d.bus("i2c", sda, scl)
    lane = d.lane("L", sda, clk=scl, cpol=1, cpha=1, ldelay=2)
    half = d.profile("HALF", nom=20, min=0, max=INF)
    w = d.profile("W", nom=40, min=0, max=INF)
    d.cond("start", owner="ctl", prof=w)
    d.xfer(8, lane, owner="ctl", prof=half, src="ones", msb_first=True)
    d.xfer(1, lane, owner="tgt", prof=half, src="zero")   # ACK: tgt holds SDA low until SCL falls
    d.cond("start", owner="ctl", prof=w)                  # ctl's own level is 1, the wire 0
    d.xfer(8, lane, owner="ctl", prof=half, src="ones", msb_first=True)
    d.halt()
    return d

# G: OD line with idle 0: released at RUN, so it reads 1; owner's COND RISE first
def G():
    d = Description("G", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=0)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("rise", x, owner="a", prof=w)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    return d

# H: RAW SET on a gated line while the gate is shut (RUN), then COND RISE
def H():
    d = Description("H", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    w = d.profile("W", nom=P, min=0, max=INF)
    with d.only("per"):
        d.raw_set(miso, 0)     # gate shut: does nothing (isa §2)
    d.cond("rise", miso, owner="per", prof=w)
    d.halt()
    return d


@pytest.mark.parametrize("build", [A, B, C, D, E, F, G, H], ids=lambda f: f.__name__)
def test_r2_edge_sees_every_partys_drive(build):
    """A: b still pulls a shared OD line low. B: a SHARED HOLD 0. C: a RAW SET 0 by the other party.
    D: a SELECT STOP releases a gated line. E: an I2C START with SCL low. F: the target's ACK
    let-go still holds SDA. G: an OD line with idle 0 starts released, at 1. H: RAW SET on a gated
    line while its gate is shut does nothing."""
    assert "R2-edge" in rules(build())



def runs_clean(d):
    """Accepted, and on the model every role halts with no violation."""
    from emu.model.engine import VIOLATIONS
    img = compile_desc(d)
    t = System([EngineSpec(img, r) for r in (Role.P0, Role.P1, Role.MON)]).run(100_000)
    assert all(et.halted for et in t.engines) and t.quiescent
    assert [e.kind for et in t.engines for e in et.events if e.kind in VIOLATIONS] == []


def test_an_od_line_with_idle_0_starts_released():
    """It reads 1 at RUN, so a COND FALL first is legal (and G's RISE first is not)."""
    d = Description("g2", parties=("a", "b"))
    x = d.line("x", "od", owner="a", idle=0)
    w = d.profile("W", nom=P, min=0, max=INF)
    bit = bitprof(d)
    d.cond("fall", x, owner="a", prof=w)
    d.hold(x, 0, owner="a", prof=bit)
    d.hold(x, 1, owner="a", prof=bit)
    d.halt()
    runs_clean(d)


@pytest.mark.parametrize("build", [lambda: desc(stop_bit), lambda: desc(nothing, src="ones")],
                         ids=["stop bit", "ones"])
def test_the_accepted_cases_run_clean(build):
    runs_clean(build())
