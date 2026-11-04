import pytest

from emu.compiler import CompileError, compile_desc
from emu.desc import Description
from emu.isa import Op, Owner, unpack_role_if


def rules(desc, **kw):
    with pytest.raises(CompileError) as e:
        compile_desc(desc, check_timing=False, **kw)
    return {d.rule for d in e.value.diags}


def base():
    d = Description("t", parties=("a", "b"))
    pp = d.line("pp", owner="a")
    od = d.line("od", kind="od", owner="shared")
    p = d.profile("P", nom=16, min=14, max=18)
    return d, pp, od, p


def test_lowering_role_if_count_and_labels():
    d, pp, od, p = base()
    d.label("top")
    with d.only("a", "mon"):
        d.li("r0", 5)
        d.alu("add", "r0", "r0", 1)
    d.cond("fall", pp, owner="a")
    d.br("top")
    img = compile_desc(d, check_timing=False)
    ins = img.instrs()
    assert [i.op for i in ins] == [Op.ROLE_IF, Op.LI, Op.ALU, Op.COND, Op.BR]
    assert unpack_role_if(ins[0].operand) == (0b101, 2)
    assert ins[4].operand == 0  # "top" labels the role block, i.e. the ROLE_IF
    assert ins[3].owner == Owner.P0 and ins[3].pin == pp.index


def test_r1_rejects_wrong_owner_on_pp_line():
    d, pp, od, p = base()
    d.hold(pp, 0, owner="b", prof=p)
    assert rules(d) == {"R1-owner"}


def test_r1_rejects_shared_on_pp():
    d, pp, od, p = base()
    d.hold(pp, 0, owner="shared", prof=p)
    assert rules(d) == {"R1-shared"}


def test_r1_accepts_shared_on_od():
    d, pp, od, p = base()
    d.hold(od, 0, owner="shared", prof=p)
    compile_desc(d, check_timing=False)


def test_r1_rejects_clock_line_without_party_owner():
    d, pp, od, p = base()
    lane = d.lane("ln", data=pp, clk=od)
    d.xfer(8, lane, owner="a", prof=p)
    assert rules(d) == {"R1-owner"}


def test_r3_rejects_timed_step_in_role_block():
    d, pp, od, p = base()
    with d.only("a"):
        d.hold(pp, 1, owner="a", prof=p)
    assert rules(d) == {"R3-timed-in-role"}


def test_r3_rejects_raw_drive_pp_outside_owner_block():
    d, pp, od, p = base()
    d.raw_set(pp, 0)
    with d.only("b"):
        d.raw_rel(pp)
    with d.only("a"):
        d.raw_set(pp, 1)  # fine: owner's own block
    d.raw_set(od, 0)  # fine: open-drain
    with pytest.raises(CompileError) as e:
        compile_desc(d, check_timing=False)
    assert [(x.rule, x.index) for x in e.value.diags] == [("R3-raw-drive", 0), ("R3-raw-drive", 1)]


def test_r3_rejects_branch_out_of_block():
    d, pp, od, p = base()
    d.label("out")
    d.nop()
    with d.only("a"):
        d.br("out")
    assert rules(d) == {"R3-escape"}


def test_r3_allows_loop_back_to_own_block_head():
    d, pp, od, p = base()
    with d.only("b"):
        d.label("wait")
        d.raw_jpin(pp, 1, "wait")  # "wait" names the block's ROLE_IF
    compile_desc(d, check_timing=False)


def test_r3_rejects_branch_into_block():
    d, pp, od, p = base()
    d.br("inside")
    with d.only("a"):
        d.nop()
        d.label("inside")
        d.nop()
    assert rules(d) == {"R3-escape"}


def test_s_rejects_unresolved_label():
    d, pp, od, p = base()
    d.br("nowhere")
    assert rules(d) == {"S-label"}


def test_s_rejects_17_profiles():
    d, pp, od, p = base()
    for k in range(15):
        d.profile(f"x{k}", nom=4)
    d.nop()
    assert rules(d) == {"S-table"}


def test_s_rejects_out_of_range_fields():
    d, pp, od, p = base()
    d.li("r0", 4096)
    d.xfer(17, pp, owner="a", prof=p)
    assert rules(d) == {"S-range"}


def test_implicit_lanes_are_deduplicated():
    d, pp, od, p = base()
    d.xfer(8, pp, owner="a", prof=p)
    d.xfer(4, pp, owner="a", prof=p)
    img = compile_desc(d, check_timing=False)
    assert len(img.lanes) == 1 and img.lanes[0].data == pp.index and img.lanes[0].clk is None


def test_debug_map_has_every_pc():
    d, pp, od, p = base()
    d.abort_to("top")
    d.label("top")
    with d.only("b"):
        d.pop("r1")
    d.hold(pp, 0, owner="a", prof=p)
    d.halt()
    img = compile_desc(d, check_timing=False)
    assert sorted(img.debug) == list(range(len(img.words)))
    assert "test_compiler.py:" in img.debug[2] and "hold" in img.debug[2]
    assert img.abort_pc == 0


def test_s_rejects_program_over_depth():
    def nops(k):
        d = Description("t", parties=("a", "b"))
        for _ in range(k):
            d.nop()
        return d
    assert rules(nops(65)) == {"S-range"}
    compile_desc(nops(64), check_timing=False)
    compile_desc(nops(65), check_timing=False, prog_depth=128)


def test_encode_failure_without_diagnostic_is_an_internal_error(monkeypatch):
    """compiler.lower must never turn an undiagnosed encode failure into a NOP word."""
    import emu.compiler as compiler

    def boom(ins):
        raise ValueError("field does not fit")
    monkeypatch.setattr(compiler, "encode", boom)
    d, pp, od, p = base()
    d.nop()
    with pytest.raises(AssertionError, match="without a diagnostic"):
        compiler.lower(d)


def test_r3_allows_early_exit_to_block_end():
    """The pc after a block is where every other role resumes, so jumping there is an early exit."""
    d, pp, od, p = base()
    with d.only("a"):
        d.li("r0", 1)
        d.br("exit", cond="z", reg="r0")
        d.nop()
    d.label("exit")
    d.halt()
    compile_desc(d, check_timing=False)


def test_r3_allows_exit_onto_adjacent_block_guard():
    """With adjacent blocks, a's exit pc is b's ROLE_IF, so b's role check still runs."""
    d, pp, od, p = base()
    with d.only("a"):
        d.li("r0", 1)
        d.br("blk_b", cond="z", reg="r0")
    d.label("blk_b")
    with d.only("b"):
        d.nop()
    d.halt()
    compile_desc(d, check_timing=False)


def test_r3_rejects_branch_into_another_blocks_body():
    d, pp, od, p = base()
    with d.only("a"):
        d.li("r0", 1)
        d.br("in_b", cond="z", reg="r0")
    with d.only("b"):
        d.nop()
        d.label("in_b")
        d.nop()
    d.halt()
    assert rules(d) == {"R3-escape"}


# ---------------------------------------------------------------- audit follow-ups
def test_r3_rejects_abort_target_inside_a_role_block():
    d = Description("t", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.abort_to("inside")
    with d.only("a"):
        d.nop()
        d.label("inside")
        d.nop()
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d, check_timing=False)
    assert {x.rule for x in e.value.diags} == {"R3-escape"}


def test_s_rejects_abort_on_unclocked_xfer_without_check():
    """An out-of-window edge (CHK) is the only fatal failure of an unclocked XFER."""
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    d.abort_to("end")
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p)
    d.xfer(8, ln, owner="a", prof=p, abort=True)
    d.label("end")
    d.halt()
    with pytest.raises(CompileError) as e:
        compile_desc(d)
    assert {x.rule for x in e.value.diags} == {"S-flags"}


def test_abort_without_abort_to_warns():
    from emu.compiler import CompileWarning
    d = Description("t", parties=("a", "b"))
    ln = d.line("l", owner="a", idle=1)
    p = d.profile("P", nom=16, min=14, max=18)
    d.cond("fall", ln, owner="a")
    d.hold(ln, 0, owner="a", prof=p, abort=True)
    d.halt()
    with pytest.warns(CompileWarning, match="abort_to"):
        compile_desc(d)
