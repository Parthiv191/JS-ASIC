# emu: role-switching emulator toolchain

Python toolchain for the role-switching protocol emulator. It is the single
source of truth for the ISA (`emu/isa.py`, contract in `../docs/design/isa-v0.md`),
the description language and compiler, the cycle-level reference model and the
coverage-guided fuzzer.

```sh
python3 -m venv ../.venv && ../.venv/bin/pip install -e ".[dev]"
../.venv/bin/python -m pytest          # unit, cross-role and regression tests
```

CI runs the same tests on Python 3.11 and 3.12 (`.github/workflows/toolchain.yaml`).
A `CompileWarning` fails the run, locally too (`pyproject.toml`).

Status (2026-11-04): the ISA encoding, the DSL, the compiler with its
R/S/T rules, and the engine and system model with the timed ops (`COND`,
`HOLD`, unclocked `XFER`, `BR TO`) are done (plan Tasks 1–7 and the r2
amendments). The protocol library (`emu/protocols`) has the UART, run in every
role assignment (the UART half of Task 10). Clocked and duplex lanes arrive in
Task 8, CRC and IRQ in Task 9; SPI follows them.
The fuzz CLI (`python -m emu.fuzz run --iters N --seed S --out DIR`) arrives
in Task 15. The plan is `../docs/superpowers/plans/2026-11-02-role-switching-emulator.md`.
