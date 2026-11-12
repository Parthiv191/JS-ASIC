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
