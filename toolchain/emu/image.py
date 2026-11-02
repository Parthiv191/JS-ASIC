"""Program image: instruction words plus the header tables an engine is loaded with."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Literal

from .isa import INF, Instr, Owner, decode


@dataclass(frozen=True)
class Profile:
    name: str
    min: int
    nom: int
    max: int

    @property
    def bounded(self) -> bool:
        return self.max != INF


IDLE = Profile("IDLE", 0, 0, INF)


@dataclass(frozen=True)
class LineCfg:
    name: str
    kind: Literal["pp", "od"]
    owner: Owner
    idle: int = 1


@dataclass(frozen=True)
class LaneCfg:
    data: int
    clk: int | None = None
    cpol: int = 0
    cpha: int = 0


@dataclass
class Image:
    name: str
    parties: tuple[str, str]
    words: list[int]
    profiles: list[Profile]
    lines: list[LineCfg]
    lanes: list[LaneCfg]
    abort_pc: int = 0
    tolerance_ppm: int = 20000
    debug: dict[int, str] = field(default_factory=dict)

    def instrs(self) -> list[Instr]:
        return [decode(w) for w in self.words]

    def profile_index(self, name: str) -> int:
        for i, p in enumerate(self.profiles):
            if p.name == name:
                return i
        raise KeyError(name)

    def with_profiles(self, scale: dict[str, float]) -> "Image":
        """Copy with the named profiles' nominal times scaled; windows unchanged.

        Models a device that runs the same description with sloppy timing.
        """
        profs = [
            replace(p, nom=max(1, round(p.nom * scale[p.name]))) if p.name in scale else p
            for p in self.profiles
        ]
        return replace(self, profiles=profs, words=list(self.words), debug=dict(self.debug))

    def to_json(self) -> str:
        return json.dumps({
            "name": self.name,
            "parties": list(self.parties),
            "words": [f"{w:08x}" for w in self.words],
            "profiles": [[p.name, p.min, p.nom, p.max] for p in self.profiles],
            "lines": [[ln.name, ln.kind, int(ln.owner), ln.idle] for ln in self.lines],
            "lanes": [[la.data, la.clk, la.cpol, la.cpha] for la in self.lanes],
            "abort_pc": self.abort_pc,
            "tolerance_ppm": self.tolerance_ppm,
            "debug": {str(k): v for k, v in self.debug.items()},
        }, indent=1)

    @classmethod
    def from_json(cls, s: str) -> "Image":
        j = json.loads(s)
        return cls(
            name=j["name"],
            parties=tuple(j["parties"]),
            words=[int(w, 16) for w in j["words"]],
            profiles=[Profile(*p) for p in j["profiles"]],
            lines=[LineCfg(n, k, Owner(o), i) for n, k, o, i in j["lines"]],
            lanes=[LaneCfg(*la) for la in j["lanes"]],
            abort_pc=j["abort_pc"],
            tolerance_ppm=j["tolerance_ppm"],
            debug={int(k): v for k, v in j["debug"].items()},
        )
