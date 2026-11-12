"""Random two-party descriptions, drawn as a recipe that `build` turns into a Description.

The recipe is plain data (JSON round-trips), so a failure can be saved, replayed and shrunk frame
by frame, and `expected_values` can say what each XFER must carry without the model. Registers are
split so that R2 holds by construction:
- r1-r4 take the wire values of frames (`dst`), start at 0 in every role (a prologue of LIs) and
  are the only branch registers besides the loop counter r0. A frame on an OD line never writes
  them: the owner reads back the wire, so isa §5 R2 taints its `dst`.
- r5-r7 belong to the owner's prep blocks (`only(owner)`): LI, POP, ALU and RAW DELAY.
Everything else is left to chance, and the compiler's timing rules and R2-edge decide what is
accepted."""
from __future__ import annotations

import random
from collections import deque
from dataclasses import asdict, dataclass

from ..desc import Description
from ..isa import INF
from ..protocols import slack

PARTIES = ("a", "b")
WIRE_REGS = ("r1", "r2", "r3", "r4")
PREP_REGS = ("r5", "r6", "r7")
ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr")
MASK16 = 0xFFFF


@dataclass(frozen=True)
class Line:
    name: str
    kind: str    # "pp" | "od"
    owner: str   # "a" | "b"
    idle: int = 1


@dataclass(frozen=True)
class Frame:
    kind: str                      # "uart": start edge, HOLD, XFER, HOLD back on `line`; "clocked": an XFER on the lane
    owner: str                     # the step owner (the data line's owner)
    line: int                      # the data line (index into Recipe.lines)
    n: int
    src: str                       # "zero" | "ones" | "fifo" (clocked only) | a register
    dst: str                       # "discard" | "fifo" | a register
    msb: bool = False
    prep: tuple[tuple, ...] = ()   # ("li", rd, imm) | ("pop", rd) | ("alu", op, rd, imm) | ("delay", n), in only(owner)
    skip_on: str | None = None     # a wire register: BR NZ over this frame
    wait: tuple[int, int] | None = None  # the start COND's (nom, min), max INF; None: the default profile


@dataclass(frozen=True)
class Lane:
    data: int
    clk: int
    cpol: int
    cpha: int


@dataclass(frozen=True)
class Recipe:
    seed: int
    lines: tuple[Line, ...]
    frames: tuple[Frame, ...]
    P: int = 20                    # UART bit cycles
    H: int = 10                    # clocked half period
    lane: Lane | None = None
    ppm: int = 20000
    loops: int = 1                 # the frames run this many times (a backward BR NZ on r0)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Recipe":
        frames = tuple(Frame(**{**f, "prep": tuple(tuple(p) for p in f["prep"]),
                                "wait": tuple(f["wait"]) if f.get("wait") else None}) for f in d["frames"])
        return Recipe(d["seed"], tuple(Line(**ln) for ln in d["lines"]), frames, d["P"], d["H"],
                      Lane(**d["lane"]) if d["lane"] else None, d["ppm"], d.get("loops", 1))


def random_recipe(seed: int) -> Recipe:
    rng = random.Random(seed)
    n_data = rng.randint(2, 3)
    lines = []
    for i in range(n_data):
        od = i == 2 and rng.random() < 0.5
        lines.append(Line(f"d{i}", "od" if od else "pp", PARTIES[i % 2] if i < 2 else rng.choice(PARTIES),
                          idle=0 if not od and rng.random() < 0.25 else 1))
    lane = None
    if rng.random() < 0.5:
        lines.append(Line("clk", "pp", rng.choice(PARTIES)))
        data = 2 if n_data == 3 and rng.random() < 0.5 else rng.randrange(n_data)
        lane = Lane(data, len(lines) - 1, rng.randint(0, 1), rng.randint(0, 1))
    P = rng.randint(19, 40)
    frames: list[Frame] = []
    written: list[str] = []        # wire registers some frame writes
    for _ in range(rng.randint(1, 5)):
        clocked = lane is not None and rng.random() < 0.4
        li = lane.data if clocked else rng.randrange(n_data)
        owner = lines[li].owner
        od = lines[li].kind == "od"
        n = rng.randint(1, 8) if clocked else rng.randint(1, 10)
        prep: list[tuple] = []
        for _ in range(rng.choice((0, 0, 1, 2, 3))):
            rd = rng.choice(PREP_REGS)
            prep.append(rng.choice((("li", rd, rng.randrange(1 << 16)), ("pop", rd),
                                    ("alu", rng.choice(ALU_OPS), rd, rng.randrange(16)),
                                    ("delay", rng.randint(1, 2 * P)))))
        srcs = ["zero", "ones"] + (["fifo"] if clocked else []) + \
               [p[2] if p[0] == "alu" else p[1] for p in prep if p[0] != "delay"] + written
        dsts = ["discard", "fifo"] + ([] if od else list(WIRE_REGS))
        dst = rng.choice(dsts)
        skip = rng.choice(written) if written and rng.random() < 0.25 else None
        wait = None
        if not clocked and rng.random() < 0.3:
            nom = rng.randint(1, 3 * P)
            wait = (nom, rng.randint(0, nom))
        frames.append(Frame("clocked" if clocked else "uart", owner, li, n, rng.choice(srcs), dst,
                            rng.random() < 0.5, tuple(prep), skip, wait))
        if dst in WIRE_REGS and dst not in written:
            written.append(dst)
    return Recipe(seed, tuple(lines), tuple(frames), P=P, H=rng.randint(6, 25), lane=lane,
                  ppm=rng.choice((20000, 20000, 20000, 5000, 0)), loops=rng.choice((1, 1, 1, 2, 3)))


def build(r: Recipe) -> Description:
    d = Description(f"rand{r.seed}", parties=PARTIES, tolerance_ppm=r.ppm)
    refs = [d.line(ln.name, ln.kind, owner=ln.owner, idle=(r.lane.cpol if r.lane and i == r.lane.clk else ln.idle))
            for i, ln in enumerate(r.lines)]
    s = slack(r.P, r.ppm)
    bit = d.profile("BIT", nom=r.P, min=r.P - s, max=r.P + s)
    lane = half = None
    if r.lane is not None:
        lane = d.lane("L", refs[r.lane.data], clk=refs[r.lane.clk], cpol=r.lane.cpol, cpha=r.lane.cpha)
        half = d.profile("HALF", nom=r.H, min=0, max=INF)
    for reg in WIRE_REGS:
        d.li(reg, 0)
    if r.loops > 1:
        d.li("r0", r.loops)
        d.label("top")
    for i, f in enumerate(r.frames):
        if f.skip_on is not None:
            d.br(f"after{i}", cond="nz", reg=f.skip_on)
        if f.prep:
            with d.only(f.owner):
                for p in f.prep:
                    if p[0] == "li":
                        d.li(p[1], p[2])
                    elif p[0] == "pop":
                        d.pop(p[1])
                    elif p[0] == "alu":
                        d.alu(p[1], p[2], p[2], p[3])
                    else:
                        d.raw_delay(p[1])
        if f.kind == "uart":
            ln = refs[f.line]
            idle = r.lines[f.line].idle
            wait = d.profile(f"W{i}", nom=f.wait[0], min=f.wait[1], max=INF) if f.wait else None
            d.cond("fall" if idle else "rise", ln, owner=f.owner, **({"prof": wait} if wait else {}))
            d.hold(ln, 1 - idle, owner=f.owner, prof=bit)
            d.xfer(f.n, ln, owner=f.owner, prof=bit, src=f.src, dst=f.dst, msb_first=f.msb)
            d.hold(ln, idle, owner=f.owner, prof=bit)
        else:
            d.xfer(f.n, lane, owner=f.owner, prof=half, src=f.src, dst=f.dst, msb_first=f.msb)
        if f.skip_on is not None:
            d.label(f"after{i}")
    if r.loops > 1:
        d.alu("sub", "r0", "r0", 1)
        d.br("top", cond="nz", reg="r0")
    d.halt()
    return d


def _alu(op: str, a: int, b: int) -> int:
    """isa §4: the ALU ops on 16-bit registers."""
    return {"add": a + b, "sub": a - b, "and": a & b, "or": a | b, "xor": a ^ b,
            "shl": a << (b & 15), "shr": a >> (b & 15)}[op] & MASK16


def expected_values(r: Recipe, words: dict[str, list[int]]) -> list[int]:
    """Each XFER's value, in program order, from the recipe alone: the reference the model's
    records must match (the `value` oracle). `words` are each party's host words."""
    wire = {reg: 0 for reg in WIRE_REGS}
    prep = {p: {reg: 0 for reg in PREP_REGS} for p in PARTIES}
    queue = {p: deque(words[p]) for p in PARTIES}
    out: list[int] = []
    for _ in range(r.loops):
        for f in r.frames:
            if f.skip_on is not None and wire[f.skip_on]:
                continue
            regs = prep[f.owner]
            for p in f.prep:
                if p[0] == "li":
                    regs[p[1]] = p[2] & MASK16
                elif p[0] == "pop":
                    regs[p[1]] = queue[f.owner].popleft() & MASK16
                elif p[0] == "alu":
                    regs[p[2]] = _alu(p[1], regs[p[2]], p[3])
            mask = (1 << f.n) - 1
            if f.src == "zero":
                v = 0
            elif f.src == "ones":
                v = mask
            elif f.src == "fifo":
                v = queue[f.owner].popleft()
            else:
                v = (wire if f.src in wire else regs)[f.src]
            v &= mask
            out.append(v)
            if f.dst in wire:
                wire[f.dst] = v
    return out
