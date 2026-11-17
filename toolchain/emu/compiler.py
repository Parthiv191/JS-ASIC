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
            _check_lead(desc, sp, instrs, pc_step, abort_pc, profiles, lines, lanes, err, spans)
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
        if kind == "select" and lines[a.index].idle != 1:
            # its owner drives idle from now = 1 (isa §2): an idle-0 CS would fall at start-up, a
            # START to every detector that no COND START made
            err("S-bus", anchor, f"a SELECT bus's CS line {lines[a.index].name!r} idles at 1 (a fall is START)")
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
        diags += analysis.check_end(desc, low)
        if check_levels:
            diags += analysis.check_levels(desc, low)
        if check_timing:
            diags += analysis.check_timing(desc, low)
    if diags:
        raise CompileError(diags)
    return low.image


# ---------------------------------------------------------------------------------------
def _drives(desc, s: Step) -> set[int]:
    """The lines a step may drive: a COND FALL/RISE's, a HOLD's or a RAW SET/REL's line, every
    line of an XFER's lane. (COND START/STOP name no line.)"""
    a = s.args
    if s.kind in ("cond", "hold", "raw_set", "raw_rel"):
        return {a["line"].index} if a.get("line") is not None else set()
    if s.kind == "xfer":
        ref = a["lane"]
        if isinstance(ref, LaneRef):
            la = desc.lanes[ref.index]
            return {x.index for x in (la.data, la.clk, la.data2) if x is not None}
        return {ref.index}
    return set()


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
    if (desc.bus_cfg is not None and desc.bus_cfg[0] == "select" and s.kind not in ("hold", "raw_set")
            and desc.bus_cfg[1].index in _drives(desc, s)):
        # isa §5 R1-bus: every follower's detector takes any edge of CS as a START or STOP (§4.7),
        # and only COND START/STOP are timed and tracked as one (a STOP releases the gated lines).
        # A HOLD or RAW SET may keep CS at its level: check_levels checks that it does
        err("R1-bus", s, f"only COND START/STOP change the SELECT line {lines[desc.bus_cfg[1].index].name!r}: "
                         f"any edge on it is a START or STOP to the others")
    if desc.bus_cfg is not None and desc.bus_cfg[0] == "i2c" and s.kind == "xfer":
        # isa §5 R1-bus: every follower's detector takes an SDA edge with SCL high as a START or STOP
        # (§4.7). The bus lane (cpol 1, cpha 1, clocked by SCL) changes SDA with SCL low; another XFER
        # on SDA may change it with SCL high, and a lane clocked by SDA makes its edges there
        sda, scl = desc.bus_cfg[1].index, desc.bus_cfg[2].index
        la = desc.lanes[ref.index] if isinstance(ref, LaneRef) else None   # ref, clk: the timed step's, above
        on = {ref.index} if la is None else {x.index for x in (la.data, la.data2) if x is not None}
        if clk == sda or sda in on and not (clk == scl and la.cpol == 1 and la.cpha == 1):
            err("R1-bus", s, f"only COND START/STOP change SDA {lines[sda].name!r} with SCL high: this XFER "
                             f"may change or clock on it then (only a cpol 1, cpha 1 lane clocked by SCL "
                             f"changes it with SCL low), a START or STOP to the others")
    if s.kind in ("raw_set", "raw_rel"):
        ln = lines[a["line"].index]
        if s.block is None or MON in s.block:
            # the walks that check levels (R2-edge, R1-bus) follow P0 and P1 only
            err("R3-raw-drive", s, f"raw drive of {ln.name!r} outside a role block, or in one MON runs: "
                                   f"only a role block without MON may drive a line by RAW")
        elif ln.kind == "pp":
            allowed = s.block is not None and s.block <= {_name(desc, ln.owner)}
            if not allowed:
                err("R3-raw-drive", s, f"raw drive of push-pull line {ln.name!r} outside an "
                                       f"only({_name(desc, ln.owner)!r}) block")


def _check_lead(desc, sp: Span, instrs: list[Instr], pc_step: list[int], abort_pc: int,
                profiles, lines: list[LineCfg], lanes: list[LaneCfg], err, spans: list[Span]) -> None:
    """R3-lead (isa §5). The others skip the block and follow its fall-through successor S
    speculatively, so S has ABORT, no CHK and max INF. The leader leaves by the fall-through exit
    with no timed step on the way, or runs a bus COND START/STOP and branches to abort_pc. Every
    timed step in the block is the leader's (owned, or on a lane it clocks), except the bus COND,
    which the master line's owner owns (R1-bus).

    When S's events come from the leader (it clocks S, or owns S), the others wait in S for the
    leader's bus COND, so an exit path may not complete S for them first: fewer clock edges than
    S needs, no edge on S's COND line, and a START successor only behind a STOP exit whose way
    back from abort_pc reaches S before any START. A COND STOP, HOLD or unclocked XFER of the
    leader's completes without it, so it can't be S. The exit's bus COND is the leader's own then:
    the others' S waits for the leader, and the master line's owner runs no detector to leave it.
    And an exit's own bus COND needs such an S: one the others complete alone lets them run on
    past it, and leader XFERs on another party's clock would wait for it there. A bus COND the
    leader follows is made by its owner from S, on a path that may not wait for the leader before
    it and goes on at abort_pc after it, as the leader does (a halt only leaves the leader
    waiting); the leader's XFERs on that owner's clock meanwhile take no more edges than S makes,
    and send from ONES on an OD line.
    Where the others send S's data on bus_a (I2C: a byte the target sends), the leader's first edge
    on S's clock launches their first bit, whose 0 holds bus_a so that the exit's bus COND makes
    no edge: an exit path makes that edge only past a BR that finds non-zero the register the
    leader read in the XFER just before the block, sent from ZERO (the target's ACK). The others,
    in S, ran it with the leader, so they would have read 0."""
    from .analysis import _taint, successors  # analysis imports this module

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
    s_from_others = False              # S's events come from another party: they do it on their own
    if succ is None or succ.op not in TIMED_OPS:
        err("R3-lead", at(sp.role_if_pc), "a lead block's fall-through successor must be a timed step: "
                                          "the others follow it while the leader decides")
        succ = None
    elif not succ.flags & F_ABORT or succ.flags & F_CHK or profiles[succ.prof].max != INF:
        err("R3-lead", at(sp.end), "the step after a lead block is followed speculatively: it needs "
                                   "abort, no check and a profile with max INF")
    if succ is not None:
        clocked = succ.op == Op.XFER and succ.line < len(lanes) and lanes[succ.line].clk is not None
        s_from_others = not (mine(lines[lanes[succ.line].clk].owner) if clocked else mine(succ.owner))
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

    # The others send S's data on bus_a (not data2: on I2C SDA's lane is the one OD lane, R1-od-lane,
    # and no lane uses SELECT's CS). The ACK: the XFER the ROLE_IF is entered from, and only from
    # (an exit that may make an edge branches to abort_pc, so abort_pc there is an entry too).
    launch, ack_reg = False, None
    p = sp.role_if_pc - 1  # -1 for a block at pc 0, which no entry set equals
    entries = {q for q in range(len(instrs)) if sp.role_if_pc in successors(instrs, q)}
    if kind == "clock":
        launch = (lanes[succ.line].data == bus_a and not mine(succ.owner)
                  and unpack_xfer(succ.operand)[1] != Src.ONES)
        if launch and instrs[p].op == Op.XFER and entries == {p}:
            _, asrc, adst = unpack_xfer(instrs[p].operand)
            if asrc == Src.ZERO and adst >= Dst.R0:
                ack_reg = adst - Dst.R0

    in_lead = {q for s in spans if s.lead and s.roles == sp.roles for q in range(s.start, s.end)}

    def back_to_start_ok() -> bool:
        """From abort_pc the leader reaches S before any COND START of its own, and before any
        timed step outside its lead blocks: the others, still in S, run none of them."""
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
            if ins.op in TIMED_OPS and pc not in in_lead:
                return False
            stack.extend(successors(instrs, pc, leader))
        return True

    said: set[tuple[int, str]] = set()

    def say(step, msg: str) -> None:
        """Once per step and message: the walk below may reach a pc in several states."""
        if (step.index, msg) not in said:
            said.add((step.index, msg))
            err("R3-lead", step, msg)

    def alone(ins: Instr, m: Owner) -> bool:
        """`m` makes this timed step's events itself: on a lane it clocks, or its own (or SHARED)."""
        if ins.op == Op.XFER and ins.line < len(lanes) and lanes[ins.line].clk is not None:
            return lines[lanes[ins.line].clk].owner == m
        return ins.owner in (m, Owner.SHARED)

    def goes_on_at_abort(pc: int, role: Role) -> bool:
        """From the COND at pc, `role` reaches abort_pc with nothing timed between, or halts."""
        seen, todo = set(), list(successors(instrs, pc, role))
        while todo:
            q = todo.pop()
            if q in seen or q == apc or q >= len(instrs) or instrs[q].op == Op.HALT:
                continue
            seen.add(q)
            if instrs[q].op in TIMED_OPS:
                return False
            todo.extend(successors(instrs, q, role))
        return True

    owner_said: dict[tuple, str | None] = {}

    def owner_path(m: Owner, follows: CondKind, abort_follow: bool, abort_before: bool) -> str | None:
        """What is wrong, if anything, on `m`'s role path from S (it skips the leader's blocks) to
        the COND START/STOP that ends the leader's exit: a timed step it can't do alone first, or
        going on after it elsewhere than at abort_pc, where the leader goes on. That COND is one of
        the kind the leader follows, or a START where that step has ABORT (a bus event, isa §4.2);
        otherwise the leader ignores it (a followed START ignores a STOP, and a followed STOP
        without ABORT waits through a START). Where an earlier step of the exit has ABORT, any of
        them may end it (a bus exit while the leader is still there), or not. A path that halts
        leaves the leader waiting, as a target waits for a controller that is done."""
        key = (m, follows, abort_follow, abort_before)
        if key in owner_said:
            return owner_said[key]
        role, out = Role(int(m)), None
        seen, todo = set(), [sp.end]
        while todo and out is None:
            pc = todo.pop()
            if pc in seen:
                continue
            seen.add(pc)
            if pc >= len(instrs) or instrs[pc].op == Op.HALT:
                continue
            ins = instrs[pc]
            ck = unpack_cond(ins.operand)[0] if ins.op == Op.COND else None
            if ck in (CondKind.START, CondKind.STOP):
                ends = ck == follows or ck == CondKind.START and abort_follow
                if (ends or abort_before) and not goes_on_at_abort(pc, role):
                    out = ("the leader follows the exit's bus COND and goes on at abort_pc: its owner, after "
                           "making it on its path from the block's successor, must go on there too "
                           "(nothing timed first), or halt")
                if ends:
                    continue
            elif ins.op in TIMED_OPS and not alone(ins, m):
                out = ("the leader follows the exit's bus COND, which its owner makes from the block's "
                       "successor on its own path; before it that path may not reach a timed step that "
                       "waits for the leader (not its own, nor on a lane it clocks): the leader isn't "
                       "there, and neither moves")
                continue
            todo.extend(successors(instrs, pc, role))
        owner_said[key] = out
        return out

    def leaders(ins: Instr) -> bool:
        if ins.owner in (Owner.P0, Owner.P1) and mine(ins.owner):
            return True
        if ins.op == Op.XFER and ins.line < len(lanes):
            clk = lanes[ins.line].clk
            return clk is not None and mine(lines[clk].owner)
        return False

    # S's edges on its clock: what the leader's XFERs on another party's clock may take meanwhile
    s_clk = lanes[succ.line].clk if succ is not None and succ.op == Op.XFER and succ.line < len(lanes) else None
    s_edges = 2 * unpack_xfer(succ.operand)[0] if s_clk is not None else 0
    HELD, OUT, LOST = 0, 1, 2  # the ACK's register still holds it; found non-zero; written since
    seen: set[tuple] = set()
    # (pc, a timed step so far, the exit's bus COND kind so far, edges the leader made on S's watch,
    # what the path knows of the ACK, edges its XFERs took on another party's clock (more than S
    # makes: s_edges + 1), whether one of them may send a 0 on an OD line, a step with ABORT so far,
    # whether the exit's bus COND so far is one the leader follows)
    stack: list[tuple] = [(sp.start, False, None, 0, HELD if ack_reg is not None else LOST, 0, False, False,
                           False)]
    while stack:
        state = stack.pop()
        if state in seen:
            continue
        seen.add(state)
        pc, timed, bus, edges, known, taken, sends, aborted, followed = state
        if apc is not None and pc == apc and bus is not None:  # the exit to abort_pc, checked first
            if kind == "start" and (bus == CondKind.START or not back_to_start_ok()):
                err("R3-lead", at(sp.end), "a START successor waits through the exit's STOP only: the "
                                           "exit's bus COND must be a STOP, and from abort_pc the leader "
                                           "must reach this START before any other, and before any timed "
                                           "step outside its lead blocks (the others are still in S)")
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
                bus, followed = ck, not mine(ins.owner)
                if mine(ins.owner):
                    if s_from_others:
                        say(at(sp.end), "the exit's bus COND is the leader's own, so the others must wait "
                                        "for the leader in the block's successor: this one completes "
                                        "without it, and they run on past it into steps the exit's bus "
                                        "COND doesn't take them out of")
                    if taken:
                        say(at(pc), "the leader's XFER on another party's clock in an exit whose bus COND is "
                                    "its own waits for that party's edges, while that party waits for the "
                                    "leader in the block's successor: neither moves")
                elif kind is not None:
                    err("R3-lead", at(pc), "the leader follows the exit's bus COND, but its owner waits for "
                                           "the leader in the block's successor and runs no bus detector "
                                           "to leave it (isa §4.7): neither moves")
                else:
                    if profiles[ins.prof].max != INF:
                        err("R3-lead", at(pc), "the leader follows the exit's bus COND, which its owner makes "
                                               "from the block's successor whenever it gets there: it needs "
                                               "max INF")
                    wrong = (owner_path(ins.owner, ck, bool(ins.flags & F_ABORT), aborted)
                             if succ is not None else None)
                    if wrong:
                        say(at(pc), wrong)
                    if taken > s_edges:
                        say(at(pc), "the leader's XFERs on another party's clock in this exit take that "
                                    "party's edges from the block's successor, which it clocks meanwhile: "
                                    "before the bus COND it follows they may take no more than S makes (2 "
                                    "per bit), or the leader is still in one when it comes")
                    if sends:
                        say(at(pc), "the leader's XFER on another party's clock in this exit sends while that "
                                    "party runs the block's successor: on an OD line it sends from ONES, as "
                                    "a 0 would hold the line (on SDA, the bus COND would make no edge)")
                if kind == "clock" and watch == bus_a:
                    edges += 1
            else:
                if not leaders(ins):
                    err("R3-lead", at(pc), f"a timed step in a lead block is the leader's: owned by "
                                           f"{_name(desc, Owner(int(leader)))!r}, or on a lane it clocks")
                la = lanes[ins.line] if ins.op == Op.XFER and ins.line < len(lanes) else None
                if la is not None and la.clk is not None and not mine(lines[la.clk].owner):
                    n_bits, xsrc = unpack_xfer(ins.operand)[:2]
                    taken = min(s_edges + 1, taken + (2 * n_bits if la.clk == s_clk else s_edges + 1))
                    sends = sends or (lines[la.data].kind == "od" and xsrc != Src.ONES)
                aborted = aborted or bool(ins.flags & F_ABORT)
                if launch and known != OUT and edges_on(ins, watch):
                    err("R3-lead", at(pc), "this exit path's edge on the successor's clock launches its "
                                           f"first bit at the others, who send it on {lines[bus_a].name!r}: "
                                           "a 0 there holds the line, so the exit's START or STOP makes "
                                           "no edge. An exit may make it only where a register the leader "
                                           "read in the XFER just before the block, sent from ZERO (an "
                                           "ACK), is not 0: the others, if in S, ran that XFER")
                    continue
                if kind in ("clock", "line"):
                    edges = min(need, edges + edges_on(ins, watch))
                    if edges >= need:
                        err("R3-lead", at(pc), "this exit path completes the lead block's successor for "
                                               "the others before its bus COND (as many edges as it waits "
                                               "for), so they would run on past it")
                        continue
            timed = True
        elif followed and not (ins.op == Op.BR and ins.sub == BrCond.ALWAYS):
            say(at(pc), "after the bus COND it follows, the leader branches to abort_pc at once, with "
                        "nothing between: its owner goes on there at once, and a leader that is late misses "
                        "the start of the next frame")
            continue
        if (known == HELD and ins.op == Op.BR and ins.ra == ack_reg and ins.sub in (BrCond.Z, BrCond.NZ)
                and ins.operand != pc + 1):
            nonzero = pc + 1 if ins.sub == BrCond.Z else ins.operand
            stack.extend((nxt, timed, bus, edges, OUT if nxt == nonzero else HELD, taken, sends, aborted,
                          followed) for nxt in successors(instrs, pc, leader))
            continue
        if known == HELD and _taint(ins, 0, True) >> ack_reg & 1:
            known = LOST
        stack.extend((nxt, timed, bus, edges, known, taken, sends, aborted, followed)
                     for nxt in successors(instrs, pc, leader))


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
