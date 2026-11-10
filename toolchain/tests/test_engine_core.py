import pytest

from emu.compiler import compile_desc
from emu.desc import Description
from emu.image import LineCfg
from emu.isa import Owner, Role
from emu.model.bus import resolve
from emu.model.engine import Engine


def img(build, lines=(("l", "pp", "a", 1),)):
    d = Description("t", parties=("a", "b"))
    refs = [d.line(n, kind=k, owner=o, idle=i) for n, k, o, i in lines]
    build(d, *refs)
    return compile_desc(d, check_timing=False)


def run(engine, cycles, wire=lambda c: [1] * 8):
    for c in range(cycles):
        if engine.halted:
            break
        engine.cycle(wire(c))
    return engine


def test_instruction_0_starts_at_now_2():
    """isa §4.0: s0 sets now := 0 and presents address 0; the word lands 2 cycles later."""
    def b(d, l):
        d.nop()
        d.halt()
    e = run(Engine(img(b), Role.P0), 20)
    assert e.starts == [(0, 2), (1, 4)]
    assert e.halt_cycle == 4


def test_untimed_ops_take_U_cycles():
    """li, 3 x (add, eq, br), halt: 11 instructions, U = 2 apart from now = 2, so HALT starts at
    2 + 2*10 = 22."""
    def b(d, l):
        d.li("r0", 0)
        d.label("loop")
        d.alu("add", "r0", "r0", 1)
        d.alu("eq", "r1", "r0", 3)
        d.br("loop", cond="z", reg="r1")
        d.halt()
    e = run(Engine(img(b), Role.P0), 50)
    assert e.halted and e.halt_cycle == 22
    assert [c for _, c in e.starts] == list(range(2, 24, 2))
    assert e.regs[0] == 3
    assert [x.taken for x in e.branches] == [True, True, False]


def test_taken_branch_and_role_if_skip_cost_U():
    """A taken branch costs U like any untimed instruction. A ROLE_IF costs U whether or not it
    skips, and the skipped words take no time (isa §4.0)."""
    def jump(d, l):
        d.br("t")
        d.nop()
        d.nop()
        d.label("t")
        d.halt()
    assert run(Engine(img(jump), Role.P0), 20).starts == [(0, 2), (3, 4)]

    def skip(d, l):
        with d.only("b"):
            d.nop()
            d.nop()
        d.halt()
    assert run(Engine(img(skip), Role.P0), 20).starts == [(0, 2), (3, 4)]


@pytest.mark.parametrize("n,gap", [(2, 2), (3, 4), (4, 4), (5, 6)])
def test_raw_delay_rounds_up_to_U(n, gap):
    """RAW DELAY n takes n cycles rounded up to a multiple of U = 2 (isa §4.0)."""
    def b(d, l):
        d.raw_delay(n)
        d.halt()
    e = run(Engine(img(b), Role.P0), 30)
    assert e.starts == [(0, 2), (1, 2 + gap)]


def test_pop_retries_on_own_cycles():
    """POP starts at 2 and retries on own cycles (even now). Data that first exists at now = 5 is
    taken at 6, so HALT starts at 8 (isa §4.0 stalls)."""
    def b(d, l):
        d.pop("r0")
        d.halt()
    e = Engine(img(b), Role.P0)
    for _ in range(6):        # cycles now = 0..5
        e.cycle([1] * 8)
    assert e.now == 5 and not e.halted
    e.tx.append(0x55)         # visible from now = 6 on
    run(e, 10)
    assert e.starts == [(0, 2), (1, 8)]
    assert e.regs[0] == 0x55


def test_alu_ops_wrap_16_bits():
    def b(d, l):
        d.li("r0", 0xFFF)
        d.alu("shl", "r0", "r0", 4)      # 0xFFF0
        d.alu("add", "r0", "r0", 0x20)   # wraps to 0x0010
        d.alu("sub", "r1", "r1", 1)      # 0 - 1 -> 0xFFFF
        d.alu("lt", "r2", "r0", 0x11)    # 0x10 < 0x11 -> 1
        d.alu("xor", "r3", "r1", 0xFFF)  # 0xF000
        d.alu("shr", "r3", "r3", 12)     # 0xF
        d.alu("or", "r3", "r3", "r2")    # register operand: 0xF | 1
        d.halt()
    e = run(Engine(img(b), Role.P0), 20)
    assert e.regs == [0x0010, 0xFFFF, 1, 0xF, 0, 0, 0, 0]


def test_all_eight_registers_and_li_imm16():
    """isa §1/§3: r0-r7 are addressable (3-bit fields) and LI loads 16 bits ({sub, operand})."""
    def b(d, l):
        d.li("r7", 0xBEEF)
        d.alu("add", "r4", "r7", 1)
        d.alu("or", "r5", "r4", "r7")    # register operand from r7: rb is 3 bits
        d.push("r6")
        d.pop("r6")
        d.halt()
    e = Engine(img(b), Role.P0)
    e.tx.append(0x1234)
    run(e, 20)
    assert e.regs == [0, 0, 0, 0, 0xBEF0, 0xBEFF, 0x1234, 0xBEEF]
    assert e.rx == [0]


def test_role_if_skips_per_role():
    def b(d, l):
        with d.only("a"):
            d.li("r0", 1)
        with d.only("mon"):
            d.li("r1", 1)
            d.li("r2", 1)
        d.halt()
    image = img(b)
    got = {r: run(Engine(image, r), 20) for r in Role}
    # U = 2 per instruction from now = 2; skipped words take no time.
    # P0: ROLE_IF, LI, ROLE_IF (skip), HALT -> 8. P1: two skips, HALT -> 6. MON: skip, ROLE_IF, 2 LI, HALT -> 10.
    assert got[Role.P0].regs[:3] == [1, 0, 0] and got[Role.P0].halt_cycle == 8
    assert got[Role.P1].regs[:3] == [0, 0, 0] and got[Role.P1].halt_cycle == 6
    assert got[Role.MON].regs[:3] == [0, 1, 1] and got[Role.MON].halt_cycle == 10


def test_pop_blocks_until_push():
    def b(d, l):
        d.pop("r0")
        d.push("r0")
        d.halt()
    e = Engine(img(b), Role.P1)
    run(e, 5)                                   # now 0..4: POP starts at 2, finds nothing at 2 and 4
    assert e.pc == 0 and e.idle and not e.halted
    e.tx.append(0x42)
    run(e, 10)
    assert e.halted and e.regs[0] == 0x42 and e.rx == [0x42]
    assert e.halt_cycle == 10  # pop at 6 (next own cycle), push at 8, halt at 10


@pytest.mark.parametrize("fall,want", [(6, [1, 1, 0, 0]), (7, [1, 1, 1, 0])])
def test_raw_get_sees_wire_after_sync_lat(fall, want):
    """ROLE_IF at 2; GETs at 4, 6, 8, 10 see the wire at now - SYNC_LAT = 2, 4, 6, 8. A wire that
    falls at 6 is first seen by the GET at 8; one that falls at 7, by the GET at 10. Together the two
    cases rule out a latency of 1 or 3."""
    def b(d, l):
        with d.only("a"):
            for r in ("r0", "r1", "r2", "r3"):
                d.raw_get(r, l)
        d.halt()
    e = Engine(img(b), Role.P0)
    run(e, 12, wire=lambda c: [1 if c < fall else 0] + [1] * 7)
    assert e.regs == want + [0, 0, 0, 0]


def test_raw_delay_exact_cycles():
    def b(d, l):
        d.raw_delay(5)
        d.halt()
    e = run(Engine(img(b), Role.P0), 20)
    assert e.halt_cycle == 8  # DELAY 5 starts at 2 and takes 6 (rounded up to a multiple of U)


def test_raw_set_drives_own_line():
    def b(d, l):
        with d.only("a"):
            d.raw_set(l, 0)
            d.raw_rel(l)
        d.halt()
    e = Engine(img(b), Role.P0)
    run(e, 4)                 # now 0..3: ROLE_IF at 2; the owner drives its PP line at idle
    assert e.drv[0] == 1
    run(e, 1)                 # SET at 4
    assert e.drv[0] == 0
    run(e, 2)                 # REL at 6
    assert e.drv[0] is None


def test_reset_drives_owned_pp_idle_only():
    lines = (("x", "pp", "a", 0), ("y", "pp", "b", 1), ("z", "od", "shared", 1))
    image = img(lambda d, *refs: d.halt(), lines)
    assert Engine(image, Role.P0).drv == [0, None, None]
    assert Engine(image, Role.P1).drv == [None, 1, None]
    assert Engine(image, Role.MON).drv == [None, None, None]


def test_resolve_contention_and_od():
    pp = LineCfg("pp", "pp", Owner.P0, 1)
    od = LineCfg("od", "od", Owner.SHARED, 1)
    assert resolve([pp, od], [[None, None], [None, None]]) == ([1, 1], [])
    assert resolve([pp, od], [[1, None], [None, 0]]) == ([1, 0], [])
    assert resolve([pp, od], [[1, 0], [0, None]]) == ([0, 0], [0])


def test_br_to_tests_the_timeout_flag_not_a_register():
    """BR TO reads `to` (isa-v0 §4), never a register: r0 is nonzero, `to` is clear."""
    def b(d, l):
        d.li("r0", 1)
        with d.only("a"):
            d.label("x")
            d.br("x", cond="to")
        d.halt()
    e = run(Engine(img(b), Role.P0), 10)
    assert e.halted and [x.taken for x in e.branches] == [False]


@pytest.mark.parametrize("op,a,b,want", [
    ("and", 0x0F0, 0x03C, 0x030),
    ("ne", 0x0F0, 0x0F0, 0), ("ne", 0x0F0, 0x0F1, 1),
    ("lt", 0x011, 0x011, 0), ("lt", 0x011, 0x010, 0), ("lt", 0x010, 0x011, 1),
    ("eq", 0x011, 0x010, 0), ("eq", 0x011, 0x011, 1),
])
def test_alu_compare_and_mask_ops(op, a, b, want):
    def prog(d, l):
        d.li("r0", a)
        d.alu(op, "r1", "r0", b)
        d.halt()
    assert run(Engine(img(prog), Role.P0), 5).regs[1] == want
