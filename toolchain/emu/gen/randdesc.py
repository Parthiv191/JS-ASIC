"""Random two-party descriptions, drawn as a recipe that `build` turns into a Description.

The recipe is plain data (JSON round-trips), so a failure can be saved, replayed and shrunk frame
by frame, and `expected_values` can say what each XFER must carry without the model. Registers are
split so that R2 holds by construction:
- r1-r4 take the wire values of frames (`dst`), start at 0 in every role (a prologue of LIs) and
  are the only branch registers besides the loop counter r0. A frame on an OD line never writes
  them: the owner reads back the wire, so isa §5 R2 taints its `dst`.
- r5-r7 belong to the owner's prep blocks (`only(owner)`): LI, POP, ALU and RAW DELAY.
Everything else is left to chance, and the compiler's timing rules and R2-edge decide what is
accepted.

Two families. `random_recipe` draws UART-style frames and clocked XFERs on point-to-point lines.
`random_bus_recipe` draws an I2C bus, where the controller a owns SCL: transactions (START, bytes
and ACKs, a STOP or a repeated START) built from clocked XFERs and from plain COND, HOLD and RAW
drives on SDA, SCL and a spare line, with random drives and edits mixed in and HALTs in role
blocks. There R1-bus's level walk decides too (isa §5): only COND START/STOP may change SDA while
SCL may be high."""
from __future__ import annotations

import random
from collections import deque
from dataclasses import asdict, dataclass, replace

from ..desc import Description
from ..isa import INF
from ..protocols import slack

PARTIES = ("a", "b")
WIRE_REGS = ("r1", "r2", "r3", "r4")
PREP_REGS = ("r5", "r6", "r7")
ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr")
MASK16 = 0xFFFF
HOST_WORDS = 24      # per party, the host words a case gives: more than any recipe pops


@dataclass(frozen=True)
class Line:
    name: str
    kind: str    # "pp" | "od"
    owner: str   # "a" | "b"
    idle: int = 1


@dataclass(frozen=True)
class Frame:
    # "uart": start edge, HOLD, XFER, HOLD back on `line`; "clocked": an XFER on the lane. The bus
    # family's steps: "start" | "stop" (a bus COND), "cond" (`edge` on `line`), "hold" (`level` on
    # `line`), "raw" (in only(owner): RAW SET `level`, or REL if None), "halt" (in only(owner)) and
    # "li" (every role: LI `dst`, `imm`, a wire register for `skip_on`)
    kind: str
    owner: str                     # the step owner (the data line's owner)
    line: int                      # the data line (index into Recipe.lines)
    n: int
    src: str                       # "zero" | "ones" | "fifo" (clocked only) | a register
    dst: str                       # "discard" | "fifo" | a register
    msb: bool = False
    prep: tuple[tuple, ...] = ()   # ("li", rd, imm) | ("pop", rd) | ("alu", op, rd, imm) | ("delay", n), in only(owner)
    skip_on: str | None = None     # a wire register: BR NZ over this frame
    wait: tuple[int, int] | None = None  # a COND's (nom, min), max INF (None: the default profile); a HOLD's nom
    edge: str | None = None        # "cond": "fall" | "rise"
    level: int | None = None       # "hold": 0 | 1; "raw": SET 0 | 1, None: REL
    imm: int = 0                   # "li"


@dataclass(frozen=True)
class Lane:
    data: int
    clk: int
    cpol: int
    cpha: int
    ldelay: int = 0


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
    bus: str | None = None         # "i2c": lane.data is SDA and lane.clk SCL (the bus family)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Recipe":
        frames = tuple(Frame(**{**f, "prep": tuple(tuple(p) for p in f["prep"]),
                                "wait": tuple(f["wait"]) if f.get("wait") else None}) for f in d["frames"])
        return Recipe(d["seed"], tuple(Line(**ln) for ln in d["lines"]), frames, d["P"], d["H"],
                      Lane(**d["lane"]) if d["lane"] else None, d["ppm"], d.get("loops", 1), d.get("bus"))


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


SDA, SCL, X = 0, 1, 2   # the bus family's lines: SDA (OD, shared), SCL (OD, a's) and x (push-pull)


def needs(f: Frame, p: str) -> bool:
    """Whether frame `f` needs party `p` running: its owner's, and a's for a bus COND or a clocked
    XFER (a owns SCL). Once p halts, no frame may need it: the others would wait for it forever,
    which no rule checks (a party's HALT in a role block ends its part)."""
    if f.kind == "li":
        return False
    return f.owner in (p, "shared") or (p == "a" and f.kind in ("start", "stop", "clocked"))


def random_bus_recipe(seed: int) -> Recipe:
    """An I2C bus (see the module doc). The transactions are drawn as a controller would drive
    them, so a fair share compiles; the random drives and edits put SDA and SCL changes, levels and
    owners where the rules must say no."""
    rng = random.Random(seed)
    x_owner = rng.choice(PARTIES)
    lines = (Line("sda", "od", "shared"), Line("scl", "od", "a"), Line("x", "pp", x_owner))
    H = rng.randint(20, 40) if rng.random() < 0.75 else rng.randint(10, 19)  # the first bit after a START
    # comes about 20 cycles after it at the followers, who confirm it (isa §4.7, §6 T5-watch)
    # SDA moves ldelay after SCL falls; most fit in the half period (isa §6 T3-latency, T4-profile)
    ldelay = rng.choice([v for v in (0, 1, 2, 4, 12, 15) if v <= H - 12] or [0]) if rng.random() < 0.8 else         rng.choice((0, 1, 2, 4, 12, 15))
    waits = [(nom, rng.randint(0, nom)) for nom in rng.sample(range(1, 3 * H + 1), 2)]
    holds = rng.sample(range(2, 3 * H + 1), 2)
    other = {"a": "b", "b": "a"}
    fr: list[Frame] = []
    x_level = [1]

    def step(kind: str, owner: str, line: int = SDA, **kw) -> None:
        fr.append(Frame(kind, owner, line, 0, "zero", "discard", **kw))

    def cond(line: int, edge: str, owner: str) -> None:
        step("cond", owner, line, edge=edge, wait=rng.choice(waits))

    def hold(line: int, level: int, owner: str) -> None:
        step("hold", owner, line, level=level, wait=(rng.choice(holds), 0))

    def drive(line: int, level: int, owner: str) -> None:
        """`line` to `level` by a HOLD or a RAW (SET, or REL up to 1 on an OD line)."""
        if rng.random() < 0.5:
            hold(line, level, "shared" if line == SDA and rng.random() < 0.1 else owner)
        elif level == 1 and line != X and rng.random() < 0.6:
            step("raw", owner, line, level=None)
        else:
            step("raw", owner, line, level=level)

    def spare() -> None:
        """An edge on x: a timed step its owner does, so the next step's owner starts on time."""
        cond(X, "fall" if x_level[0] else "rise", x_owner)
        x_level[0] ^= 1

    def xfer(owner: str, n: int, src: str | None = None) -> None:
        prep: list[tuple] = []
        if rng.random() < 0.2:
            rd = rng.choice(PREP_REGS)
            prep.append(rng.choice((("li", rd, rng.randrange(1 << 16)), ("pop", rd),
                                    ("alu", rng.choice(ALU_OPS), rd, rng.randrange(16)), ("delay", rng.randint(1, 2 * H)))))
        regs = [p[2] if p[0] == "alu" else p[1] for p in prep if p[0] != "delay"]
        fr.append(Frame("clocked", owner, SDA, n, src or rng.choice(["zero", "ones", "fifo"] + regs),
                        rng.choice(("discard", "fifo")), rng.random() < 0.5, tuple(prep)))

    def random_drive() -> None:
        line = rng.choice((SDA, SDA, SCL, X))
        owner = x_owner if line == X else "a" if line == SCL else rng.choice(PARTIES)
        if rng.random() < 0.4:
            cond(line, rng.choice(("fall", "rise")), owner)
        else:
            drive(line, rng.randint(0, 1), owner)

    def scl_pulse() -> None:
        """SCL low and up again right after a bus COND, now and then a spare edge first: in the
        followers' confirm window, unless enough time passes (isa §4.7)."""
        if rng.random() < 0.5:
            spare()
        drive(SCL, 0, "a")
        drive(SCL, 1, "a")

    for _ in range(rng.randint(1, 2)):
        step("start", "a", wait=rng.choice(waits))
        if rng.random() < 0.1:
            scl_pulse()
        for k in range(rng.randint(1, 3)):
            sender = "a" if k == 0 else rng.choice(PARTIES)
            xfer(sender, 8 if rng.random() < 0.7 else rng.randint(1, 8))
            if rng.random() < 0.85:
                xfer(other[sender], 1, src=rng.choice(("zero", "zero", "ones")))  # the ACK or NACK
            if rng.random() < 0.2:
                spare()
            if rng.random() < 0.15:  # a repeated START: SCL low, SDA up, SCL high
                cond(SCL, "fall", "a")
                drive(SDA, 1, "a")
                cond(SCL, "rise", "a")
                step("start", "a", wait=rng.choice(waits))
        end = rng.random()
        if end < 0.45:  # the library's STOP: a ZERO bit, then SDA up
            xfer("a", 1, src="zero")
        elif end < 0.9:  # by hand: SCL low, SDA low, SCL high
            cond(SCL, "fall", "a")
            if rng.random() < 0.3:
                spare()
            drive(SDA, 0, "a")
            cond(SCL, "rise", "a")
        step("stop", "a", wait=rng.choice(waits))
        if rng.random() < 0.1:
            scl_pulse()
        if rng.random() < 0.3:
            spare()
    for _ in range(rng.choice((0, 0, 0, 1, 1, 2, 3))):
        edit = rng.choice(("insert", "insert", "delete", "level", "owner", "swap"))
        k = rng.randrange(len(fr))
        f = fr[k]
        if edit == "insert":
            at = len(fr)
            random_drive()
            fr.insert(k, fr.pop(at))
        elif edit == "delete" and len(fr) > 1:
            del fr[k]
        elif edit == "level" and f.kind == "hold":
            fr[k] = replace(f, level=1 - f.level)
        elif edit == "level" and f.kind == "raw":  # SET 0 goes up (by SET 1, or REL on an OD line)
            fr[k] = replace(f, level=0 if f.level != 0 else None if f.line != X and rng.random() < 0.5 else 1)
        elif edit == "owner" and f.line == SDA and f.kind in ("cond", "hold", "raw"):
            fr[k] = replace(f, owner=other.get(f.owner, "a"))
        elif edit == "owner" and f.kind == "clocked":
            fr[k] = replace(f, owner=other[f.owner], prep=(), src="zero" if f.src in PREP_REGS else f.src)
        elif edit == "swap" and k + 1 < len(fr):
            fr[k], fr[k + 1] = fr[k + 1], f
    written: list[str] = []
    for _ in range(rng.choice((0, 0, 1, 2))):  # wire registers to branch on, equal in every role
        reg = rng.choice(WIRE_REGS)
        fr.insert(rng.randrange(len(fr) + 1), Frame("li", "a", SDA, 0, "zero", reg, imm=rng.choice((0, 1, 0xBEEF))))
    for k, f in enumerate(fr):
        if f.kind == "li":
            written.append(f.dst)
        elif written and rng.random() < 0.2:
            fr[k] = replace(f, skip_on=rng.choice(written))
    loops = 1
    if rng.random() < 0.35:  # a party halts in its own block, and no later frame needs it
        p = rng.choice(PARTIES)
        stops = [i + 1 for i, f in enumerate(fr) if f.kind == "stop"]
        k = rng.choice(stops) if stops and rng.random() < 0.5 else rng.randrange(len(fr) + 1)
        fr = fr[:k] + [Frame("halt", p, SDA, 0, "zero", "discard",
                             skip_on=rng.choice(written) if written and rng.random() < 0.3 else None)] + \
            [f for f in fr[k:] if not needs(f, p)]
    else:
        loops = rng.choice((1, 1, 1, 2))
    left = {p: HOST_WORDS // loops for p in PARTIES}  # a POP past the host's words would never end
    for k, f in enumerate(fr):
        prep = []
        for p in f.prep:
            left[f.owner] -= p[0] == "pop"
            prep.append(("li", p[1], 0) if p[0] == "pop" and left[f.owner] < 0 else p)
        f = fr[k] = replace(f, prep=tuple(prep))
        if f.kind == "clocked" and f.src == "fifo":
            left[f.owner] -= 1
            if left[f.owner] < 0:
                fr[k] = replace(f, src="ones")
    return Recipe(seed, lines, tuple(fr), H=H, lane=Lane(SDA, SCL, 1, 1, ldelay=ldelay),
                  ppm=rng.choice((20000, 20000, 5000, 0)), loops=loops, bus="i2c")


def build(r: Recipe) -> Description:
    d = Description(f"rand{r.seed}", parties=PARTIES, tolerance_ppm=r.ppm)
    refs = [d.line(ln.name, ln.kind, owner=ln.owner, idle=(r.lane.cpol if r.lane and i == r.lane.clk else ln.idle))
            for i, ln in enumerate(r.lines)]
    if r.bus is None:
        s = slack(r.P, r.ppm)
        bit = d.profile("BIT", nom=r.P, min=r.P - s, max=r.P + s)
    else:
        d.bus(r.bus, refs[r.lane.data], refs[r.lane.clk])
    lane = half = None
    if r.lane is not None:
        lane = d.lane("L", refs[r.lane.data], clk=refs[r.lane.clk], cpol=r.lane.cpol, cpha=r.lane.cpha,
                      ldelay=r.lane.ldelay)
        half = d.profile("HALF", nom=r.H, min=0, max=INF)
    profs: dict[tuple[int, int, int], object] = {}

    def prof(nom: int, lo: int, hi: int):
        """One profile per value: the chip has NUM_PROFILES."""
        if (nom, lo, hi) not in profs:
            profs[(nom, lo, hi)] = d.profile(f"P{len(profs)}", nom=nom, min=lo, max=hi)
        return profs[(nom, lo, hi)]

    def wait_kw(f: Frame) -> dict:
        return {"prof": prof(f.wait[0], f.wait[1], INF)} if f.wait else {}

    for reg in WIRE_REGS:
        d.li(reg, 0)
    if r.loops > 1:
        d.li("r0", r.loops)
        d.label("top")
    for i, f in enumerate(r.frames):
        if f.skip_on is not None:
            d.br(f"after{i}", cond="nz", reg=f.skip_on)
        if f.prep or f.kind in ("raw", "halt"):
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
                if f.kind == "raw" and f.level is None:
                    d.raw_rel(refs[f.line])
                elif f.kind == "raw":
                    d.raw_set(refs[f.line], f.level)
                elif f.kind == "halt":
                    d.halt()
        if f.kind == "uart":
            ln = refs[f.line]
            idle = r.lines[f.line].idle
            wait = d.profile(f"W{i}", nom=f.wait[0], min=f.wait[1], max=INF) if f.wait else None
            d.cond("fall" if idle else "rise", ln, owner=f.owner, **({"prof": wait} if wait else {}))
            d.hold(ln, 1 - idle, owner=f.owner, prof=bit)
            d.xfer(f.n, ln, owner=f.owner, prof=bit, src=f.src, dst=f.dst, msb_first=f.msb)
            d.hold(ln, idle, owner=f.owner, prof=bit)
        elif f.kind == "clocked":
            d.xfer(f.n, lane, owner=f.owner, prof=half, src=f.src, dst=f.dst, msb_first=f.msb)
        elif f.kind in ("start", "stop"):
            d.cond(f.kind, owner=f.owner, **wait_kw(f))
        elif f.kind == "cond":
            d.cond(f.edge, refs[f.line], owner=f.owner, **wait_kw(f))
        elif f.kind == "hold":
            nom = f.wait[0] if f.wait else r.H   # (the shrinker drops `wait`)
            d.hold(refs[f.line], f.level, owner=f.owner, prof=prof(nom, nom, nom))
        elif f.kind == "li":
            d.li(f.dst, f.imm)
        if f.skip_on is not None:
            d.label(f"after{i}")
            if f.kind in ("raw", "halt"):
                d.nop()  # or a next block of the same party's merges with this one, and the label is in it
    if r.loops > 1:
        d.alu("sub", "r0", "r0", 1)
        d.br("top", cond="nz", reg="r0")
    d.halt()
    return d


def expected_halts(r: Recipe) -> set[str]:
    """The parties that halt in their own block, from the recipe alone (a halt frame that its
    `skip_on` doesn't skip; halt frames come only with loops = 1)."""
    wire = {reg: 0 for reg in WIRE_REGS}
    out: set[str] = set()
    for f in r.frames:
        if f.skip_on is not None and wire[f.skip_on]:
            continue
        if f.kind == "li":
            wire[f.dst] = f.imm & MASK16
        elif f.kind == "halt":
            out.add(f.owner)
    return out


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
            regs = prep.get(f.owner)   # (a SHARED HOLD has no prep)
            for p in f.prep:
                if p[0] == "li":
                    regs[p[1]] = p[2] & MASK16
                elif p[0] == "pop":
                    regs[p[1]] = queue[f.owner].popleft() & MASK16
                elif p[0] == "alu":
                    regs[p[2]] = _alu(p[1], regs[p[2]], p[3])
            if f.kind == "li":
                wire[f.dst] = f.imm & MASK16
            if f.kind not in ("uart", "clocked"):
                continue
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
