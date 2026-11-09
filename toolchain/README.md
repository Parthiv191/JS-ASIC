# emu: role-switching emulator toolchain

Python toolchain for the role-switching protocol emulator. It is the single
reference for the ISA (`emu/isa.py`; the contract is `../docs/design/isa.md`),
the description language and compiler, the cycle-level reference model and the
coverage-guided fuzzer.

```sh
python3 -m venv ../.venv && ../.venv/bin/pip install -e ".[dev]"
../.venv/bin/python -m pytest          # unit, cross-role and regression tests
```

CI runs the same tests on Python 3.11 and 3.12 (`.github/workflows/toolchain.yaml`).
A `CompileWarning` fails the run, locally too (`pyproject.toml`).

Status (2026-11-09): the encoding, the DSL, the compiler with its R/S/T rules,
and the engine and system model with the timed ops are at ISA v0. The protocol
library (`emu/protocols`) runs a UART in every role assignment. Moving all of it
to ISA v1 (`../docs/design/isa.md`) is the next step.
