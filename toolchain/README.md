# emu: role-switching emulator toolchain

Python toolchain for the role-switching protocol emulator. It is the single
reference for the ISA (`emu/isa.py`; the contract is `../docs/design/isa.md`),
the description language and compiler, and the cycle-level reference model. A
coverage-guided fuzzer will live here too.

```sh
python3 -m venv ../.venv && ../.venv/bin/pip install -e ".[dev]"
../.venv/bin/python -m pytest          # unit, cross-role and regression tests
```

CI runs the same tests on Python 3.11 and 3.12 (`.github/workflows/toolchain.yaml`).
A `CompileWarning` fails the run, locally too (`pyproject.toml`).

Status (2026-11-09): the encoding (`emu/isa.py`) and the image with its host
table bytes (`emu/image.py`) are at ISA v1. The DSL, the compiler with its R/S/T
rules, and the engine and system model still execute ISA v0 (1-cycle untimed
instructions, no clocked transfers). The protocol library (`emu/protocols`) runs
a UART in every role assignment. Moving execution to v1 is the next step.
