"""The fuzzer instructions in the compiler and the engine (isa §4.6, §5 R3-fz; bus-fuzzer.md §3).

The fuzz unit itself (emu/fuzz/unit.py) has its own tests; these check what the engine does with
it: the ops' fixed costs, src = FZ, the auto-tokens and event mask while armed, and the hang."""
import pytest

from emu.analysis import _cost
from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.fuzz.devices import ARM_MARK, tokens_of
from emu.fuzz.unit import EVENT_BIT, FuzzConfig, FuzzUnit, bucket
from emu.isa import (FZ_END_CYCLES, FZ_HANG_CYCLES, FZ_NEXT_CYCLES, FZ_TOK_CYCLES, INF, FzOp, Instr,
                     Op, Role, Src, unpack_xfer)
from emu.model.system import EngineSpec, System
from emu.protocols import slack

P = 20
SEED = bytes([0xA4])


def unit(seed=SEED, **cfg):
    u = FuzzUnit(FuzzConfig(length=len(seed), **cfg))
    u.load(0, seed)
    return u


def frame(d, line, owner, src, dst, bit):
    """A UART-style frame: start bit, 8 data bits, stop bit."""
    d.cond("fall", line, owner=owner)
    d.hold(line, 0, owner=owner, prof=bit)
    d.xfer(8, line, owner=owner, prof=bit, src=src, dst=dst)
    d.hold(line, 1, owner=owner, prof=bit)


def fz_desc(reply=True, end_before_reply=False, wait=None, abort=False):
    """ctl: fz.next, sends the staged byte; tgt echoes it inverted; ctl: fz.end r4.
    `wait` bounds ctl's wait for the reply (TIMEOUT); `abort` makes that wait's TIMEOUT fatal."""
    d = Description("fz", parties=("ctl", "tgt"))
    tx = d.line("tx", owner="ctl", idle=1)
    rx = d.line("rx", owner="tgt", idle=1)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    if abort:
        d.abort_to("fail")
    with d.only("ctl"):
        d.fz_next("r1")
        d.mark(ARM_MARK)  # where devOps's stage-1 model puts fz.next: tokens_of counts from here
    frame(d, tx, "ctl", "fz", "r2", bit)
    if end_before_reply:
        with d.only("ctl"):
            d.fz_end("r4")
    if reply:
        with d.only("tgt"):
            d.alu("xor", "r2", "r2", 0xFF)
        if wait is None:
            frame(d, rx, "tgt", "r2", "r3", bit)
        else:
            w = d.profile("WAIT", nom=wait, min=0, max=wait)
            d.cond("fall", rx, owner="tgt", prof=w, abort=abort)
            d.hold(rx, 0, owner="tgt", prof=bit)
            d.xfer(8, rx, owner="tgt", prof=bit, src="r2", dst="r3")
            d.hold(rx, 1, owner="tgt", prof=bit)
    if not end_before_reply:
        with d.only("ctl"):
            d.fz_end("r4")
    d.halt()
    if abort:
        d.label("fail")
        d.mark(7)
        d.halt()
    return d


def run(img, fuzz=None, engine=0, peer_img=None, peers=True, max_ticks=20_000):
    specs = [EngineSpec(img, Role.P0, engine=engine, fuzz=fuzz)]
    if peers:
        specs.append(EngineSpec(peer_img or img, Role.P1, engine=1))
    return System(specs).run(max_ticks)


def start_of(et, op_pc):
    return next(t for pc, t in et.starts if pc == op_pc)


def pcs_of(img, op, sub=None):
    return [pc for pc, ins in enumerate(img.instrs()) if ins.op == op and (sub is None or ins.sub == sub)]


# ---- the compiler -------------------------------------------------------------------------
def test_fz_ops_lower_to_op_fz():
    img = compile_desc(fz_desc())
    ins = img.instrs()
    nxt, = pcs_of(img, Op.FZ, FzOp.NEXT)
    end, = pcs_of(img, Op.FZ, FzOp.END)
    assert (ins[nxt].rd, ins[end].rd) == (1, 4)
    sends = [i for i in ins if i.op == Op.XFER and unpack_xfer(i.operand)[1] == Src.FZ]
    assert len(sends) == 1


def test_fz_tok_lowers_with_its_register():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.fz_tok("r5")
    d.halt()
    tok, = [i for i in compile_desc(d).instrs() if i.op == Op.FZ]
    assert (tok.sub, tok.ra) == (FzOp.TOK, 5)


def r3_fz(build) -> list[str]:
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    s = slack(P, 20000)
    bit = d.profile("BIT", nom=P, min=P - s, max=P + s)
    build(d, ln, bit)
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    return [x.rule for x in e.value.diags]


def test_r3_fz_outside_a_role_block():
    assert "R3-fz" in r3_fz(lambda d, ln, bit: d.fz_next("r1"))


def test_r3_fz_in_a_block_for_two_parties():
    def build(d, ln, bit):
        with d.only("a", "b"):
            d.fz_next("r1")
    assert "R3-fz" in r3_fz(build)


def test_r3_fz_in_a_block_for_mon():
    def build(d, ln, bit):
        with d.only("mon"):
            d.fz_next("r1")
    assert "R3-fz" in r3_fz(build)


def test_r3_fz_for_two_parties_in_two_blocks():
    def build(d, ln, bit):
        with d.only("a"):
            d.fz_next("r1")
        with d.only("b"):
            d.fz_end("r1")
    assert "R3-fz" in r3_fz(build)


def test_r3_fz_src_fz_sent_by_another_party():
    def build(d, ln, bit):
        with d.only("b"):
            d.fz_next("r1")
        frame(d, ln, "a", "fz", "r2", bit)
    assert "R3-fz" in r3_fz(build)


def test_r3_fz_allows_the_duplex_data2_sender():
    d = Description("t", parties=("ctl", "per"))
    sck = d.line("sck", owner="ctl", idle=0)
    mosi = d.line("mosi", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1)
    lane = d.lane("L", mosi, clk=sck, cpol=0, cpha=0, data2=miso)
    half = d.profile("HALF", nom=10, min=0, max=INF)
    with d.only("per"):
        d.fz_next("r1")
    d.xfer(8, lane, owner="ctl", prof=half, src="fz", dst="r2")
    with d.only("per"):
        d.fz_end("r1")
    d.halt()
    compile_desc(d, check_timing=False)  # R3-fz only: per's fz.next makes it late for the first edge


def test_fz_costs_in_the_timing_analysis():
    costs = {s: _cost(Instr(Op.FZ, sub=int(s))) for s in FzOp}
    assert costs == {FzOp.NEXT: FZ_NEXT_CYCLES, FzOp.TOK: FZ_TOK_CYCLES, FzOp.END: FZ_END_CYCLES}
    assert (FZ_NEXT_CYCLES, FZ_TOK_CYCLES, FZ_END_CYCLES, FZ_HANG_CYCLES) == (48, 2, 40, 12)


# ---- the engine ---------------------------------------------------------------------------
@pytest.mark.parametrize("fuzz", [True, False], ids=["unit", "no unit"])
def test_fz_ops_take_their_fixed_costs(fuzz):
    img = compile_desc(fz_desc())
    et = run(img, unit() if fuzz else None).engines[0]
    nxt, = pcs_of(img, Op.FZ, FzOp.NEXT)
    end, = pcs_of(img, Op.FZ, FzOp.END)
    assert start_of(et, nxt + 1) - start_of(et, nxt) == FZ_NEXT_CYCLES
    assert start_of(et, end + 1) - start_of(et, end) == FZ_END_CYCLES


def test_fz_tok_takes_2_cycles_and_queues_a_token():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    with d.only("a"):
        d.fz_next("r1")
        d.li("r5", 0x1C3)
        d.fz_tok("r5")
        d.fz_end("r6")
    d.halt()
    img = compile_desc(d)
    u = unit()
    et = run(img, u, peers=False).engines[0]
    tok, = pcs_of(img, Op.FZ, FzOp.TOK)
    assert start_of(et, tok + 1) - start_of(et, tok) == FZ_TOK_CYCLES
    assert u.tokens == [0xC3]


def test_src_fz_sends_the_staged_byte():
    img = compile_desc(fz_desc())
    u = unit()
    t = run(img, u)
    assert t.engines[0].regs[1] == 1                    # fz.next: rd := the length
    assert t.engines[1].regs[2] == u.staged()[0] ^ 0xFF  # the target got it (and inverted it)


def test_src_fz_without_a_unit_sends_0():
    t = run(compile_desc(fz_desc()))
    assert t.engines[0].regs[1] == 0 and t.engines[1].regs[2] == 0xFF and t.engines[0].regs[4] == 0


def test_auto_tokens_match_the_stage_1_reading():
    """The followed COND's latency, then the followed XFER's byte: what devOps's tokens_of reads
    from the same trace (bus-fuzzer §3)."""
    img = compile_desc(fz_desc())
    u = unit()
    t = run(img, u)
    want = tokens_of(img, t, 0).tokens
    assert len(want) == 2 and want[0] & 0xF0 == 0x40 and want[1] == u.staged()[0] ^ 0xFF
    assert tuple(u.tokens) == want


def test_no_tokens_after_fz_end():
    u = unit()
    run(compile_desc(fz_desc(end_before_reply=True), check_timing=False), u)
    assert u.tokens == []


def test_fz_costs_reach_the_timing_rules():
    """fz.end's 40 cycles before the reply: ctl would start watching for its start bit too late."""
    with pytest.raises(CompileError) as e:
        compile_desc(fz_desc(end_before_reply=True))
    assert "T5-watch" in {x.rule for x in e.value.diags}
    compile_desc(fz_desc())  # with fz.end after the reply, it compiles


def test_fz_end_reports_new_then_not_new():
    img = compile_desc(fz_desc())
    u = unit()
    assert run(img, u).engines[0].regs[4] == 1     # a fresh bitmap: new
    u2 = unit()
    u2.bitmap = bytearray(b"\xff" * len(u2.bitmap))
    assert run(img, u2).engines[0].regs[4] == 0    # every bit set: not new


def test_a_timeout_while_armed_sets_its_event_bit():
    """No reply within the wait: TIMEOUT without ABORT is not fatal; its bit is in the mask and the
    COND's latency token is 0x40 + bucket(0xFFFF)."""
    img = compile_desc(fz_desc(wait=200), check_timing=False)
    u = unit()
    run(img, u, peers=False)
    assert u.events & 1 << EVENT_BIT["TIMEOUT"]
    assert u.tokens[0] == 0x40 + bucket(0xFFFF)
    assert u.stats.hangs == 0 and not u.armed  # fz.end ran


def test_a_fatal_abort_while_armed_is_a_hang_and_costs_12_more():
    img = compile_desc(fz_desc(wait=200, abort=True), check_timing=False)
    fail = img.abort_pc
    with_unit = unit()
    et = run(img, with_unit, peers=False).engines[0]
    plain = run(img, None, peers=False).engines[0]
    assert with_unit.stats.hangs == 1 and with_unit.log[-1].kind == "hang"
    assert not with_unit.armed and with_unit.pinned[with_unit.log[-1].slot]
    assert start_of(et, fail) - start_of(plain, fail) == FZ_HANG_CYCLES


def test_an_abort_while_disarmed_is_not_a_hang():
    img = compile_desc(fz_desc(wait=200, abort=True, end_before_reply=True), check_timing=False)
    u = unit()
    et = run(img, u, peers=False).engines[0]
    plain = run(img, None, peers=False).engines[0]
    assert u.stats.hangs == 0
    assert start_of(et, img.abort_pc) == start_of(plain, img.abort_pc)


def test_fz_on_engine_1_is_illegal():
    img = compile_desc(fz_desc())
    et = run(img, None, engine=1, peers=False).engines[0]
    nxt, = pcs_of(img, Op.FZ, FzOp.NEXT)
    assert [e.kind for e in et.events] == ["ILLEGAL"] and et.halt_cycle == start_of(et, nxt)


def test_the_unit_belongs_to_engine_0():
    with pytest.raises(ValueError):
        EngineSpec(compile_desc(fz_desc()), Role.P0, engine=1, fuzz=unit())


def spi_base():
    d = Description("x", parties=("ctl", "per"))
    cs = d.line("cs", owner="ctl", idle=1)
    sck = d.line("sck", owner="ctl", idle=0)
    mosi = d.line("mosi", owner="ctl", idle=1)
    miso = d.line("miso", owner="per", idle=1, gated=True)
    d.bus("select", cs)
    lane = d.lane("L", mosi, clk=sck, cpol=0, cpha=0, data2=miso)
    half = d.profile("HALF", nom=10, min=0, max=INF)
    wait = d.profile("WAIT", nom=10, min=0, max=INF)
    return d, lane, half, wait


def bus_exit_pair():
    """The peripheral fuzzes (engine 0); the controller (engine 1) raises CS after 4 of the 8
    bits, so the peripheral's XFER ends in a bus exit to abort_pc, where it runs fz.end."""
    d, lane, half, wait = spi_base()
    d.raw_delay(120)  # past the peripheral's fz.next
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(4, lane, owner="ctl", prof=half, src="zero")
    d.cond("stop", owner="ctl", prof=wait)
    d.halt()
    ctl = compile_desc(d, check_timing=False)
    d, lane, half, wait = spi_base()
    d.abort_to("out")
    with d.only("per"):
        d.fz_next("r1")
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="fz", dst="r2", abort=True)
    d.cond("stop", owner="ctl", prof=wait)
    d.halt()
    d.label("out")
    d.mark(5)
    with d.only("per"):
        d.fz_end("r4")
    d.halt()
    return ctl, compile_desc(d, check_timing=False)


def run_pair(ctl, per, fuzz):
    return System([EngineSpec(per, Role.P1, engine=0, fuzz=fuzz),
                   EngineSpec(ctl, Role.P0, engine=1)]).run(20_000).engines[0]


def test_a_bus_exit_while_armed_is_not_a_hang():
    ctl, per = bus_exit_pair()
    u = unit()
    et = run_pair(ctl, per, u)
    plain = run_pair(ctl, per, None)
    assert "BUS" in [e.kind for e in et.events] and "ABORT" not in [e.kind for e in et.events]
    assert u.stats.hangs == 0 and u.events & 1 << EVENT_BIT["BUS"]  # armed until fz.end
    assert et.regs[4] == 1                                            # fz.end ran: new
    assert start_of(et, per.abort_pc) == start_of(plain, per.abort_pc)  # no FZ_HANG_CYCLES


def test_r2_fz_next_taints_its_register():
    """rd is the fuzzing party's alone (the length, or 0 elsewhere): no branch on it outside the block."""
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.li("r1", 0)  # the same in every role, until fz.next writes it
    with d.only("a"):
        d.fz_next("r1")
    d.br("x", cond="nz", reg="r1")
    d.label("x")
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    assert "R2-branch" in {x.rule for x in e.value.diags}


def test_a_duplex_xfer_gives_two_tokens():
    """The fuzzing peripheral follows a duplex XFER: (MISO << 8) | MOSI gives two tokens, low byte
    first, as the stage-1 reading has it; the followed START and STOP give one each."""
    d, lane, half, wait = spi_base()
    with d.only("per"):
        d.fz_next("r1")
        d.mark(ARM_MARK)
    with d.only("ctl"):
        d.raw_delay(120)  # past the peripheral's fz.next
    d.cond("start", owner="ctl", prof=wait)
    d.xfer(8, lane, owner="ctl", prof=half, src="fz", dst="r2")
    d.cond("stop", owner="ctl", prof=wait)
    with d.only("per"):
        d.fz_end("r4")
    d.halt()
    img = compile_desc(d, check_timing=False)
    u = unit()
    t = System([EngineSpec(img, Role.P1, engine=0, fuzz=u), EngineSpec(img, Role.P0, engine=1)]).run(20_000)
    assert t.engines[0].regs[2] == u.staged()[0] << 8   # ctl's src = fz read 0: no unit on engine 1
    want = tokens_of(img, t, 0).tokens
    assert len(want) == 4 and want[1:3] == (0x00, u.staged()[0])
    assert tuple(u.tokens) == want
