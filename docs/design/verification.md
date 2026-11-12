# Verification plan

Status: v1 (2026-11-09), owner: verification role. How we show the chip matches the spec
(`isa.md`, `architecture.md`) and the golden model (`toolchain/emu`). Each layer catches a
different kind of bug; a new layer earns its place by catching what the others miss.

## 1. Layers

| Layer | Catches | Status (11-09) | Where |
|---|---|---|---|
| Model tests, and cross-role tests where the two engines play opposite roles (pytest) | the model misreading the spec | Running in CI on Python 3.11 and 3.12. Encoding and image (#5), fetch and issue timing (isa §4.0), reserved codes, `BR ROLE` and registers across RUN at v1; clocked XFER (one data line or duplex, push-pull or OD with the let-go, with clock stretching) in every role, both phases, with CLK_PHASE, TIMEOUT and UNDERRUN, cross-checked against the compiler's completion offsets and T3 at 2% (`test_engine_clocked.py`); the bus detector, COND START/STOP, bus exits, MEAS and gated lines in `test_engine_bus.py`; lead blocks, with an I2C target's conditional ACK and a controller ending a frame early run on the model, in `test_lead.py`; the library SPI (any mode, duplex, gated MISO, CS as a SELECT bus) and I2C (a controller write with the target's conditional ACK) in every role permutation, at 2% drift and at the shortest period that compiles (the rule cross-check), in `test_spi.py` and `test_i2c.py` | `toolchain/tests/`; cross-role: `test_system.py`, `test_protocols.py` |
| Rule coverage: every isa.md rule, failure kind, COND and XFER case mapped to a test | spec corners nobody tested | Checked by a test (11-11): every rule and event kind isa.md names has a test, and the rules the code raises match the ones isa.md names. No gaps since the FZ ops reached the compiler (11-12) | §2, `toolchain/tests/test_rule_coverage.py` |
| Co-sim: one image on the model and the RTL, every trace-port pulse and the pins every cycle compared (directed tests, then the protocol programs) | the RTL misreading the spec | Harness built (11-10): the model's peers are replayed into engine 0. Directed tests pass for untimed ops, HOLD, COND and unclocked XFER, and UART frames in every role with peers up to 2% slow and glitches. Registers are read back after each run, and `uio_oe`/`uio_out` are compared every cycle; the pin check found an image-format bug (#24). On main's engine (#13, #14) every test passes, including ILLEGAL and the ROLE_IF/RAW JPIN branches. With the FIFOs (#38) the library UART co-sims in every role, from TXDATA to RXDATA: M1's target (11-11). The library SPI and I2C co-sim in every role (11-12): SPI in all four modes, MON's duplex samples included; I2C ACKed and NACKed, with short `ldelay`s. Next: engine 1, and random programs | `test/cosim/` (design in its README) |
| Constrained-random programs from the compiler (legal by its rules), with a shrinker that cuts a failure to its smallest program | cases nobody thought to write | First slice (11-12): random two-party descriptions. They use UART-style frames (idle 1 or 0, with or without an EARLY window) and clocked XFERs on PP and OD lines, owner-only prep blocks (LI, POP, ALU, delays), forward branches on wire values and whole-recipe loops. Each runs as P0, P1 and MON with clocks within the tolerance (corners half the time). Five oracles apply, `value` among them: every XFER carries what the recipe says, so bugs every role shares fail too. A failure is shrunk to its smallest recipe and kept in a regression corpus. The first 200 recipes found a rule gap: a COND whose owner left the line at the post-edge level makes no edge, so the followers hung or misread with no violation (new rule `R2-edge`). Since then the campaigns are clean, about 70% of recipes accepted. Planted bugs caught: followers' values flipped, the ALU writing 0, a POP dropping a word. Not yet generated: aborts, the bus, lead blocks, duplex, SHARED and gated lines, CHK/RESYNC, MEAS, FZ. A late host is (11-12): the host writes one word at a time, so POPs stall and T5-stall is cross-checked; it holds, and a POP that doesn't wait, invisible with an early host, fails. No coverage-guided weights yet. Next: feed them to co-sim, which also catches what only the RTL can: a uniform follower-latency change passes every oracle on the model | `toolchain/emu/gen/`, `toolchain/tests/test_gen.py`, `toolchain/tests/gen_corpus/` |
| Rule cross-check: no program the compiler accepts breaks a timing rule on the model | wrong timing rules (isa §6, T1–T5) | Directed: each re-derived boundary runs on both the compiler and the model, e.g. the shortest accepted UART (P = 19) in all role assignments, at ±2% drift and on chip. Random (11-12): every accepted random program must run without a violation, contention or hang, with every role agreeing on each step's value (`emu/gen/check.py`); `python -m emu.gen run` is the campaign | `toolchain/tests/test_analysis.py`, `toolchain/emu/gen/` |
| Formal proofs (SymbiYosys): FIFOs, host SPI, fetch parity, the bus detector | handshake bugs simulation misses | Not started | `formal/` |
| Gate level: the cocotb tests on the synthesized netlist (CI `gl_test`) | synthesis mismatches, X propagation | Running on every push. Co-sim runs on the netlist too (`make GATES=yes`: pins and registers, aligned by clk count and checked on ACT), locally so far; devOps adds it to CI | `.github/workflows/gds.yaml`, `test/`, `test/cosim/` |
| FPGA (Basys 3): LOOPBACK (the two engines talk on-chip), then real devices (sensor, EEPROM, a microcontroller) | real timing, pads, electrical surprises | Not started; bring-up order in architecture §5 | `fpga/`, `host/` |
| Mutation testing: plant a bug, check that some test fails | checks too loose to fail | By hand so far: planted bugs in the image encoder (#5), the issue timing (step 3a) and the reserved-code rule (off-by-one indices, a dropped dst code, an inverted ROLE_IF record; 11-10), each caught by a test | — |

## 2. Rule coverage

One row per rule isa.md names (§5, §6) and per event kind (§7), with the test files that name it.
`toolchain/tests/test_rule_coverage.py` builds this table from isa.md and the tests, and fails when:
- a rule or kind has no test;
- the compiler or the analysis raises a rule that isa.md doesn't name, or isa.md names one that
  nothing raises;
- a listed gap has closed;
- this table is stale (`cd toolchain && PYTHONPATH=. python tests/test_rule_coverage.py` prints it).

A name in a test shows that a test expects the rule, not that the test is strong: mutation testing
(§1) checks that. COND kinds and XFER cases are covered in the model's own test files (§1).

| Rule or kind | Tests |
|---|---|
| `R1-bus` | `test_engine_bus.py` |
| `R1-od-lane` | `test_compiler.py`, `test_engine_clocked.py` |
| `R1-owner` | `test_compiler.py`, `test_engine_clocked.py` |
| `R1-shared` | `test_compiler.py` |
| `R2-branch` | `test_analysis.py`, `test_engine_bus.py`, `test_engine_clocked.py`, `test_fz.py` |
| `R2-edge` | `test_edge_rule.py`, `test_gen.py` |
| `R3-escape` | `test_compiler.py`, `test_lead.py` |
| `R3-fz` | `test_fz.py` |
| `R3-lead` | `test_lead.py` |
| `R3-raw-drive` | `test_compiler.py` |
| `R3-timed-in-role` | `test_compiler.py`, `test_lead.py` |
| `S-bus` | `test_engine_bus.py`, `test_engine_clocked.py` |
| `S-flags` | `test_compiler.py`, `test_engine_bus.py` |
| `S-label` | `test_compiler.py` |
| `S-lane` | `test_engine_clocked.py` |
| `S-line` | `test_engine_bus.py` |
| `S-range` | `test_compiler.py`, `test_engine_clocked.py` |
| `S-table` | `test_compiler.py` |
| `T1-sample` | `test_analysis.py` |
| `T1-unanchored` | `test_analysis.py` |
| `T2-window` | `test_analysis.py`, `test_engine_timed.py` |
| `T3-latency` | `test_analysis.py` |
| `T4-profile` | `test_analysis.py` |
| `T5-abort` | `test_analysis.py`, `test_engine_bus.py`, `test_fz.py`, `test_lead.py` |
| `T5-overrun` | `test_analysis.py` |
| `T5-stall` | `test_analysis.py`, `test_engine_clocked.py`, `test_lead.py` |
| `T5-startup` | `test_startup.py` |
| `T5-timeout` | `test_analysis.py` |
| `T5-watch` | `test_analysis.py`, `test_engine_bus.py`, `test_fz.py` |
| `OVERRUN` | `test_analysis.py`, `test_engine_timed.py` |
| `EARLY` | `test_engine_bus.py`, `test_engine_timed.py`, `test_fuzz_unit.py`, `test_startup.py` |
| `TIMEOUT` | `test_engine_bus.py`, `test_engine_clocked.py`, `test_engine_timed.py`, `test_fuzz_devices.py`, `test_fuzz_unit.py`, `test_fz.py` |
| `LEVEL` | `test_engine_timed.py`, `test_protocols.py` |
| `EDGE` | `test_engine_timed.py` |
| `CLK_PHASE` | `test_engine_clocked.py` |
| `ABORT` | `test_engine_bus.py`, `test_engine_clocked.py`, `test_engine_timed.py`, `test_fuzz_devices.py`, `test_fz.py`, `test_protocols.py` |
| `OVERFLOW` | `test_cosim.py`, `test_engine_core.py`, `test_system.py` |
| `UNDERRUN` | `test_engine_clocked.py`, `test_spi.py` |
| `BUS` | `test_engine_bus.py`, `test_fz.py`, `test_i2c.py`, `test_lead.py`, `test_spi.py` |
| `ILLEGAL` | `test_engine_core.py`, `test_fz.py` |

## 3. Rules

- Golden values come from the docs and are built apart from the code under test (explicit
  shifts, hand-written bytes), so the code and its test can't share a misreading.
- Run mutants with `python -B`. A same-length edit made within one second can leave a stale
  `.pyc`, so a restored file still runs the mutant (seen 11-09).
- When a bug gets through, name the layer that should have caught it and add that check here.
  - 11-09: the compiler accepted a program that overran on the model (an owner's HOLD completes
    up to ISSUE late). The rule cross-check should catch this, so every timing-rule change now gets
    a test that runs both sides of its boundary on the compiler and the model.
