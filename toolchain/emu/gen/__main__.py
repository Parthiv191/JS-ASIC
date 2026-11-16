"""`python -m emu.gen run --iters N --seed S [--out DIR]`: a campaign of random programs. Each
failure is shrunk and saved as `DIR/<oracles>-<seed>.json`; exit 1 if any.
`python -m emu.gen replay FILE...`: run saved cases again; exit 1 if any fails."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .check import Case, campaign, run_case, shrink


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emu.gen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--iters", type=int, default=500)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--envs", type=int, default=2, help="environments per accepted program")
    r.add_argument("--out", type=Path, default=Path("gen-out"))
    p = sub.add_parser("replay")
    p.add_argument("files", nargs="+", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "replay":
        bad = 0
        for f in a.files:
            res = run_case(Case.from_json(f.read_text(encoding="utf-8")))
            print(f"{f.name}: {res.verdict} {' '.join(res.rules)}")
            bad += res.verdict in ("fail", "structural")
        return 1 if bad else 0
    rep = campaign(a.seed, a.iters, a.envs)
    print(f"{a.iters} recipes from seed {a.seed}: {rep.accepted} accepted, {rep.runs} runs, "
          f"{len(rep.failures)} failures, {len(rep.structural)} generator bugs")
    print("rejected by: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.rejected.items(), key=lambda x: -x[1])))
    if rep.failures or rep.structural:
        a.out.mkdir(parents=True, exist_ok=True)
    for case, res in rep.failures:
        small = shrink(case)
        f = a.out / f"{'-'.join(res.rules)}-{case.recipe.seed}.json"
        f.write_text(small.to_json(), encoding="utf-8")
        print(f"  {f}: {run_case(small).detail[:200]}")
    for case in rep.structural:
        f = a.out / f"structural-{case.recipe.seed}.json"
        f.write_text(case.to_json(), encoding="utf-8")
        print(f"  {f}: {run_case(case).detail[:200]}")
    return 1 if rep.failures or rep.structural else 0


if __name__ == "__main__":
    sys.exit(main())
