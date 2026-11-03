"""Description compiler: lowering, well-formedness rules, encoding, debug map.

Rule ids follow docs/design/isa-v0.md §5 (R*, S*) and §6 (T*, in analysis.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .desc import MON, SHARED, Description, LaneRef, LineRef, Step
from .image import IDLE, Image, LaneCfg, LineCfg
from .isa import (
    F_ABORT, F_CHK, F_REG, F_RESYNC, NUM_LANES, NUM_LINES, NUM_PROFILES, PROG_DEPTH,
    AluOp, BrCond, CondKind, Dst, Instr, Op, Owner, RawOp, Role, Src, encode, role_if_operand,
    role_mask, xfer_operand,
)

TIMED = ("cond", "hold", "xfer")
BRANCHES = ("br", "raw_jpin")


@dataclass(frozen=True)
class Diagnostic:
    rule: str
    index: int
    loc: str
    msg: str

    def __str__(self) -> str:
        return f"{self.loc}: [{self.rule}] step {self.index}: {self.msg}"


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

    lines = [LineCfg(ln.name, ln.kind, _owner(desc, ln.owner), ln.idle) for ln in desc.lines]
    profiles = [IDLE] + list(desc.profiles)
    lanes = [LaneCfg(la.data.index, None if la.clk is None else la.clk.index, la.cpol, la.cpha)
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
        while j < len(steps) and steps[j].block == s.block:
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
            spans.append(Span(pc, pc + 1, pc + 1 + extra, roles))
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

    # ---- pass 2: rules + instructions ----
    instrs: list[Instr] = []
    pc = 0
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
            if here is not None and not (here.start <= t <= here.end or t == here.role_if_pc):
                err("R3-escape", s, "branch leaves its role block")
            if here is None and there is not None:
                err("R3-escape", s, "branch enters a role block from outside")
            if here is not None and there is not None and here != there:
                err("R3-escape", s, "branch between role blocks")
        instrs.append(ins)
        pc += 1

    if len(profiles) > NUM_PROFILES or len(lanes) > NUM_LANES or len(lines) > NUM_LINES:
        anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
        err("S-table", anchor, f"tables exceed limits: {len(profiles)} profiles (max {NUM_PROFILES}), "
                               f"{len(lanes)} lanes (max {NUM_LANES}), {len(lines)} lines (max {NUM_LINES})")
    if len(instrs) > prog_depth:
        err("S-range", desc.steps[-1], f"program has {len(instrs)} words (max {prog_depth})")

    abort_pc = 0
    if desc.abort_label is not None:
        if desc.abort_label in labels:
            abort_pc = labels[desc.abort_label]
        else:
            anchor = desc.steps[0] if desc.steps else Step("nop", {}, None, None, "?", 0)
            err("S-label", anchor, f"unknown abort label {desc.abort_label!r}")

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
    image = Image(desc.name, desc.parties, words, profiles, lines, lanes, abort_pc, desc.tolerance_ppm, debug)
    return Lowered(instrs, image, spans, pc_step, diags)


def compile_desc(desc: Description, *, check_timing: bool = True,
                 prog_depth: int = PROG_DEPTH) -> Image:
    low = lower(desc, prog_depth)
    diags = list(low.diags)
    if not diags:
        from . import analysis  # imports this module; deferred to avoid a cycle
        diags += analysis.check_branches(desc, low)
        if check_timing:
            diags += analysis.check_timing(desc, low)
    if diags:
        raise CompileError(diags)
    return low.image


# ---------------------------------------------------------------------------------------
def _check_rules(desc, s: Step, lines: list[LineCfg], lanes: list[LaneCfg], err) -> None:
    a = s.args
    if s.kind in TIMED:
        if s.block is not None:
            err("R3-timed-in-role", s, f"{s.kind} inside a role block")
        if s.owner is None:
            err("R1-owner", s, "timed step without an owner")
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
    if s.kind in ("raw_set", "raw_rel"):
        ln = lines[a["line"].index]
        if ln.kind == "pp":
            allowed = s.block is not None and s.block <= {_name(desc, ln.owner)}
            if not allowed:
                err("R3-raw-drive", s, f"raw drive of push-pull line {ln.name!r} outside an "
                                       f"only({_name(desc, ln.owner)!r}) block")


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
        return Instr(Op.COND, owner, a["line"].index, prof, int(CondKind[a["kind"].upper()]), flags)
    if k == "hold":
        return Instr(Op.HOLD, owner, a["line"].index, prof, a["level"], flags)
    if k == "xfer":
        operand = xfer_operand(a["n"], Src[a["src"].upper()], Dst[a["dst"].upper()], a["msb_first"])
        return Instr(Op.XFER, owner, lane_index(a["lane"]), prof, operand, flags)
    if k == "li":
        return Instr(Op.LI, pin=reg(a["rd"]), operand=_imm(a["imm"]))
    if k == "alu":
        b = a["b"]
        isreg = isinstance(b, str)
        return Instr(Op.ALU, pin=reg(a["rd"]) | (reg(a["ra"]) << 2), prof=int(AluOp[a["op"].upper()]),
                     operand=reg(b) if isreg else _imm(b), flags=F_REG if isreg else 0)
    if k == "br":
        return Instr(Op.BR, pin=0 if a["reg"] is None else reg(a["reg"]),
                     prof=int(BrCond[a["cond"].upper()]), operand=target(a["target"]))
    if k == "push":
        return Instr(Op.PUSH, pin=reg(a["ra"]))
    if k == "pop":
        return Instr(Op.POP, pin=reg(a["rd"]))
    if k == "mark":
        return Instr(Op.MARK, operand=_imm(a["id"]))
    if k == "halt":
        return Instr(Op.HALT)
    if k == "nop":
        return Instr(Op.NOP)
    if k == "raw_set":
        return Instr(Op.RAW, pin=a["line"].index, prof=int(RawOp.SET), operand=a["level"] & 1)
    if k == "raw_rel":
        return Instr(Op.RAW, pin=a["line"].index, prof=int(RawOp.REL))
    if k == "raw_get":
        return Instr(Op.RAW, pin=a["line"].index, prof=int(RawOp.GET), operand=reg(a["rd"]))
    if k == "raw_delay":
        n = a["n"]
        if not 1 <= n <= 0xFFF:
            raise ValueError(f"raw_delay {n} not in 1..4095")
        return Instr(Op.RAW, prof=int(RawOp.DELAY), operand=n)
    if k == "raw_jpin":
        t = target(a["target"])
        if t >= 2048:
            raise ValueError("raw_jpin target must be < 2048")
        return Instr(Op.RAW, pin=a["line"].index, prof=int(RawOp.JPIN), operand=(a["level"] & 1) << 11 | t)
    raise ValueError(f"unknown step kind {k!r}")


def _imm(v: int) -> int:
    if not isinstance(v, int) or not 0 <= v <= 0xFFF:
        raise ValueError(f"immediate {v!r} not in 0..4095")
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
