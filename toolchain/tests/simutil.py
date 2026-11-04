"""Test helpers for running small multi-engine systems."""
from emu.isa import Role
from emu.model.system import EngineSpec, System


def run_sys(image, roles=(Role.P0, Role.P1, Role.MON), tx=None, images=None, clocks=None,
            phases=None, glitches=(), max_ticks=50_000, push_at=0):
    specs = []
    for i, r in enumerate(roles):
        specs.append(EngineSpec((images or {}).get(i, image), r,
                                clock=(clocks or {}).get(i, (1, 1)), phase=(phases or {}).get(i, 0)))
    s = System(specs, glitches=glitches)
    for i, words in (tx or {}).items():
        s.host_push(i, words, at_tick=push_at)
    return s.run(max_ticks)


def kinds(trace, i):
    return [e.kind for e in trace.engines[i].events]
