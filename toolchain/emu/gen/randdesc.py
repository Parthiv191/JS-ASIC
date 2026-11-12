"""Random two-party descriptions, drawn as a recipe that `build` turns into a Description.

The recipe is plain data (JSON round-trips), so a failure can be saved, replayed and shrunk frame
by frame. Registers are split so that R2 holds by construction:
- r1-r4 take the wire values of frames (`dst`), start at 0 in every role (a prologue of LIs) and
  are the only branch registers. A frame on an OD line never writes them: the owner reads back the
  wire, so isa §5 R2 taints its `dst`.
- r5-r7 belong to the owner's prep blocks (`only(owner)`): LI, POP and ALU compute a value to send.
Everything else is left to chance, and the compiler's timing rules decide what is accepted."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass

from ..desc import Description
from ..isa import INF
from ..protocols import slack

PARTIES = ("a", "b")
WIRE_REGS = ("r1", "r2", "r3", "r4")
PREP_REGS = ("r5", "r6", "r7")
ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr")


@dataclass(frozen=True)
class Line:
    name: str
    kind: str    # "pp" | "od"
    owner: str   # "a" | "b"


@dataclass(frozen=True)
class Frame:
    kind: str                      # "uart": COND fall, HOLD 0, XFER, HOLD 1 on `line`; "clocked": an XFER on the lane
    owner: str                     # the step owner (the data line's owner)
    line: int                      # the data line (index into Recipe.lines)
    n: int
    src: str                       # "zero" | "ones" | "fifo" | a register
    dst: str                       # "discard" | "fifo" | a register
    msb: bool = False
    prep: tuple[tuple, ...] = ()   # ("li", rd, imm) | ("pop", rd) | ("alu", op, rd, imm), in only(owner)
    skip_on: str | None = None     # a wire register: BR NZ over this frame


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

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Recipe":
        frames = tuple(Frame(**{**f, "prep": tuple(tuple(p) for p in f["prep"])}) for f in d["frames"])
        return Recipe(d["seed"], tuple(Line(**ln) for ln in d["lines"]), frames, d["P"], d["H"],
                      Lane(**d["lane"]) if d["lane"] else None, d["ppm"])


def random_recipe(seed: int) -> Recipe:
    rng = random.Random(seed)
    n_data = rng.randint(2, 3)
    lines = [Line(f"d{i}", "od" if i == 2 and rng.random() < 0.5 else "pp",
                  PARTIES[i % 2] if i < 2 else rng.choice(PARTIES)) for i in range(n_data)]
    lane = None
    if rng.random() < 0.5:
        clk_owner = rng.choice(PARTIES)
        lines.append(Line("clk", "pp", clk_owner))
        lane = Lane(rng.randrange(n_data), len(lines) - 1, rng.randint(0, 1), rng.randint(0, 1))
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
                                    ("alu", rng.choice(ALU_OPS), rd, rng.randrange(16)))))
        srcs = ["zero", "ones", "fifo"] + [p[2] if p[0] == "alu" else p[1] for p in prep] + written
        dsts = ["discard", "fifo"] + ([] if od else list(WIRE_REGS))
        dst = rng.choice(dsts)
        skip = rng.choice(written) if written and rng.random() < 0.25 else None
        frames.append(Frame("clocked" if clocked else "uart", owner, li, n, rng.choice(srcs), dst,
                            rng.random() < 0.5, tuple(prep), skip))
        if dst in WIRE_REGS and dst not in written:
            written.append(dst)
    return Recipe(seed, tuple(lines), tuple(frames), P=rng.randint(19, 40), H=rng.randint(6, 25),
                  lane=lane, ppm=rng.choice((20000, 20000, 20000, 5000, 0)))


def build(r: Recipe) -> Description:
    d = Description(f"rand{r.seed}", parties=PARTIES, tolerance_ppm=r.ppm)
    refs = [d.line(ln.name, ln.kind, owner=ln.owner, idle=(r.lane.cpol if r.lane and i == r.lane.clk else 1))
            for i, ln in enumerate(r.lines)]
    s = slack(r.P, r.ppm)
    bit = d.profile("BIT", nom=r.P, min=r.P - s, max=r.P + s)
    lane = half = None
    if r.lane is not None:
        lane = d.lane("L", refs[r.lane.data], clk=refs[r.lane.clk], cpol=r.lane.cpol, cpha=r.lane.cpha)
        half = d.profile("HALF", nom=r.H, min=0, max=INF)
    for reg in WIRE_REGS:
        d.li(reg, 0)
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
                    else:
                        d.alu(p[1], p[2], p[2], p[3])
        if f.kind == "uart":
            ln = refs[f.line]
            d.cond("fall", ln, owner=f.owner)
            d.hold(ln, 0, owner=f.owner, prof=bit)
            d.xfer(f.n, ln, owner=f.owner, prof=bit, src=f.src, dst=f.dst, msb_first=f.msb)
            d.hold(ln, 1, owner=f.owner, prof=bit)
        else:
            d.xfer(f.n, lane, owner=f.owner, prof=half, src=f.src, dst=f.dst, msb_first=f.msb)
        if f.skip_on is not None:
            d.label(f"after{i}")
    d.halt()
    return d
