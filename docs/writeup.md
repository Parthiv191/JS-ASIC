# Writeup: a role-switching protocol emulator

Owner: supervisor role. The story the judges read. Status: **skeleton (2026-11-10)**. Sections
marked *To come* fill in as the work lands. Each section links to the design docs, which have
the detail.

## 1. What it is

A Tiny Tapeout chip (IHP CMOS5L, 8x4 tiles) for debugging and reverse-engineering serial buses.
Each protocol is a short program, written once as an owner-labelled description. The same
program runs in any role: controller, target or monitor. That is **role switching**. Two
identical timing engines share one program memory, so the chip can play both sides of a bus,
or one side and a monitor. A **learning bus fuzzer** mutates what the chip sends, and keeps the
inputs that make a device respond in new ways. Scope and limits: `design/chip-spec.md`.

## 2. Why this answers the challenge

The brief puts flexibility first: can the chip run protocols it wasn't designed for?

| The brief asks | Our answer | Detail |
|---|---|---|
| Reprogrammable after fabrication | programs and tables load over host SPI; standalone boot from flash (Plus) | `design/architecture.md` §4, §6 |
| General purpose | a cycle-level timing ISA with no protocol logic; the only helpers are generic (CRC, a START/STOP/select detector) | `design/isa.md` |
| UART, SPI, I2C | all three in every role from one description each | `design/chip-spec.md` §5 |
| Protocols it wasn't designed for | PS/2, JTAG, SWD and 1-Wire paper-checked against the ISA | `design/chip-spec.md` §5 |
| Novelty | role switching, the on-chip learning fuzzer, MEASURE timing capture | §4, §5 |
| Verification | two independent readings of one spec (model and RTL), compared cycle by cycle | §6 |

Full compliance map: `challenge.md`.

## 3. Architecture

*To come, from `design/architecture.md` §1–3:* the two engines, the interleaved program SRAM
(engine 0 on even cycles, engine 1 on odd), the shared image tables, the pin driver (every output
from a flop), the host SPI port, and the debug bundle (TESTMODE, LOOPBACK, GPIO override, the
trace port).

## 4. The ISA and role switching

*To come:* how an owner-labelled step becomes "do" for its owner and "follow" for everyone else;
why timing is anchored to the other party's edges (no drift over long frames); the timing rules
(isa §6) that the compiler checks so the chip needn't. One worked example: a UART frame as
transmitter, receiver and monitor from the same program.

## 5. The learning bus fuzzer

**First result (model, #34).** Four devices with planted behaviour, 30K iterations, 5 seeds:

| Device | Learning | Baseline (no feedback) |
|---|---|---|
| unlock (3-byte sequence) | 5/5 | 0/5 |
| leak (progress shows only in timing) | 5/5 | 0/5 |
| hang | 5/5 | 0/5 |
| 16-bit magic (control, no partial feedback) | 0/5 | 0/5 |

The model also caught a design flaw: a 16-bit LFSR with one step per draw replays the same ~5 200
mutations and can't learn, so the unit uses a 32-bit LFSR with 16 steps per draw.

*To come, from `design/bus-fuzzer.md`:* corpus distillation on the chip, the bitmap of observed
responses, hang detection, and what the Dec 15 gate decided (on-chip or host-driven).

## 6. Verification

The spec (`design/isa.md`) is the contract. Two roles read it independently: verification
writes the Python reference model, devOps writes the RTL. Neither copies the other. Wherever
they disagree, the spec is fixed or one side is, and the ruling goes in `design/decisions.md`.

| Layer | What it catches | Status |
|---|---|---|
| model tests and cross-role tests | the model misreading the spec | running in CI |
| co-sim: model vs RTL, every trace-port pulse compared | the RTL misreading the spec | draft (#16), 36 directed tests |
| cocotb tests per RTL block, at RTL and gate level | block bugs, synthesis mismatches | running in CI |
| mutation checks: plant a bug, a test must fail | tests too loose to fail | every RTL PR so far |
| formal (SymbiYosys) on small blocks | handshake bugs | *to come* |
| FPGA (Basys 3), then real devices | real timing and electrical surprises | *to come* |

Detail and status: `design/verification.md`. *To come:* the bugs each layer actually caught.

## 7. Physical design

*To come, from `design/architecture.md` §9:* the SRAM placement, area per block (synthesis vs
place-and-route), the sign-off clock and why.

## 8. How we built it, and AI use

Three roles, each run as a Claude Code session with a human lead: **verification** (spec, model,
co-sim, FPGA), **devOps** (RTL, physical design, CI, the fuzzer algorithm) and **supervisor**
(plan, challenge compliance, this writeup). Each role edits only the paths it owns. The roles
share context through Delta threads: a board lists each session, and every session ends with a
handoff that the next one picks up. Rows marked "Claude (delegated)" in `design/decisions.md` are
decisions the verification role asked Claude to make on evidence; the rest were made by a role.

*To come:* what worked, what didn't, and what we'd change.

## 9. Results

*To come at each milestone:* M1 (first end-to-end chip, Nov 29), M2 (Core on the FPGA, Dec 13),
the Dec 15 gate, the improvement rounds, and the final tag submitted.

## 10. Limits

*To come:* what the chip can't do and why (chip-spec §5–6), and what was cut (chip-spec §4).
