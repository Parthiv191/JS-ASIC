"""The learning experiment (bus-fuzzer.md §6 stage 1): learning against the no-feedback baseline.

One iteration is the loop of §4: `fz.next`, the transaction on the model (a fresh System, so the
device starts from reset), the auto-tokens and event mask into the fingerprint, then `fz.end`,
or `abort` if engine 0 aborted (a hang).

Measures, per run:
- `found`: the iteration (1-based) at which the device's planted state was first reached, or None;
- `coverage`: distinct fingerprints (bitmap bits set) after the budget, and at checkpoints.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from .devices import DEVICES, Target, TableDevice
from .unit import FuzzConfig, FuzzUnit

SEEDS = (0xACE1, 0x5EED, 0x1D2C, 0xBEEF, 0x7A3B)  # spread out: seeds 1 and 2 are one draw apart
ITERS = 30_000  # ~1300 cycles each on the chip: 30K iterations is ~1 s at 40 MHz


@dataclass
class Report:
    device: str
    seed: int
    feedback: bool
    iters: int
    found: int | None
    coverage: int
    curve: list[tuple[int, int]] = field(default_factory=list)  # (iteration, coverage)
    corpus: int = 0
    hangs: int = 0
    first_hang: int | None = None


def run(dev: TableDevice | str, iters: int, seed: int, feedback: bool = True,
        target: Target | None = None, every: int = 0) -> Report:
    """One campaign. The corpus starts as one all-zero seed in slot 0 (energy 15)."""
    dev = DEVICES[dev]() if isinstance(dev, str) else dev
    target = target or Target(dev)
    unit = FuzzUnit(FuzzConfig(seed=seed, length=dev.length, feedback=feedback))
    unit.load(0, bytes(dev.length))
    found = first_hang = None
    curve = []
    for it in range(1, iters + 1):
        n = unit.next()
        msg = bytes(unit.read_byte() for _ in range(n))
        obs = target.observe(msg)
        for t in obs.tokens:
            unit.tok(t)
        for k in obs.events:
            unit.event(k)
        if obs.hang:
            unit.abort()
            if first_hang is None:
                first_hang = it
        else:
            unit.end()
        if found is None and dev.reached(msg):
            found = it
        if every and it % every == 0:
            curve.append((it, unit.coverage))
    return Report(dev.name, seed, feedback, iters, found, unit.coverage, curve,
                  corpus=len(unit.corpus()), hangs=unit.stats.hangs, first_hang=first_hang)


def experiment(devices=tuple(DEVICES), seeds=SEEDS, iters: int = ITERS, every: int = 0) -> list[Report]:
    out = []
    for name in devices:
        target = Target(DEVICES[name]())
        for fb in (True, False):
            for s in seeds:
                out.append(run(target.dev, iters, s, fb, target, every))
    return out


def table(reports: list[Report]) -> str:
    """A Markdown table: per device and seed, learning against baseline."""
    def f(r: Report) -> str:
        return "–" if r.found is None else str(r.found)

    by = {(r.device, r.seed, r.feedback): r for r in reports}
    devices = list(dict.fromkeys(r.device for r in reports))
    seeds = sorted({r.seed for r in reports})
    iters = reports[0].iters if reports else 0
    lines = [f"Budget {iters} iterations per run. Cells: iterations to the planted behaviour "
             "(– = not found) / distinct fingerprints at the end.", "",
             "| Device | " + " | ".join(f"seed {s:#06x}" for s in seeds) + " | found | median coverage |",
             "|---|" + "---|" * (len(seeds) + 2)]
    for d in devices:
        for fb in (True, False):
            rs = [by[(d, s, fb)] for s in seeds]
            cells = [f"{f(r)} / {r.coverage}" for r in rs]
            hit = sum(r.found is not None for r in rs)
            med = statistics.median(r.coverage for r in rs)
            lines.append(f"| {d} ({'learning' if fb else 'baseline'}) | " + " | ".join(cells)
                         + f" | {hit}/{len(rs)} | {med:g} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time
    ap = argparse.ArgumentParser(prog="python -m emu.fuzz", description=__doc__.splitlines()[0])
    ap.add_argument("--devices", default=",".join(DEVICES), help="comma-separated: " + ", ".join(DEVICES))
    ap.add_argument("--seeds", default=",".join(hex(s) for s in SEEDS), help="LFSR seeds, e.g. 0xace1,7,9")
    ap.add_argument("--iters", type=int, default=ITERS)
    a = ap.parse_args(argv)
    seeds = tuple(int(s, 0) for s in a.seeds.split(","))
    t = time.time()
    reports = experiment(tuple(a.devices.split(",")), seeds, a.iters)
    print(table(reports))
    print(f"\n({time.time() - t:.0f} s)")
    return 0


__all__ = ["Report", "run", "experiment", "table", "main", "SEEDS", "ITERS"]
