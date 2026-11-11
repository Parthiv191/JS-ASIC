# Verification plan

Status: v1 (2026-11-09), owner: verification role. How we show the chip matches the spec
(`isa.md`, `architecture.md`) and the golden model (`toolchain/emu`). Each layer catches a
different kind of bug; a new layer earns its place by catching what the others miss.

## 1. Layers

| Layer | Catches | Status (11-09) | Where |
|---|---|---|---|
| Model tests, and cross-role tests where the two engines play opposite roles (pytest) | the model misreading the spec | Running in CI on Python 3.11 and 3.12. Encoding and image (#5), fetch and issue timing (isa §4.0), reserved codes, `BR ROLE` and registers across RUN at v1; clocked XFER (one data line or duplex, push-pull or OD with the let-go, with clock stretching) in every role, both phases, with CLK_PHASE, TIMEOUT and UNDERRUN, cross-checked against the compiler's completion offsets and T3 at 2% (`test_engine_clocked.py`); the bus detector, COND START/STOP, bus exits, MEAS and gated lines in `test_engine_bus.py`; lead blocks and the library I2C and SPI next | `toolchain/tests/`; cross-role: `test_system.py`, `test_protocols.py` |
| Rule coverage: every isa.md rule, failure kind, COND and XFER case mapped to a test | spec corners nobody tested | Not started; built as the model moves to v1 | §2 |
| Co-sim: one image on the model and the RTL, every trace-port pulse compared (directed tests, then the protocol programs) | the RTL misreading the spec | Harness built (11-10): the model's peers are replayed into engine 0. Directed tests pass for untimed ops, HOLD, COND and unclocked XFER, and UART frames in every role with peers up to 2% slow and glitches; registers are read back after each run. The RTL's own pin drives aren't compared yet, so owner roles are checked through their trace and registers only. On main's engine (#13, #14) all 36 pass (the slow clamp test runs with COSIM_SLOW=1), including ILLEGAL and ROLE_IF/RAW JPIN branches. Next: pins and gate level, then protocol UART once FIFOs exist | `test/cosim/` (design in its README) |
| Constrained-random programs from the compiler (legal by its rules), with a shrinker that cuts a failure to its smallest program | cases nobody thought to write | Not started; designs in the old plan, Tasks 11–18 | `toolchain/` |
| Rule cross-check: no program the compiler accepts breaks a timing rule on the model | wrong timing rules (isa §6, T1–T5) | Directed: each re-derived boundary runs on both the compiler and the model, e.g. the shortest accepted UART (P = 19) in all role assignments, at ±2% drift and on chip. The random version comes with the generator | `toolchain/tests/test_analysis.py` |
| Formal proofs (SymbiYosys): FIFOs, host SPI, fetch parity, the bus detector | handshake bugs simulation misses | Not started | `formal/` |
| Gate level: the cocotb tests on the synthesized netlist (CI `gl_test`) | synthesis mismatches, X propagation | Running on every push | `.github/workflows/gds.yaml`, `test/` |
| FPGA (Basys 3): LOOPBACK (the two engines talk on-chip), then real devices (sensor, EEPROM, a microcontroller) | real timing, pads, electrical surprises | Not started; bring-up order in architecture §5 | `fpga/`, `host/` |
| Mutation testing: plant a bug, check that some test fails | checks too loose to fail | By hand so far: planted bugs in the image encoder (#5), the issue timing (step 3a) and the reserved-code rule (off-by-one indices, a dropped dst code, an inverted ROLE_IF record; 11-10), each caught by a test | — |

## 2. Rule coverage

Starts with the model's move to v1: one row per isa.md rule, failure kind (§4.1), COND kind and
XFER case, each naming the test that exercises it. A row with no test is a gap.

## 3. Rules

- Golden values come from the docs and are built apart from the code under test (explicit
  shifts, hand-written bytes), so the code and its test can't share a misreading.
- Run mutants with `python -B`. A same-length edit made within one second can leave a stale
  `.pyc`, so a restored file still runs the mutant (seen 11-09).
- When a bug gets through, name the layer that should have caught it and add that check here.
  - 11-09: the compiler accepted a program that overran on the model (an owner's HOLD completes
    up to ISSUE late). The rule cross-check should catch this, so every timing-rule change now gets
    a test that runs both sides of its boundary on the compiler and the model.
