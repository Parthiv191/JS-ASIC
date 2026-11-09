"""Description DSL: a protocol written once, each step labeled with the party that performs it.

The builder rejects only name and type errors (unknown party, bad register,
duplicate names, nested role blocks). Well-formedness and timing rules are the
compiler's job, so ill-formed descriptions can still be built and fed to it.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .image import Profile
from .isa import INF, NUM_REGS

REGS = tuple(f"r{i}" for i in range(NUM_REGS))
SRCS = ("zero", "ones", "fifo") + REGS
DSTS = ("discard", "fifo") + REGS
COND_KINDS = ("fall", "rise")
ALU_OPS = ("add", "sub", "and", "or", "xor", "shl", "shr", "eq", "ne", "lt")
BR_CONDS = ("always", "z", "nz", "to")
MON = "mon"
SHARED = "shared"


@dataclass(frozen=True)
class LineRef:
    name: str
    index: int


@dataclass(frozen=True)
class ProfRef:
    name: str
    index: int  # index into the image's timing table (0 is IDLE)


@dataclass(frozen=True)
class LaneRef:
    name: str
    index: int


@dataclass(frozen=True)
class LineDecl:
    name: str
    kind: str
    owner: str
    idle: int


@dataclass(frozen=True)
class LaneDecl:
    name: str
    data: LineRef
    clk: LineRef | None
    cpol: int
    cpha: int


@dataclass
class Step:
    kind: str
    args: dict[str, Any]
    owner: str | None
    block: frozenset[str] | None
    loc: str
    index: int
    labels: list[str] = field(default_factory=list)


def _caller_loc() -> str:
    here = os.path.abspath(__file__)
    f = sys._getframe(1)
    while f is not None and os.path.abspath(f.f_code.co_filename) == here:
        f = f.f_back
    if f is None:
        return "?"
    return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}"


class Description:
    def __init__(self, name: str, parties: tuple[str, str] = ("p0", "p1"), tolerance_ppm: int = 20000):
        if len(parties) != 2 or len(set(parties)) != 2 or {MON, SHARED} & set(parties):
            raise ValueError(f"need two distinct party names, got {parties!r}")
        self.name = name
        self.parties = tuple(parties)
        self.tolerance_ppm = tolerance_ppm
        self.lines: list[LineDecl] = []
        self.profiles: list[Profile] = []
        self.lanes: list[LaneDecl] = []
        self.steps: list[Step] = []
        self.abort_label: str | None = None
        self._pending_labels: list[str] = []
        self._labels: set[str] = set()
        self._block: frozenset[str] | None = None

    # ---- declarations ---------------------------------------------------------------
    def line(self, name: str, kind: str = "pp", *, owner: str, idle: int = 1) -> LineRef:
        if kind not in ("pp", "od"):
            raise ValueError(f"line kind must be 'pp' or 'od', got {kind!r}")
        if owner not in self.parties + (SHARED,):
            raise ValueError(f"unknown line owner {owner!r}")
        if idle not in (0, 1):
            raise ValueError("idle must be 0 or 1")
        self._unique(name, [ln.name for ln in self.lines], "line")
        self.lines.append(LineDecl(name, kind, owner, idle))
        return LineRef(name, len(self.lines) - 1)

    def profile(self, name: str, nom: int, min: int | None = None, max: int | None = None) -> ProfRef:
        self._unique(name, ["IDLE"] + [p.name for p in self.profiles], "profile")
        for v in (nom, min, max):
            if v is not None and not isinstance(v, int):
                raise ValueError("profile values must be int cycles")
        self.profiles.append(Profile(name, nom if min is None else min, nom, nom if max is None else max))
        return ProfRef(name, len(self.profiles))

    def lane(self, name: str, data: LineRef, clk: LineRef | None = None, cpol: int = 0, cpha: int = 0) -> LaneRef:
        self._unique(name, [la.name for la in self.lanes], "lane")
        if cpol not in (0, 1) or cpha not in (0, 1):
            raise ValueError("cpol/cpha must be 0 or 1")
        self.lanes.append(LaneDecl(name, self._line(data), None if clk is None else self._line(clk), cpol, cpha))
        return LaneRef(name, len(self.lanes) - 1)

    def label(self, name: str) -> None:
        if name in self._labels:
            raise ValueError(f"duplicate label {name!r}")
        self._labels.add(name)
        self._pending_labels.append(name)

    def abort_to(self, label: str) -> None:
        self.abort_label = label

    @contextmanager
    def only(self, *roles: str) -> Iterator[None]:
        if self._block is not None:
            raise ValueError("role blocks cannot nest")
        if not roles:
            raise ValueError("only() needs at least one role")
        for r in roles:
            if r not in self.parties + (MON,):
                raise ValueError(f"unknown role {r!r}")
        self._block = frozenset(roles)
        try:
            yield
        finally:
            self._block = None

    # ---- timed steps ----------------------------------------------------------------
    def cond(self, kind: str, line: LineRef, owner: str, prof: ProfRef | None = None, abort: bool = False) -> None:
        self._choice(kind, COND_KINDS, "cond kind")
        self._add("cond", owner, kind=kind, line=self._line(line), prof=self._prof(prof), abort=abort)

    def hold(self, line: LineRef, level: int, owner: str, prof: ProfRef, abort: bool = False) -> None:
        if level not in (0, 1):
            raise ValueError("level must be 0 or 1")
        self._add("hold", owner, line=self._line(line), level=level, prof=self._prof(prof), abort=abort)

    def xfer(self, n: int, lane: LaneRef | LineRef, owner: str, prof: ProfRef, src: str = "zero",
             dst: str = "discard", msb_first: bool = False, check: bool = False, resync: bool = False,
             abort: bool = False) -> None:
        if not isinstance(lane, (LaneRef, LineRef)):
            raise ValueError("xfer needs a lane or a line")
        if isinstance(lane, LineRef):
            self._line(lane)
        elif not 0 <= lane.index < len(self.lanes) or self.lanes[lane.index].name != lane.name:
            raise ValueError(f"unknown lane {lane!r}")
        self._choice(src, SRCS, "src")
        self._choice(dst, DSTS, "dst")
        self._add("xfer", owner, n=n, lane=lane, prof=self._prof(prof), src=src, dst=dst,
                  msb_first=msb_first, check=check, resync=resync, abort=abort)

    # ---- untimed steps --------------------------------------------------------------
    def li(self, rd: str, imm: int) -> None:
        self._add("li", None, rd=self._reg(rd), imm=imm)

    def alu(self, op: str, rd: str, ra: str, b: int | str) -> None:
        self._choice(op, ALU_OPS, "alu op")
        if isinstance(b, str):
            self._reg(b)
        self._add("alu", None, op=op, rd=self._reg(rd), ra=self._reg(ra), b=b)

    def br(self, target: str, cond: str = "always", reg: str | None = None) -> None:
        self._choice(cond, BR_CONDS, "branch condition")
        if (cond in ("z", "nz")) == (reg is None):
            raise ValueError("br: reg is required for z/nz and not allowed for always/to")
        self._add("br", None, target=target, cond=cond, reg=None if reg is None else self._reg(reg))

    def push(self, ra: str) -> None:
        self._add("push", None, ra=self._reg(ra))

    def pop(self, rd: str) -> None:
        self._add("pop", None, rd=self._reg(rd))

    def mark(self, id: int) -> None:
        self._add("mark", None, id=id)

    def halt(self) -> None:
        self._add("halt", None)

    def nop(self) -> None:
        self._add("nop", None)

    # ---- raw tier -------------------------------------------------------------------
    def raw_set(self, line: LineRef, level: int) -> None:
        self._add("raw_set", None, line=self._line(line), level=level)

    def raw_rel(self, line: LineRef) -> None:
        self._add("raw_rel", None, line=self._line(line))

    def raw_get(self, rd: str, line: LineRef) -> None:
        self._add("raw_get", None, rd=self._reg(rd), line=self._line(line))

    def raw_delay(self, n: int) -> None:
        self._add("raw_delay", None, n=n)

    def raw_jpin(self, line: LineRef, level: int, target: str) -> None:
        self._add("raw_jpin", None, line=self._line(line), level=level, target=target)

    # ---- helpers --------------------------------------------------------------------
    def _add(self, _step_kind: str, owner: str | None, **args: Any) -> None:
        if owner is not None and owner not in self.parties + (SHARED,):
            raise ValueError(f"unknown step owner {owner!r}")
        step = Step(_step_kind, args, owner, self._block, _caller_loc(), len(self.steps), self._pending_labels)
        self._pending_labels = []
        self.steps.append(step)

    def _line(self, ref: LineRef) -> LineRef:
        if not isinstance(ref, LineRef) or ref.index >= len(self.lines) or self.lines[ref.index].name != ref.name:
            raise ValueError(f"unknown line {ref!r}")
        return ref

    def _prof(self, ref: ProfRef | None) -> ProfRef | None:
        if ref is not None and (not isinstance(ref, ProfRef) or not 1 <= ref.index <= len(self.profiles)
                                or self.profiles[ref.index - 1].name != ref.name):
            raise ValueError(f"unknown profile {ref!r}")
        return ref

    @staticmethod
    def _reg(r: str) -> str:
        if r not in REGS:
            raise ValueError(f"unknown register {r!r}")
        return r

    @staticmethod
    def _choice(v: str, options: tuple[str, ...], what: str) -> None:
        if v not in options:
            raise ValueError(f"unknown {what} {v!r}; expected one of {options}")

    @staticmethod
    def _unique(name: str, existing: list[str], what: str) -> None:
        if name in existing:
            raise ValueError(f"duplicate {what} name {name!r}")


__all__ = ["Description", "LineRef", "ProfRef", "LaneRef", "Step", "MON", "SHARED", "INF"]
