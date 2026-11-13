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

Two longer checks run by hand (`docs/design/verification.md` §1):

```sh
../.venv/bin/python -m emu.gen run --iters 500 --seed 1          # random programs, the rule cross-check
../.venv/bin/python -m emu.mutate emu/model/engine.py --workers 4 # mutation testing: which planted bugs no test catches
```

Status (2026-11-09): the encoding (`emu/isa.py`), the image with its host table
bytes (`emu/image.py`), and the model's fetch and issue timing (isa §4.0, with
the matching T rules in `emu/analysis.py`) are at ISA v1. Clocked transfers,
START/STOP, MEAS and the FIFO depths come next. The protocol library
(`emu/protocols`) runs a UART in every role assignment; at 2% tolerance its bit
must be at least 19 cycles.
