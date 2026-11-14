# Verification plan

Status: v1 (2026-11-09), owner: verification role. How we show the chip matches the spec
(`isa.md`, `architecture.md`) and the golden model (`toolchain/emu`). Each layer catches a
different kind of bug; a new layer earns its place by catching what the others miss.

## 1. Layers

| Layer | Catches | Status (11-09) | Where |
|---|---|---|---|
| Model tests, and cross-role tests where the two engines play opposite roles (pytest) | the model misreading the spec | Running in CI on Python 3.11 and 3.12. Encoding and image (#5), fetch and issue timing (isa §4.0), reserved codes, `BR ROLE` and registers across RUN at v1; clocked XFER (one data line or duplex, push-pull or OD with the let-go, with clock stretching) in every role, both phases, with CLK_PHASE, TIMEOUT and UNDERRUN, cross-checked against the compiler's completion offsets and T3 at 2% (`test_engine_clocked.py`); the bus detector, COND START/STOP, bus exits, MEAS and gated lines in `test_engine_bus.py`; lead blocks, with an I2C target's conditional ACK and a controller ending a frame early run on the model, in `test_lead.py`; the library SPI (any mode, duplex, gated MISO, CS as a SELECT bus) and I2C (a controller write with the target's conditional ACK) in every role permutation, at 2% drift and at the shortest period that compiles (the rule cross-check), in `test_spi.py` and `test_i2c.py` | `toolchain/tests/`; cross-role: `test_system.py`, `test_protocols.py` |
| Rule coverage: every isa.md rule, failure kind, COND and XFER case mapped to a test | spec corners nobody tested | Checked by a test (11-11): every rule and event kind isa.md names has a test, and the rules the code raises match the ones isa.md names. No gaps since the FZ ops reached the compiler (11-12) | §2, `toolchain/tests/test_rule_coverage.py` |
| Co-sim: one image on the model and the RTL, every trace-port pulse and the pins every cycle compared (directed tests, then the protocol programs) | the RTL misreading the spec | Harness built (11-10): the model's peers are replayed into engine 0. Directed tests pass for untimed ops, HOLD, COND and unclocked XFER, and UART frames in every role with peers up to 2% slow and glitches. Registers are read back after each run, and `uio_oe`/`uio_out` are compared every cycle; the pin check found an image-format bug (#24). On main's engine (#13, #14) every test passes, including ILLEGAL and the ROLE_IF/RAW JPIN branches. With the FIFOs (#38) the library UART co-sims in every role, from TXDATA to RXDATA: M1's target (11-11). The library SPI and I2C co-sim in every role (11-12): SPI in all four modes, MON's duplex samples included; I2C ACKed and NACKed, with short `ldelay`s. Random programs from `emu/gen` too: 60 clean, and a model latency bug that the model-only oracles miss fails 10 of 12. Next: engine 1 | `test/cosim/` (design in its README) |
| Constrained-random programs from the compiler (legal by its rules), with a shrinker that cuts a failure to its smallest program | cases nobody thought to write | First slice (11-12): random two-party descriptions. They use UART-style frames (idle 1 or 0, with or without an EARLY window) and clocked XFERs on PP and OD lines, owner-only prep blocks (LI, POP, ALU, delays), forward branches on wire values and whole-recipe loops. Each runs as P0, P1 and MON with clocks within the tolerance (corners half the time). Five oracles apply, `value` among them: every XFER carries what the recipe says, so bugs every role shares fail too. A failure is shrunk to its smallest recipe and kept in a regression corpus. The first 200 recipes found a rule gap: a COND whose owner left the line at the post-edge level makes no edge, so the followers hung or misread with no violation (new rule `R2-edge`). Since then the campaigns are clean, about 70% of recipes accepted. Planted bugs caught: followers' values flipped, the ALU writing 0, a POP dropping a word. Not yet generated: aborts, the bus, lead blocks, duplex, SHARED and gated lines, CHK/RESYNC, MEAS, FZ. A late host is (11-12): the host writes one word at a time, so POPs stall and T5-stall is cross-checked; it holds, and a POP that doesn't wait, invisible with an early host, fails. No coverage-guided weights yet. They also run in co-sim against the RTL (`test_random_program`), which catches what the model alone can't: a uniform follower-latency change passes every oracle on the model and fails 10 of 12 there | `toolchain/emu/gen/`, `toolchain/tests/test_gen.py`, `toolchain/tests/gen_corpus/` |
| Rule cross-check: no program the compiler accepts breaks a timing rule on the model | wrong timing rules (isa §6, T1–T5) | Directed: each re-derived boundary runs on both the compiler and the model, e.g. the shortest accepted UART (P = 19) in all role assignments, at ±2% drift and on chip. Random (11-12): every accepted random program must run without a violation, contention or hang, with every role agreeing on each step's value (`emu/gen/check.py`); `python -m emu.gen run` is the campaign | `toolchain/tests/test_analysis.py`, `toolchain/emu/gen/` |
| Formal proofs (SymbiYosys): FIFOs, host SPI, fetch parity, the bus detector | handshake bugs simulation misses | Not started | `formal/` |
| Gate level: the cocotb tests on the synthesized netlist (CI `gl_test`) | synthesis mismatches, X propagation | Running on every push. Co-sim runs on the netlist too (`make GATES=yes`: pins and registers, aligned by clk count and checked on ACT), locally so far; devOps adds it to CI | `.github/workflows/gds.yaml`, `test/`, `test/cosim/` |
| FPGA (Basys 3): LOOPBACK (the two engines talk on-chip), then real devices (sensor, EEPROM, a microcontroller) | real timing, pads, electrical surprises | Not started; bring-up order in architecture §5 | `fpga/`, `host/` |
| Mutation testing: plant a bug, check that some test fails | checks too loose to fail | `emu/mutate.py` (11-12) plants one small bug at a time (a flipped comparison, a constant off by one, a swapped operator, a deleted statement) and runs the tests: a mutant no test kills is a test gap, or an equivalent mutant that changes nothing a program can see. The first full run on the model: `engine.py` 1479 mutants, 1332 killed, 142 survived, 5 timed out; `system.py` 133, 3 survived, 17 timed out; `bus.py` 19, none survived. Of the engine's 142, tests written for them (`test_mutation_gaps.py`) now kill 99, 42 are reviewed equivalents, and the last one was a real gap (below); of `system.py`'s 3, one is killed and 2 are equivalents. The tests pin: the event bits (the bus flags too) and RAW EVT's mask; a confirm-0 bus confirming in the candidate's own cycle; what an abort or a bus exit keeps, releases and restarts; the EARLY and TIMEOUT boundaries and values (COND, followed START and STOP, clocked, the owner's stretched XFER and COND RISE); the CHK window (from RUN, at boundary k, what accumulates, what re-anchors); RESYNC's end; owner lateness (ISSUE only at offset 0, one bit per cycle); completion against the compiler; `to` and the fall-through; the 24-bit clamp on the bus anchors; the let-go after a timeout or a RAW REL; the ALU's rb and shift mask; RAW SET and JPIN levels; the FZ auto-tokens. The reviewed equivalents (55 so far: 44 in the model, 11 in the analysis), each with its reason, are in `mutation_equivalents.json`, which a test keeps in step with the code. The last engine survivor was a real gap: a program could run past its last word (the compiler accepted one with no final HALT; the model halts there, the chip would run on through whatever the SRAM holds). The compiler now rejects it (`S-end`, #65), and a model test kills the mutant. The first `analysis.py` run (11-14) killed all 1090, falsely: the runner wrote each mutant back with `ast.unparse`, which requotes strings and drops comments, so `test_rule_coverage.py`, which greps the analysis for the rule ids it raises, failed on every one. A mutant now changes only its own node's text, and the checks that read the source skip in a mutation run (`EMU_MUTANT`), since a mutant they fail would hide a missing behaviour test. Rerun with the fixed runner, `analysis.py` (the compiler's checks) has 1090 mutants: 906 killed, 182 survived, 2 timed out (83.3%). The first pass, `test_mutation_analysis.py`, pins `check_timing` and the anchor states: each T rule's boundary (T1-unanchored's caps, T2's accumulated and clock-phase windows, T3, T4, T5-startup, T5-watch's confirm, T5-timeout) and the start times and drift the pair checks use. Over `check_timing`, the anchor walk and `_check_stalls`, 274 of 277 mutants are now killed and 3 are reviewed equivalents. It found a real gap: code only an abort reaches (after a HALT) got no T1 or T2 check, because the anchor walk started only at pc 0. It now starts at `abort_pc` too when a step has ABORT and pc 0 never reaches it. Rerun after that pass: 1091 mutants, 970 killed, 120 survived (3 of them equivalents), 1 timed out (89.0%). The second pass pins the pair checks and the paths between timed steps: T5-overrun at nom 0 and at nom, a COND's T2 window, a clock follower's first-edge timeout from its own start (also when the clock owner starts early and re-bases), each clock phase against max, T5-watch for a clock follower and for a HOLD's sample, `drift`'s rounding, a JPIN to an odd pc, a role block that halts, the 4096-path cap, an untimed loop, and T5-stall around role and lead blocks. Of its 50 mutants, 41 are now caught (40 killed, 1 timed out on the untimed loop) and 8 are reviewed equivalents. The review showed the last one, first logged as equivalent, to be a real gap: for a step in a lead block on a lane the other party clocks, the checker timed the clock owner's first edge from 0, not from its start of the block's successor, where it really clocks, so it accepted a bounded follower that times out on the model. Next: that gap (the clock owner's start in a lead block), the level checks, `_steps` and the taint walk, then `compiler.py`, then the timed-out mutants (22 in the model, 1 in the analysis; 4 workers at most: 11 ran this machine out of memory). Before the runner, by hand: planted bugs in the image encoder (#5), the issue timing (step 3a) and the reserved-code rule (11-10), each caught by a test | `toolchain/emu/mutate.py`, `toolchain/tests/test_mutation_gaps.py`, `toolchain/tests/test_mutation_analysis.py` |

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
| `R2-branch` | `test_analysis.py`, `test_engine_bus.py`, `test_engine_clocked.py`, `test_fz.py`, `test_mutation_analysis.py` |
| `R2-edge` | `test_edge_rule.py`, `test_gen.py` |
| `R3-escape` | `test_compiler.py`, `test_lead.py` |
| `R3-fz` | `test_fz.py` |
| `R3-lead` | `test_lead.py` |
| `R3-raw-drive` | `test_compiler.py` |
| `R3-timed-in-role` | `test_compiler.py`, `test_lead.py` |
| `S-bus` | `test_engine_bus.py`, `test_engine_clocked.py` |
| `S-end` | `test_analysis.py` |
| `S-flags` | `test_compiler.py`, `test_engine_bus.py` |
| `S-label` | `test_compiler.py` |
| `S-lane` | `test_engine_clocked.py` |
| `S-line` | `test_engine_bus.py` |
| `S-range` | `test_compiler.py`, `test_engine_clocked.py` |
| `S-table` | `test_compiler.py` |
| `T1-sample` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T1-unanchored` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T2-window` | `test_analysis.py`, `test_engine_timed.py`, `test_mutation_analysis.py` |
| `T3-latency` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T4-profile` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T5-abort` | `test_analysis.py`, `test_engine_bus.py`, `test_fz.py`, `test_lead.py`, `test_mutation_analysis.py` |
| `T5-overrun` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T5-stall` | `test_analysis.py`, `test_engine_clocked.py`, `test_lead.py` |
| `T5-startup` | `test_mutation_analysis.py`, `test_startup.py` |
| `T5-timeout` | `test_analysis.py`, `test_mutation_analysis.py` |
| `T5-watch` | `test_analysis.py`, `test_engine_bus.py`, `test_fz.py`, `test_mutation_analysis.py` |
| `OVERRUN` | `test_analysis.py`, `test_engine_timed.py`, `test_mutation_gaps.py` |
| `EARLY` | `test_engine_bus.py`, `test_engine_timed.py`, `test_fuzz_unit.py`, `test_mutation_gaps.py`, `test_startup.py` |
| `TIMEOUT` | `test_engine_bus.py`, `test_engine_clocked.py`, `test_engine_timed.py`, `test_fuzz_devices.py`, `test_fuzz_unit.py`, `test_fz.py`, `test_mutation_gaps.py` |
| `LEVEL` | `test_engine_timed.py`, `test_mutation_gaps.py`, `test_protocols.py` |
| `EDGE` | `test_engine_timed.py`, `test_mutation_gaps.py` |
| `CLK_PHASE` | `test_engine_clocked.py`, `test_mutation_gaps.py` |
| `ABORT` | `test_engine_bus.py`, `test_engine_clocked.py`, `test_engine_timed.py`, `test_fuzz_devices.py`, `test_fz.py`, `test_mutation_gaps.py`, `test_protocols.py` |
| `OVERFLOW` | `test_cosim.py`, `test_engine_core.py`, `test_mutation_gaps.py`, `test_system.py` |
| `UNDERRUN` | `test_engine_clocked.py`, `test_mutation_gaps.py`, `test_spi.py` |
| `BUS` | `test_engine_bus.py`, `test_fz.py`, `test_i2c.py`, `test_lead.py`, `test_mutation_gaps.py`, `test_spi.py` |
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
