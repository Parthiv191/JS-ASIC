"""Description compiler: lowering, well-formedness rules, encoding, debug map.

Rule ids follow docs/design/isa-v0.md §5 (R*, S*) and §6 (T*, in analysis.py).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from .desc import MON, SHARED, Description, LaneRef, LineRef, Step
from .image import IDLE, BusCfg, Image, LaneCfg, LineCfg
from .isa import (
    F_ABORT, F_CHK, F_MSB, F_REG, F_RESYNC, INF, NUM_LANES, NUM_LINES, NUM_PROFILES, PROG_DEPTH, TIMED_OPS,
    AluOp, BrCond, BusKind, CondKind, Dst, FzOp, Instr, Op, Owner, RawOp, Role, Src, cond_operand, encode, li,
    role_if_operand, role_mask, unpack_cond, unpack_xfer, xfer_operand,
)

TIMED = ("cond", "hold", "xfer")
BRANCHES = ("br", "raw_jpin")
FZ_KINDS = ("fz_next", "fz_tok", "fz_end")


@dataclass(frozen=True)
class Diagnostic:
    rule: str
    index: int
    loc: str
    msg: str

    def __str__(self) -> str:
        return f"{self.loc}: [{self.rule}] step {self.index}: {self.msg}"


class CompileWarning(UserWarning):
    """A legal description whose behaviour is probably not what was meant."""


class CompileError(Exception):
    def __init__(self, diags: list[Diagnostic]):
        self.diags = diags
        super().__init__("\n".join(str(d) for d in diags))


@dataclass(frozen=True)
class Span:
    """A role block: ROLE_IF at `role_if_pc`, body pcs [start, end)."""
    role_if_pc: int
    start: int
    end: int
    roles: frozenset[Role]
    lead: bool = False  # a lead block (isa §5 R3-lead): one party, may hold timed steps


@dataclass
class Lowered:
    instrs: list[Instr]
    image: Image
    spans: list[Span]
    pc_step: list[int]  # description step index for each pc
    diags: list[Diagnostic] = field(default_factory=list)


def _role(desc: Description, name: str) -> Role:
    return Role.MON if name == MON else Role(desc.parties.index(name))


def _owner(desc: Description, name: str | None) -> Owner:
    if name is None:
        return Owner.NONE
    if name == SHARED:
        return Owner.SHARED
    return Owner(desc.parties.index(name))


def lower(desc: Description, prog_depth: int = PROG_DEPTH) -> Lowered:
    diags: list[Diagnostic] = []

    def err(rule: str, step: Step, msg: str) -> None:
        diags.append(Diagnostic(rule, step.index, step.loc, msg))

    lines = [LineCfg(ln.name, ln.kind, _owner(desc, ln.owner), ln.idle, gated=ln.gated) for ln in desc.lines]
    profiles = [IDLE] + list(desc.profiles)
    lanes = [LaneCfg(la.data.index, None if la.clk is None else la.clk.index, la.cpol, la.cpha,
                     data2=None if la.data2 is None else la.data2.index, ldelay=la.ldelay,
                     stretch=int(la.stretch))
             for la in desc.lanes]

    # ---- pass 1: layout (pcs, labels, spans) ----
    layout: list[tuple[str, Step | frozenset[str], int]] = []  # ("role_if", roles, first step idx) or ("step", step)
    i = 0
    steps = desc.steps
    while i < len(steps):
        s = steps[i]
        if s.block is None:
            layout.append(("step", s, s.index))
            i += 1
            continue
        j = i
        while j < len(steps) and steps[j].block == s.block and steps[j].lead == s.lead:
            j += 1
        layout.append(("role_if", s.block, j - i))
        layout.extend(("step", t, t.index) for t in steps[i:j])
        i = j

    labels: dict[str, int] = {}
    spans: list[Span] = []
    pc_step: list[int] = []
    pc = 0
    k = 0
    while k < len(layout):
        kind, obj, extra = layout[k]
        if kind == "role_if":
            first = layout[k + 1][1]
            for lb in first.labels:  # a label on a block's first step names the block
                labels[lb] = pc
            roles = frozenset(_role(desc, r) for r in obj)
            spans.append(Span(pc, pc + 1, pc + 1 + extra, roles, lead=first.lead))
            pc_step.append(first.index)
            pc += 1
            for _, t, _ in layout[k + 1:k + 1 + extra]:
                if t is not first:
                    for lb in t.labels:
                        labels[lb] = pc
                pc_step.append(t.index)
                pc += 1
            k += 1 + extra
        else:
            for lb in obj.labels:
                labels[lb] = pc
            pc_step.append(obj.index)
            pc += 1
            k += 1

    def span_of(p: int) -> Span | None:
        for sp in spans:
            if sp.start <= p < sp.end:
                return sp
        return None

    def target(step: Step, name: str) -> int:
        if name not in labels:
            err("S-label", step, f"unknown label {name!r}")
            return 0
        return labels[name]

    def lane_index(step: Step, ref: LaneRef | LineRef) -> int:
        if isinstance(ref, LaneRef):
            return ref.index
        cfg = LaneCfg(ref.index, None, 0, 0)
        if cfg not in lanes:
            lanes.append(cfg)
        return lanes.index(cfg)

    abort_pc = 0
    if desc.abort_label is not None:
        anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
        if desc.abort_label in labels:
            abort_pc = labels[desc.abort_label]
            if span_of(abort_pc) is not None:
                err("R3-escape", anchor, f"abort target {desc.abort_label!r} is inside a role block; "
                                         f"an abort would skip the block's role check")
        else:
            err("S-label", anchor, f"unknown abort label {desc.abort_label!r}")

    # ---- pass 2: rules + instructions ----
    instrs: list[Instr] = []
    pc = 0
    od_lane: int | None = None  # R1-od-lane: the one clocked lane with an OD data line
    fz_party: str | None = None  # R3-fz: the one party whose role blocks hold the FZ ops
    fz_sends: list[Step] = []    # XFERs with src = fz: the fuzzing party must send in each
    for kind, obj, extra in layout:
        if kind == "role_if":
            mask = role_mask(_role(desc, r) for r in obj)
            instrs.append(Instr(Op.ROLE_IF, operand=role_if_operand(mask, min(extra, 511))))
            if extra > 511:
                err("S-range", desc.steps[pc_step[pc]], "role block longer than 511")
            pc += 1
            continue
        s: Step = obj
        a = s.args
        _check_rules(desc, s, lines, lanes, err)
        if s.kind == "xfer" and isinstance(a["lane"], LaneRef):
            la = desc.lanes[a["lane"].index]
            if la.clk is not None and any(x is not None and lines[x.index].kind == "od"
                                          for x in (la.data, la.data2)):
                if od_lane is None:
                    od_lane = a["lane"].index
                elif od_lane != a["lane"].index:
                    err("R1-od-lane", s, f"lane {la.name!r} is a second clocked lane with an OD data "
                                         f"line (after {desc.lanes[od_lane].name!r}); the engine has one "
                                         f"OD let-go tracker (isa §4.3)")
        if s.kind in FZ_KINDS:
            party = next(iter(s.block)) if s.block is not None and len(s.block) == 1 else None
            if party not in desc.parties:
                err("R3-fz", s, "an FZ op belongs in a role block for exactly one party (isa §5)")
            elif fz_party is None:
                fz_party = party
            elif party != fz_party:
                err("R3-fz", s, f"FZ ops for {party!r} and {fz_party!r}: one party fuzzes (isa §5)")
        if s.kind == "xfer" and a["src"] == "fz":
            fz_sends.append(s)
        try:
            ins = _instr(desc, s, a, lambda n: target(s, n), lambda r: lane_index(s, r))
        except ValueError as e:
            err("S-range", s, str(e))
            ins = Instr(Op.NOP)
        if s.kind in BRANCHES:
            t = ins.operand if s.kind == "br" else ins.operand & 0x7FF
            here, there = span_of(pc), span_of(t)
            # here.end is the block's exit, where every role resumes, so `<=` allows an early exit.
            # With adjacent blocks it is the next block's ROLE_IF, so that role check still runs.
            to_abort = here is not None and here.lead and desc.abort_label is not None and t == abort_pc
            if here is not None and not (here.start <= t <= here.end or t == here.role_if_pc or to_abort):
                err("R3-escape", s, "branch leaves its role block (only to its exit, or from a lead "
                                    "block to abort_pc)")
            if here is None and there is not None:
                err("R3-escape", s, "branch enters a role block from outside")
            if here is not None and there is not None and here != there:
                err("R3-escape", s, "branch between role blocks")
        instrs.append(ins)
        pc += 1

    for s in fz_sends:
        senders = {s.owner}
        if isinstance(s.args["lane"], LaneRef) and desc.lanes[s.args["lane"].index].data2 is not None:
            senders.add(desc.lines[desc.lanes[s.args["lane"].index].data2.index].owner)
        if fz_party is None:
            err("R3-fz", s, "src = fz with no FZ op: no party fuzzes")
        elif fz_party not in senders:
            err("R3-fz", s, f"src = fz sent by {', '.join(sorted(senders))}, not the fuzzing party "
                            f"{fz_party!r} (isa §5)")

    if len(profiles) > NUM_PROFILES or len(lanes) > NUM_LANES or len(lines) > NUM_LINES:
        anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
        err("S-table", anchor, f"tables exceed limits: {len(profiles)} profiles (max {NUM_PROFILES}), "
                               f"{len(lanes)} lanes (max {NUM_LANES}), {len(lines)} lines (max {NUM_LINES})")
    if len(instrs) > prog_depth:
        err("S-range", desc.steps[-1], f"program has {len(instrs)} words (max {prog_depth})")
    for sp in spans:
        if sp.lead:
            _check_lead(desc, sp, instrs, pc_step, abort_pc, profiles, lines, lanes, err)
    sel_master = (lines[desc.bus_cfg[1].index].owner
                  if desc.bus_cfg is not None and desc.bus_cfg[0] == "select" else None)
    for ln in lines:
        anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
        if ln.gated and ln.kind != "pp":
            err("S-line", anchor, f"line {ln.name!r}: `gated` only on push-pull lines (isa §2)")
        if ln.gated and ln.idle != 1:
            err("S-line", anchor, f"gated line {ln.name!r} needs idle 1: released, it reads the pull-up")
        if ln.gated and ln.owner == sel_master:
            err("S-line", anchor, f"gated line {ln.name!r} is owned by the SELECT master, which runs no "
                                  f"detector, so no STOP would release it")

    words = []
    for ins in instrs:
        try:
            words.append(encode(ins))
        except ValueError as e:
            assert diags, f"encode failed without a diagnostic: {ins} ({e})"
            words.append(0)  # already diagnosed; compile_desc refuses the image
    debug = {}
    for p, ins in enumerate(instrs):
        st = desc.steps[pc_step[p]]
        if ins.op == Op.ROLE_IF:
            debug[p] = f"{st.loc} only({','.join(sorted(st.block or ()))})"
        else:
            debug[p] = f"{st.loc} {st.kind} {_summary(st)}".rstrip()
    bus = BusCfg()
    if desc.bus_cfg is not None:
        kind, a, b, confirm = desc.bus_cfg
        bus = BusCfg(BusKind[kind.upper()], a.index, 0 if b is None else b.index, confirm)
        anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
        if kind == "i2c" and (lines[a.index].kind != "od" or lines[b.index].kind != "od"):
            err("S-bus", anchor, "an I2C bus needs OD lines for SDA (a) and SCL (b)")
        if kind == "i2c" and a.index == b.index:
            err("S-bus", anchor, "an I2C bus needs two lines: SDA (a) and SCL (b)")
        if kind == "i2c" and not any(la.data.index == a.index and la.clk is not None and la.clk.index == b.index
                                     and la.cpol == 1 and la.cpha == 1 for la in desc.lanes):
            err("S-bus", anchor, "an I2C bus is used with a cpol 1, cpha 1 lane on (SDA, SCL)")
        master = lines[(b if kind == "i2c" else a).index]
        if master.owner not in (Owner.P0, Owner.P1):
            err("S-bus", anchor, f"the bus's master line {master.name!r} needs a party owner (it alone "
                                 f"runs no detector)")
        if kind == "select" and lines[a.index].kind != "pp":
            err("S-bus", anchor, "a SELECT bus needs a push-pull CS line (a)")
    image = Image(desc.name, desc.parties, words, profiles, lines, lanes, abort_pc, desc.tolerance_ppm, debug,
                  bus=bus)
    if not any(d.rule == "S-table" for d in diags):
        try:
            image.table_bytes()  # the bytes the host writes: every field must fit its slot
        except ValueError as e:
            anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
            err("S-table", anchor, f"tables don't fit the chip: {e}")
    return Lowered(instrs, image, spans, pc_step, diags)


def compile_desc(desc: Description, *, check_timing: bool = True, check_levels: bool = True,
                 prog_depth: int = PROG_DEPTH) -> Image:
    low = lower(desc, prog_depth)
    diags = list(low.diags)
    if desc.abort_label is None and any(s.args.get("abort") for s in desc.steps):
        warnings.warn(CompileWarning(f"{desc.name}: a step has abort but abort_to is not set, so an "
                                     f"abort restarts the description at pc 0"), stacklevel=2)
    if not diags:
        from . import analysis  # imports this module; deferred to avoid a cycle
        diags += analysis.check_branches(desc, low)
        if check_levels:
            diags += analysis.check_levels(desc, low)
        if check_timing:
            diags += analysis.check_timing(desc, low)
    if diags:
        raise CompileError(diags)
    return low.image


# ---------------------------------------------------------------------------------------
def _check_rules(desc, s: Step, lines: list[LineCfg], lanes: list[LaneCfg], err) -> None:
    a = s.args
    if s.kind in TIMED:
        if s.block is not None and not s.lead:
            err("R3-timed-in-role", s, f"{s.kind} inside a role block (only a lead block may hold timed steps)")
        if s.owner is None:
            err("R1-owner", s, "timed step without an owner")
            return
        if s.kind == "cond" and a["kind"] in ("start", "stop"):
            # isa §5 R1-bus, S-bus: on the bus, owned by the master line's owner (I2C: SCL, SELECT: CS)
            if desc.bus_cfg is None:
                err("S-bus", s, f"COND {a['kind']} needs a bus (bus_kind is NONE)")
                return
            kind, bus_a, bus_b = desc.bus_cfg[0], desc.bus_cfg[1], desc.bus_cfg[2]
            master = lines[(bus_b if kind == "i2c" else bus_a).index]
            if master.owner != _owner(desc, s.owner):
                err("R1-bus", s, f"COND {a['kind']} is owned by {master.name!r}'s owner, "
                                 f"{_name(desc, master.owner)!r}")
            return
        if s.kind in ("cond", "hold"):
            data = a["line"].index
            clk = None
        else:
            ref = a["lane"]
            if isinstance(ref, LaneRef):
                la = desc.lanes[ref.index]
                data, clk = la.data.index, None if la.clk is None else la.clk.index
            else:
                data, clk = ref.index, None
        ln = lines[data]
        if s.owner == SHARED:
            if ln.kind == "pp":
                err("R1-shared", s, f"shared step on push-pull line {ln.name!r}")
        elif ln.kind == "pp" and ln.owner != _owner(desc, s.owner):
            err("R1-owner", s, f"{s.owner!r} drives push-pull line {ln.name!r} owned by "
                               f"{_name(desc, ln.owner)!r}")
        if clk is not None and lines[clk].owner not in (Owner.P0, Owner.P1):
            err("R1-owner", s, f"clock line {lines[clk].name!r} needs a party owner")
        if clk is not None and lines[clk].kind == "od" and lines[clk].idle != 1:
            err("S-lane", s, f"OD clock line {lines[clk].name!r} rests released, so it idles at 1 "
                             f"(and its lane has cpol 1)")
        elif clk is not None and lines[clk].idle != la.cpol:
            # engines tell a leading edge by the level the clock goes to (isa §4.3)
            err("S-lane", s, f"clock line {lines[clk].name!r} idles at {lines[clk].idle}, "
                             f"not at the lane's cpol {la.cpol}")
        if s.kind == "xfer" and isinstance(ref, LaneRef) and la.stretch and (clk is None or lines[clk].kind != "od"):
            err("S-bus", s, f"lane {la.name!r} stretches, so its clock must be an OD line")
        if s.kind == "xfer" and isinstance(ref, LaneRef):
            used = [x.index for x in (la.data, la.clk, la.data2) if x is not None]
            if len(set(used)) != len(used):
                err("S-lane", s, f"lane {la.name!r} uses one line twice (data, clk and data2 are "
                                 f"distinct lines)")
        if s.kind == "xfer" and isinstance(ref, LaneRef) and la.data2 is not None:
            d2 = lines[la.data2.index]
            if clk is None:
                err("S-lane", s, f"data2 line {d2.name!r} on an unclocked lane (duplex needs a clock)")
            # one party per data line: the step has a party owner and data2 belongs to the other
            if (s.owner == SHARED or d2.owner not in (Owner.P0, Owner.P1)
                    or d2.owner == _owner(desc, s.owner)):
                err("R1-owner", s, f"duplex: the step needs a party owner, and data2 line {d2.name!r} "
                                   f"the other party")
            if a["n"] > 8:
                err("S-range", s, f"duplex XFER of {a['n']} bits: dst packs both lines, so n <= 8")
        if s.kind == "xfer" and clk is None and a["abort"] and not a["check"] and desc.bus_cfg is None:
            err("S-flags", s, "abort on an unclocked xfer needs check: an out-of-window edge is its "
                              "only fatal failure")
    if s.kind == "hold" and a["level"] == 1 and any(
            la.stretch and la.clk is not None and la.clk.index == a["line"].index for la in desc.lanes):
        err("S-bus", s, f"HOLD 1 on stretch clock {lines[a['line'].index].name!r}: its followers would "
                        f"keep the unstretched timeline; release it with COND RISE")
    if s.kind in ("raw_set", "raw_rel"):
        ln = lines[a["line"].index]
        if ln.kind == "pp":
            allowed = s.block is not None and s.block <= {_name(desc, ln.owner)}
            if not allowed:
                err("R3-raw-drive", s, f"raw drive of push-pull line {ln.name!r} outside an "
                                       f"only({_name(desc, ln.owner)!r}) block")


def _check_lead(desc, sp: Span, instrs: list[Instr], pc_step: list[int], abort_pc: int,
                profiles, lines: list[LineCfg], lanes: list[LaneCfg], err) -> None:
    """R3-lead (isa §5). The others skip the block and follow its fall-through successor S
    speculatively, so S has ABORT, no CHK and max INF. The leader leaves by the fall-through exit
    with no timed step on the way, or runs a bus COND START/STOP and branches to abort_pc. Every
    timed step in the block is the leader's (owned, or on a lane it clocks), except the bus COND,
    which the master line's owner owns (R1-bus).

    When S's events come from the leader (it clocks S, or owns S), the others wait in S for the
    leader's bus COND, so an exit path may not complete S for them first: fewer clock edges than
    S needs, no edge on S's COND line, and a START successor only behind a STOP exit whose way
    back from abort_pc reaches S before any START. A COND STOP, HOLD or unclocked XFER of the
    leader's completes without it, so it can't be S."""
    from .analysis import successors  # analysis imports this module

    (leader,) = sp.roles
    at = lambda pc: desc.steps[pc_step[pc]]  # noqa: E731
    apc = abort_pc if desc.abort_label is not None else None
    bus_a = desc.bus_cfg[1].index if desc.bus_cfg is not None else None

    def mine(owner) -> bool:
        return int(owner) == int(leader)

    def drives(ins: Instr, line: int) -> bool:
        """The leader drives `line` in this timed step."""
        if ins.op in (Op.COND, Op.HOLD):
            return ins.line == line and (mine(ins.owner) or ins.owner == Owner.SHARED)
        la = lanes[ins.line] if ins.line < len(lanes) else None
        return la is not None and (
            (la.data == line and (mine(ins.owner) or ins.owner == Owner.SHARED))
            or (la.data2 == line and mine(lines[line].owner))
            or (la.clk == line and mine(lines[line].owner)))

    def edges_on(ins: Instr, line: int) -> int:
        if ins.op == Op.XFER and ins.line < len(lanes) and lanes[ins.line].clk == line:
            return 2 * unpack_xfer(ins.operand)[0] if mine(lines[line].owner) else 0
        return 1 if drives(ins, line) else 0

    succ = instrs[sp.end] if sp.end < len(instrs) else None
    kind, watch, need = None, None, 0  # what S waits for, when its events come from the leader
    if succ is None or succ.op not in TIMED_OPS:
        err("R3-lead", at(sp.role_if_pc), "a lead block's fall-through successor must be a timed step: "
                                          "the others follow it while the leader decides")
        succ = None
    elif not succ.flags & F_ABORT or succ.flags & F_CHK or profiles[succ.prof].max != INF:
        err("R3-lead", at(sp.end), "the step after a lead block is followed speculatively: it needs "
                                   "abort, no check and a profile with max INF")
    if succ is not None:
        clocked = succ.op == Op.XFER and succ.line < len(lanes) and lanes[succ.line].clk is not None
        if clocked:
            if mine(lines[lanes[succ.line].clk].owner):
                kind, watch, need = "clock", lanes[succ.line].clk, 2 * unpack_xfer(succ.operand)[0]
        elif mine(succ.owner):
            ck = unpack_cond(succ.operand)[0] if succ.op == Op.COND else None
            if ck in (CondKind.FALL, CondKind.RISE) and succ.line != bus_a:
                kind, watch, need = "line", succ.line, 1
            elif ck == CondKind.START:
                kind = "start"
            else:
                err("R3-lead", at(sp.end), "the step after a lead block must wait for the leader: a "
                                           "COND STOP, HOLD, unclocked XFER, or COND on the bus line "
                                           "would complete before the leader's exit reaches its bus COND")

    def back_to_start_ok() -> bool:
        """From abort_pc the leader reaches S before any COND START of its own."""
        seen, stack = set(), [apc]
        while stack:
            pc = stack.pop()
            if pc in seen or pc >= len(instrs):
                continue
            seen.add(pc)
            if pc == sp.end:
                continue
            ins = instrs[pc]
            if ins.op == Op.COND and unpack_cond(ins.operand)[0] == CondKind.START:
                return False
            stack.extend(successors(instrs, pc, leader))
        return True

    def leaders(ins: Instr) -> bool:
        if ins.owner in (Owner.P0, Owner.P1) and mine(ins.owner):
            return True
        if ins.op == Op.XFER and ins.line < len(lanes):
            clk = lanes[ins.line].clk
            return clk is not None and mine(lines[clk].owner)
        return False

    seen: set[tuple] = set()
    # (pc, a timed step so far, the exit's bus COND kind so far, edges the leader made on S's watch)
    stack: list[tuple] = [(sp.start, False, None, 0)]
    while stack:
        state = stack.pop()
        if state in seen:
            continue
        seen.add(state)
        pc, timed, bus, edges = state
        if apc is not None and pc == apc and bus is not None:  # the exit to abort_pc, checked first
            if kind == "start" and (bus == CondKind.START or not back_to_start_ok()):
                err("R3-lead", at(sp.end), "a START successor waits through the exit's STOP only: the "
                                           "exit's bus COND must be a STOP, and from abort_pc the leader "
                                           "must reach this START before any other")
            continue
        if pc == sp.end:
            if timed:
                err("R3-lead", at(sp.end), "a timed step lies on a path to the lead block's fall-through "
                                           "exit; the others are already in the next step")
            continue
        if not sp.role_if_pc <= pc < sp.end:
            if pc == apc:
                err("R3-lead", at(sp.role_if_pc), "a lead block's exit to abort_pc must run a bus COND "
                                                  "START/STOP first, so the others leave their step too")
            continue  # any other target is R3-escape's
        ins = instrs[pc]
        if ins.op == Op.HALT:
            err("R3-lead", at(pc), "a lead block leaves by its exit, or by a bus COND START/STOP and a "
                                   "branch to abort_pc; not by HALT")
            continue
        if ins.op in TIMED_OPS:
            ck = unpack_cond(ins.operand)[0] if ins.op == Op.COND else None
            if bus is not None:
                err("R3-lead", at(pc), "nothing timed between the exit's bus COND and its branch to "
                                       "abort_pc: the others have already left")
                continue
            if ck in (CondKind.START, CondKind.STOP):
                bus = ck
                if kind == "clock" and watch == bus_a:
                    edges += 1
            else:
                if not leaders(ins):
                    err("R3-lead", at(pc), f"a timed step in a lead block is the leader's: owned by "
                                           f"{_name(desc, Owner(int(leader)))!r}, or on a lane it clocks")
                if kind in ("clock", "line"):
                    edges = min(need, edges + edges_on(ins, watch))
                    if edges >= need:
                        err("R3-lead", at(pc), "this exit path completes the lead block's successor for "
                                               "the others before its bus COND (as many edges as it waits "
                                               "for), so they would run on past it")
                        continue
            timed = True
        stack.extend((nxt, timed, bus, edges) for nxt in successors(instrs, pc, leader))


def _name(desc, owner: Owner) -> str:
    return SHARED if owner == Owner.SHARED else desc.parties[int(owner)]


def _instr(desc, s: Step, a: dict, target, lane_index) -> Instr:
    reg = lambda r: int(r[1])  # noqa: E731
    owner = _owner(desc, s.owner)
    prof = 0 if a.get("prof") is None else a["prof"].index
    flags = (F_ABORT if a.get("abort") else 0) | (F_CHK if a.get("check") else 0) | \
            (F_RESYNC if a.get("resync") else 0)
    k = s.kind
    if k == "cond":
        meas = None if a.get("meas") is None else reg(a["meas"])
        line = 0 if a["line"] is None else a["line"].index  # START/STOP: line field 0 (isa §3)
        return Instr(Op.COND, owner, line, prof, operand=cond_operand(CondKind[a["kind"].upper()], meas),
                     flags=flags)
    if k == "hold":
        return Instr(Op.HOLD, owner, a["line"].index, prof, operand=a["level"], flags=flags)
    if k == "xfer":
        operand = xfer_operand(a["n"], Src[a["src"].upper()], Dst[a["dst"].upper()])
        return Instr(Op.XFER, owner, lane_index(a["lane"]), prof, operand=operand,
                     flags=flags | (F_MSB if a["msb_first"] else 0))
    if k == "li":
        return li(reg(a["rd"]), _imm(a["imm"], 0xFFFF))
    if k == "alu":
        b = a["b"]
        isreg = isinstance(b, str)
        return Instr(Op.ALU, rd=reg(a["rd"]), ra=reg(a["ra"]), sub=int(AluOp[a["op"].upper()]),
                     operand=reg(b) if isreg else _imm(b), flags=F_REG if isreg else 0)
    if k == "br":
        return Instr(Op.BR, ra=0 if a["reg"] is None else reg(a["reg"]),
                     sub=int(BrCond[a["cond"].upper()]), operand=target(a["target"]))
    if k == "push":
        return Instr(Op.PUSH, ra=reg(a["ra"]))
    if k == "pop":
        return Instr(Op.POP, rd=reg(a["rd"]))
    if k == "mark":
        return Instr(Op.MARK, operand=_imm(a["id"]))
    if k in ("fz_next", "fz_end"):
        return Instr(Op.FZ, rd=reg(a["rd"]), sub=int(FzOp.NEXT if k == "fz_next" else FzOp.END))
    if k == "fz_tok":
        return Instr(Op.FZ, ra=reg(a["ra"]), sub=int(FzOp.TOK))
    if k == "halt":
        return Instr(Op.HALT)
    if k == "nop":
        return Instr(Op.NOP)
    if k == "raw_set":
        return Instr(Op.RAW, ra=a["line"].index, sub=int(RawOp.SET), operand=a["level"] & 1)
    if k == "raw_rel":
        return Instr(Op.RAW, ra=a["line"].index, sub=int(RawOp.REL))
    if k == "raw_get":
        return Instr(Op.RAW, rd=reg(a["rd"]), ra=a["line"].index, sub=int(RawOp.GET))
    if k == "raw_delay":
        n = a["n"]
        if not 1 <= n <= 0xFFF:
            raise ValueError(f"raw_delay {n} not in 1..4095")
        return Instr(Op.RAW, sub=int(RawOp.DELAY), operand=n)
    if k == "raw_evt":
        if not 0 <= a["mask"] <= 0x3F:
            raise ValueError(f"raw_evt clear mask {a['mask']} not in 0..63")
        return Instr(Op.RAW, rd=reg(a["rd"]), sub=int(RawOp.EVT), operand=a["mask"])
    if k == "raw_jpin":
        t = target(a["target"])
        if t >= 2048:
            raise ValueError("raw_jpin target must be < 2048")
        return Instr(Op.RAW, ra=a["line"].index, sub=int(RawOp.JPIN), operand=(a["level"] & 1) << 11 | t)
    raise ValueError(f"unknown step kind {k!r}")


def _imm(v: int, hi: int = 0xFFF) -> int:
    if not isinstance(v, int) or not 0 <= v <= hi:
        raise ValueError(f"immediate {v!r} not in 0..{hi}")
    return v


def _summary(s: Step) -> str:
    parts = []
    for k, v in s.args.items():
        if hasattr(v, "name"):
            v = v.name
        if v in (False, None):
            continue
        parts.append(f"{k}={v}" if v is not True else k)
    own = f"owner={s.owner} " if s.owner else ""
    return own + " ".join(parts)
