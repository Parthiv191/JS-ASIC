"""Wire resolution for push-pull and open-drain (wired-AND) lines."""
from __future__ import annotations

from ..image import LineCfg

PULL = 1  # undriven lines read high


def resolve(lines: list[LineCfg], drives: list[list[int | None]]) -> tuple[list[int], list[int]]:
    """Resolve one tick. `drives[e][l]` is engine e's drive on line l (None = released).

    Returns (wire values, indices of push-pull lines with more than one driver).
    """
    values: list[int] = []
    contended: list[int] = []
    for i, ln in enumerate(lines):
        col = [d[i] for d in drives if d[i] is not None]
        if ln.kind == "od":
            values.append(0 if 0 in col else PULL)
        elif not col:
            values.append(PULL)
        else:
            if len(col) > 1:
                contended.append(i)
            values.append(min(col))
    return values, contended
