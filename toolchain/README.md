# emu: role-switching emulator toolchain

Python toolchain for the role-switching protocol emulator. It is the single
source of truth for the ISA (`emu/isa.py`, contract in `../docs/design/isa-v0.md`),
the description language and compiler, the cycle-level reference model and the
coverage-guided fuzzer.

```sh
python3 -m venv ../.venv && ../.venv/bin/pip install -e ".[dev]"
../.venv/bin/python -m pytest          # unit, cross-role and regression tests
../.venv/bin/python -m emu.fuzz run --iters 2000 --seed 1 --out /tmp/fuzz
```
