"""Protocol library: each protocol is one Description that runs in every role (plan Task 10).

`slack` is the window convention: an edge `nom` cycles after the last anchor may move by
2·d·nom between two clocks each off by d, plus one cycle of quantization.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from ..compiler import compile_desc
from ..desc import Description
from ..isa import Role
from ..model.system import EngineSpec, Glitch, System, Trace


def slack(nom: int, tolerance_ppm: int) -> int:
    return math.ceil(2 * tolerance_ppm * nom / 1e6) + 1


@dataclass(frozen=True)
class ProtocolSpec:
    name: str
    build: Callable[..., Description]
    defaults: dict
    space: dict[str, list]                          # every point compiles (tested)
    payload: Callable[..., dict[str, list[int]]]    # (rng, n, **build params) -> words per party


def run_roles(desc: Description, perm, payload: dict[str, list[int]], clocks=None, phases=None,
              glitches: list[Glitch] | tuple = (), max_ticks: int = 500_000) -> Trace:
    """Run one compiled image on len(perm) engines; engine i takes role perm[i].

    A P0/P1 engine gets the host words of the party it plays; a monitor gets none.
    """
    image = compile_desc(desc)
    specs = [EngineSpec(image, r, clock=(clocks or {}).get(i, (1, 1)), phase=(phases or {}).get(i, 0))
             for i, r in enumerate(perm)]
    s = System(specs, glitches=glitches)
    for i, r in enumerate(perm):
        if r != Role.MON:
            s.host_push(i, payload.get(desc.parties[int(r)], []))
    return s.run(max_ticks)


from . import uart  # noqa: E402  (registers after the helpers it uses)

PROTOCOLS: dict[str, ProtocolSpec] = {p.name: p for p in (uart.SPEC,)}


def get(name: str) -> ProtocolSpec:
    try:
        return PROTOCOLS[name]
    except KeyError:
        raise KeyError(f"unknown protocol {name!r}; known: {', '.join(sorted(PROTOCOLS))}") from None
