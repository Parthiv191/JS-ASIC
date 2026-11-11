"""Bring-up layer 5 (architecture §5): engine 0 runs a program from column 1.

Programs are built with the v1 encoder (toolchain/emu/isa.py). Expected times come from
isa §4.0, not from the RTL: RUN's first own cycle is now = 0, instruction 0 starts at now = 2,
every untimed instruction (taken branches included) costs U = 2, and RAW DELAY n costs n
rounded up to a multiple of 2 (n = 0 counts as 1). A halted engine's NOW is its HALT's start.

All checks go through the pins, except test_trace_port, which reads the tr_* trace port by
hierarchy and is skipped at gate level (synthesis removes it)."""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

# The v1 encoder is pure Python with no dependencies; CI installs only test/requirements.txt.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolchain"))

from emu.isa import AluOp, BrCond, F_IRQ, F_REG, Instr, Op, RawOp, Role, encode, li, role_if_operand

from chip import (COL1, E_FV_KIND, E_FV_PC, E_HALTED, E_IRQ_ID, E_NOW, E_PC, E_RUNNING, E_STATUS, E_TB,
                  E_VIOL, ENG0, ERR_CLR, IRQ, LINE, RUN, SOFT_RESET, STATUS_HOST_ERR, Host, eng_read,
                  get_reg, line_byte, reset, run_program, set_reg, wait_halted)

GL = os.environ.get("GATES") == "yes"
U = 2
IRQ_PIN, ERR_PIN = 1 << 1, 1 << 2
K_ILLEGAL = 10
UNUSED_LINES = [line_byte(i, od=True, owner=2, idle=1) for i in range(8)]  # 0x68 + i


def alu(op, rd, ra, b, reg=False):
    return Instr(Op.ALU, rd=rd, ra=ra, sub=int(op), operand=b, flags=F_REG if reg else 0)


def br(cond, target, ra=0):
    return Instr(Op.BR, ra=ra, sub=int(cond), operand=target)


def raw(op, line=0, rd=0, operand=0):
    return Instr(Op.RAW, rd=rd, ra=line, sub=int(op), operand=operand)


HALT = Instr(Op.HALT)
NOP = Instr(Op.NOP)


def words(prog):
    """Encode a program; a plain int is a raw word (e.g. a free opcode the encoder refuses)."""
    return [i if isinstance(i, int) else encode(i) for i in prog]


def delay_cost(n):
    """isa §4.0: n cycles rounded up to a multiple of U; the model treats n = 0 as 1."""
    return -(-max(1, n) // U) * U


async def start(dut, prog, role=0, lines=None):
    await reset(dut)
    host = Host(dut)
    await run_program(host, words(prog), role, lines)
    return host


# ---------------------------------------------------------------- straight-line code, time

@cocotb.test()
async def test_li_halt_timing(dut):
    """LI writes all 16 bits; HALT at index 3 starts at now = 2 + 3U = 8; tb stays 0."""
    host = await start(dut, [li(1, 0x1234), li(7, 0xBEEF), NOP, HALT])
    await wait_halted(host)
    assert await eng_read(host, E_STATUS) == E_HALTED
    assert await get_reg(host, 1) == 0x1234
    assert await get_reg(host, 7) == 0xBEEF
    assert await eng_read(host, E_PC, 2) == 3
    assert await eng_read(host, E_NOW, 3) == 2 + 3 * U
    assert await eng_read(host, E_TB, 3) == 0


@cocotb.test()
async def test_alu(dut):
    """Every aluop, register and immediate forms, against a Python reference (isa §3)."""
    ref = {AluOp.ADD: lambda a, b: a + b, AluOp.SUB: lambda a, b: a - b, AluOp.AND: lambda a, b: a & b,
           AluOp.OR: lambda a, b: a | b, AluOp.XOR: lambda a, b: a ^ b,
           AluOp.SHL: lambda a, b: a << (b & 15), AluOp.SHR: lambda a, b: a >> (b & 15),
           AluOp.EQ: lambda a, b: int(a == b), AluOp.NE: lambda a, b: int(a != b),
           AluOp.LT: lambda a, b: int(a < b)}
    await reset(dut)
    host = Host(dut)
    rng = random.Random(5)
    for op, f in ref.items():
        a, b, imm = rng.getrandbits(16), rng.getrandbits(16), rng.getrandbits(12)
        if op in (AluOp.EQ, AluOp.NE):
            b = a  # make the equal case show up
        prog = [li(1, a), li(2, b), alu(op, 3, 1, 2, reg=True), alu(op, 4, 1, imm), HALT]
        await run_program(host, words(prog))
        await wait_halted(host)
        assert await get_reg(host, 3) == f(a, b) & 0xFFFF, f"{op.name} {a:#x}, {b:#x} (reg)"
        assert await get_reg(host, 4) == f(a, imm) & 0xFFFF, f"{op.name} {a:#x}, {imm:#x} (imm)"


@cocotb.test()
async def test_loop_timing(dut):
    """A counted loop: 1 + 2N instructions before HALT, taken branches cost U like the rest."""
    n = 5
    prog = [li(0, n), alu(AluOp.SUB, 0, 0, 1), br(BrCond.NZ, 1, ra=0), HALT]
    host = await start(dut, prog)
    await wait_halted(host)
    assert await get_reg(host, 0) == 0
    assert await eng_read(host, E_PC, 2) == 3
    assert await eng_read(host, E_NOW, 3) == 2 + (1 + 2 * n) * U


@cocotb.test()
async def test_delay(dut):
    """RAW DELAY n, then HALT: HALT starts at 2 + cost(n)."""
    await reset(dut)
    host = Host(dut)
    for n in (0, 1, 2, 3, 4, 7, 50, 301):
        await run_program(host, words([raw(RawOp.DELAY, operand=n), HALT]))
        await wait_halted(host)
        got = await eng_read(host, E_NOW, 3)
        assert got == 2 + delay_cost(n), f"DELAY {n}: HALT at now {got}, want {2 + delay_cost(n)}"


# ---------------------------------------------------------------- branches and roles

@cocotb.test()
async def test_branches(dut):
    """BR ALWAYS / Z / NZ: each writes a marker on the path it takes."""
    prog = [
        li(1, 0),                     # 0
        br(BrCond.Z, 3, ra=1),        # 1: taken (r1 = 0)
        li(7, 0xBAD),                 # 2: skipped
        br(BrCond.NZ, 6, ra=1),       # 3: not taken
        br(BrCond.ALWAYS, 7),         # 4: taken
        li(7, 0xBAD),                 # 5
        li(7, 0xBAD),                 # 6
        li(2, 0x600D),                # 7
        HALT,                         # 8
    ]
    host = await start(dut, prog)
    await wait_halted(host)
    assert await get_reg(host, 7) == 0
    assert await get_reg(host, 2) == 0x600D
    # executed: 0, 1, 3, 4, 7, then HALT: 5 instructions before it
    assert await eng_read(host, E_NOW, 3) == 2 + 5 * U


@cocotb.test()
async def test_roles(dut):
    """ROLE_IF skips its block for other roles and costs U either way; BR ROLE tests the mask."""
    p1 = role_if_operand(1 << int(Role.P1), 2)
    prog = [
        Instr(Op.ROLE_IF, operand=p1),                    # 0
        li(1, 0x11),                                      # 1: P1 only
        li(2, 0x22),                                      # 2: P1 only
        br(BrCond.ROLE, 5, ra=1 << int(Role.MON)),        # 3: MON jumps to 5
        li(3, 0x33),                                      # 4: P0, P1
        HALT,                                             # 5
    ]
    await reset(dut)
    host = Host(dut)
    for role in (Role.P0, Role.P1, Role.MON):
        for i in (1, 2, 3):
            await set_reg(host, i, 0)
        await run_program(host, words(prog), role=int(role))
        await wait_halted(host)
        got = [await get_reg(host, i) for i in (1, 2, 3)]
        want = [0x11 if role == Role.P1 else 0, 0x22 if role == Role.P1 else 0,
                0 if role == Role.MON else 0x33]
        assert got == want, f"{role.name}: r1-r3 = {got}"
        ran = (1 + (2 if role == Role.P1 else 0) + 1 + (0 if role == Role.MON else 1))
        assert await eng_read(host, E_NOW, 3) == 2 + ran * U, role.name


# ---------------------------------------------------------------- lines and pins

@cocotb.test()
async def test_lines_drive(dut):
    """RUN drives owned non-gated PP lines to idle; RAW SET/REL drive the mapped pin; HALT keeps
    PP levels and releases OD lines; stopping releases everything."""
    lines = list(UNUSED_LINES)
    lines[0] = line_byte(2, od=False, owner=0, idle=1)   # L0: pin 2, PP, P0, idle 1
    lines[1] = line_byte(5, od=True, owner=2, idle=1)    # L1: pin 5, OD, shared
    lines[2] = line_byte(6, od=False, owner=1, idle=0)   # L2: pin 6, PP, P1 (not ours)
    lines[3] = line_byte(0, od=False, owner=0, idle=0)   # L3: pin 0, PP, P0, idle 0
    prog = [raw(RawOp.DELAY, operand=2000),              # 0: look at the RUN levels
            raw(RawOp.SET, line=0, operand=0),           # 1
            raw(RawOp.SET, line=1, operand=0),           # 2
            raw(RawOp.SET, line=3, operand=1),           # 3
            raw(RawOp.DELAY, operand=2000),              # 4: look at the driven levels
            HALT]
    host = await start(dut, prog, role=0, lines=lines)
    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    assert oe == 0b0000_0101, f"at RUN: uio_oe {oe:#04x}"  # pins 0 and 2
    assert out & 0b101 == 0b100, f"at RUN: uio_out {out:#04x}"
    await ClockCycles(dut.clk, 2000)
    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    assert oe == 0b0010_0101, f"driven: uio_oe {oe:#04x}"
    assert out & 0b0010_0101 == 0b0000_0001, f"driven: uio_out {out:#04x}"
    await wait_halted(host)
    oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
    assert oe == 0b0000_0101 and out & 0b101 == 0b001, f"halted: oe {oe:#04x} out {out:#04x}"
    await host.write(RUN, [0])
    await ClockCycles(dut.clk, 4)
    assert int(dut.uio_oe.value) == 0


@cocotb.test()
async def test_lines_read(dut):
    """RAW GET reads the mapped pin; RAW JPIN branches on its level."""
    lines = list(UNUSED_LINES)
    lines[4] = line_byte(7, od=True, owner=2, idle=1)  # L4: pin 7, released: an input
    prog = [raw(RawOp.GET, line=4, rd=1),                         # 0
            raw(RawOp.JPIN, line=4, operand=1 << 11 | 4),         # 1: pin high -> 4
            li(2, 0xAAAA),                                        # 2
            HALT,                                                 # 3
            li(2, 0x5555),                                        # 4
            HALT]                                                 # 5
    await reset(dut)
    host = Host(dut)
    for level in (0, 1):
        dut.uio_in.value = level << 7
        await run_program(host, words(prog), lines=lines)
        await wait_halted(host)
        assert await get_reg(host, 1) == level
        assert await get_reg(host, 2) == (0x5555 if level else 0xAAAA)


# ---------------------------------------------------------------- MARK, IRQ, ILLEGAL, errors

@cocotb.test()
async def test_mark_irq(dut):
    """MARK IRQ raises the IRQ pin and latches the id until the host clears it; MARK alone doesn't."""
    host = await start(dut, [Instr(Op.MARK, operand=0x7AB), HALT])
    await wait_halted(host)
    assert not int(dut.uo_out.value) & IRQ_PIN
    await run_program(host, words([Instr(Op.MARK, operand=0x123, flags=F_IRQ), HALT]))
    await wait_halted(host)
    assert int(dut.uo_out.value) & IRQ_PIN
    assert await eng_read(host, E_IRQ_ID, 2) == 0x123
    assert await host.read(IRQ, 1) == [1]
    await host.write(IRQ, [1])
    await ClockCycles(dut.clk, 3)
    assert not int(dut.uo_out.value) & IRQ_PIN
    assert await host.read(IRQ, 1) == [0]


@cocotb.test()
async def test_illegal(dut):
    """A free opcode halts the engine: VIOL bit 10, FIRST_VIOL, and the ERR pin until cleared."""
    host = await start(dut, [NOP, NOP, 20 << 27, li(1, 1), HALT])
    await wait_halted(host)
    assert await get_reg(host, 1) == 0, "ran past ILLEGAL"
    assert await eng_read(host, E_VIOL, 2) == 1 << K_ILLEGAL
    assert await eng_read(host, E_FV_PC, 2) == 2
    assert await eng_read(host, E_FV_KIND) == K_ILLEGAL
    assert await eng_read(host, E_PC, 2) == 2
    assert int(dut.uo_out.value) & ERR_PIN
    await host.write(ENG0 + E_VIOL, [0x00, 0x04])  # W1C
    await ClockCycles(dut.clk, 3)
    assert await eng_read(host, E_VIOL, 2) == 0
    assert not int(dut.uo_out.value) & ERR_PIN


@cocotb.test()
async def test_host_err(dut):
    """While an engine runs, column 1, LINE and REG_DATA writes are refused and set host_err."""
    host = await start(dut, [li(1, 0x4242), br(BrCond.ALWAYS, 1)])  # runs forever
    await ClockCycles(dut.clk, 20)
    assert await eng_read(host, E_STATUS) == E_RUNNING
    await host.write_words(COL1 + 4 * 8, [0x12345678])
    assert (await host.read(0x0003, 1))[0] & STATUS_HOST_ERR
    await host.write(ERR_CLR, [1])
    assert not (await host.read(0x0003, 1))[0] & STATUS_HOST_ERR
    await set_reg(host, 1, 0x0000)
    assert (await host.read(0x0003, 1))[0] & STATUS_HOST_ERR
    await host.write(ERR_CLR, [1])
    await host.write(LINE, [0x00])
    assert (await host.read(0x0003, 1))[0] & STATUS_HOST_ERR
    await host.write(RUN, [0])
    await ClockCycles(dut.clk, 4)
    assert await get_reg(host, 1) == 0x4242, "REG_DATA write landed while running"
    assert (await host.read_words(COL1 + 4 * 1, 1))[0] == encode(br(BrCond.ALWAYS, 1))
    await host.write_words(COL1 + 4 * 8, [0x12345678])  # stopped: allowed
    assert (await host.read_words(COL1 + 4 * 8, 1))[0] == 0x12345678


@cocotb.test()
async def test_soft_reset(dut):
    """SOFT_RESET stops the engine, clears RUN and the engine's registers, not the globals."""
    host = await start(dut, [li(3, 0x3333), br(BrCond.ALWAYS, 1)])
    await host.write(0x0002, [0x5C])  # SCRATCH, a global
    await host.write(SOFT_RESET, [1])
    assert await host.read(RUN, 1) == [0]
    assert await eng_read(host, E_STATUS) == 0
    assert await get_reg(host, 3) == 0
    assert await eng_read(host, E_PC, 2) == 0
    assert await host.read(0x0002, 1) == [0x5C]


@cocotb.test()
async def test_now_counts(dut):
    """NOW (3 bytes, snapshotted on its low byte) increases between reads while running."""
    host = await start(dut, [br(BrCond.ALWAYS, 0)])
    prev = -1
    for _ in range(5):
        now = await eng_read(host, E_NOW, 3)
        assert now > prev, f"NOW went {prev} -> {now}"
        prev = now



def reserved_words():
    """Reserved codes from isa §3, one instruction each (the encoder refuses free opcodes)."""
    from emu.isa import CondKind, Dst, Owner, Src, cond_operand, xfer_operand
    hold = lambda **k: Instr(Op.HOLD, **({"owner": Owner.P0, "line": 0, "prof": 0} | k))
    xf = lambda src=Src.ZERO, dst=Dst.DISCARD, **k: Instr(
        Op.XFER, **({"owner": Owner.P0, "line": 0, "prof": 0, "operand": xfer_operand(1, src, dst)} | k))
    return {
        "ALU sub 10": Instr(Op.ALU, sub=10), "ALU sub 15": Instr(Op.ALU, sub=15),
        "BR 5": Instr(Op.BR, sub=5), "BR 15": Instr(Op.BR, sub=15),
        "RAW 6": Instr(Op.RAW, sub=6), "RAW 15": Instr(Op.RAW, sub=15),
        "step owner 3": hold(owner=Owner.NONE),
        "profile 8": hold(prof=8), "line 8": hold(line=8),
        "COND line 8": Instr(Op.COND, owner=Owner.P0, line=8, prof=0, operand=cond_operand(CondKind.FALL)),
        "lane 4": xf(line=4),
        "src 4": Instr(Op.XFER, owner=Owner.P0, line=0, prof=0, operand=4 << 4),
        "src 7": Instr(Op.XFER, owner=Owner.P0, line=0, prof=0, operand=7 << 4),
        "dst 1": Instr(Op.XFER, owner=Owner.P0, line=0, prof=0, operand=1 << 8),
        "dst 3": Instr(Op.XFER, owner=Owner.P0, line=0, prof=0, operand=3 << 8),
        "dst 7": Instr(Op.XFER, owner=Owner.P0, line=0, prof=0, operand=7 << 8),
        "free opcode 20": 20 << 27,
    }


@cocotb.test()
async def test_reserved_illegal(dut):
    """Every reserved code halts as ILLEGAL in its start cycle (isa §3): NOP at 2, the reserved
    instruction at 4 (VIOL bit 10, FIRST_VIOL at pc 1), nothing after it runs, NOW stays 4."""
    try:
        from emu.isa import reserved   # the model's list (verification #15), once merged
    except ImportError:
        reserved = None
    await reset(dut)
    host = Host(dut)
    for name, ins in reserved_words().items():
        if reserved is not None and not isinstance(ins, int):
            assert reserved(ins), f"{name}: the model does not call it reserved"
        await set_reg(host, 1, 0)
        await run_program(host, words([NOP, ins, li(1, 1), HALT]))
        await wait_halted(host)
        assert await eng_read(host, E_VIOL, 2) == 1 << K_ILLEGAL, name
        assert await eng_read(host, E_FV_PC, 2) == 1, name
        assert await get_reg(host, 1) == 0, f"{name}: ran past it"
        assert await eng_read(host, E_NOW, 3) == 4, name
        await host.write(ENG0 + E_VIOL, [0xFF, 0xFF])  # clear for the next one

# ---------------------------------------------------------------- trace port (RTL only)

def expected_starts(prog, role=0):
    """(pc, now) of each instruction start, from isa §4.0 directly, for untimed programs."""
    out, pc, now, regs = [], 0, 2, [0] * 8
    while True:
        ins = prog[pc]
        out.append((pc, now))
        if ins.op == Op.HALT:
            return out
        cost, nxt = U, pc + 1
        if ins.op == Op.LI:
            regs[ins.rd] = ins.sub << 12 | ins.operand
        elif ins.op == Op.ALU and ins.sub == AluOp.SUB:
            regs[ins.rd] = (regs[ins.ra] - ins.operand) & 0xFFFF
        elif ins.op == Op.BR:
            taken = {BrCond.ALWAYS: True, BrCond.Z: regs[ins.ra] == 0,
                     BrCond.NZ: regs[ins.ra] != 0}[BrCond(ins.sub)]
            nxt = ins.operand if taken else nxt
        elif ins.op == Op.RAW and ins.sub == RawOp.DELAY:
            cost = delay_cost(ins.operand)
        elif ins.op == Op.ROLE_IF:
            mask, count = ins.operand & 7, ins.operand >> 3
            nxt = nxt if mask >> role & 1 else nxt + count
        pc, now = nxt, now + cost


@cocotb.test(skip=GL)
async def test_trace_port(dut):
    """tr_issue fires at every instruction start with (tr_pc, tr_now) as isa §4.0 predicts;
    tr_br/tr_taken report the conditional branches, tr_mark the MARK, tr_halted the halt."""
    prog = [li(0, 3), Instr(Op.MARK, operand=0x42), raw(RawOp.DELAY, operand=5),
            Instr(Op.ROLE_IF, operand=role_if_operand(1 << int(Role.P1), 1)), li(5, 5),
            alu(AluOp.SUB, 0, 0, 1), br(BrCond.NZ, 5, ra=0), raw(RawOp.DELAY, operand=0), HALT]
    eng = dut.user_project.u_eng0
    starts, branches, marks = [], [], []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            if int(eng.tr_issue.value):
                starts.append((int(eng.tr_pc.value), int(eng.tr_now.value)))
            if int(eng.tr_br.value):
                branches.append((int(eng.tr_pc.value), int(eng.tr_taken.value)))
            if int(eng.tr_mark.value):
                marks.append((int(eng.tr_pc.value), int(eng.tr_value.value), int(eng.tr_now.value)))

    await reset(dut)
    cocotb.start_soon(watch())
    host = Host(dut)
    await run_program(host, words(prog))
    await wait_halted(host)
    want = expected_starts(prog)
    assert starts == want, f"starts {starts}\n want {want}"
    # ROLE_IF at 3 is a branch whose `taken` means skipped (P0 is not in its P1 mask)
    assert branches == [(3, 1), (6, 1), (6, 1), (6, 0)]
    assert marks == [(1, 0x42, 4)]
    assert int(eng.tr_halted.value) == 1
    assert int(eng.tr_tb.value) == 0
