# Chip spec: what the chip does

Status: v1 (updated 2026-11-09), owner: verification role. Hardware: `architecture.md`. Instructions:
`isa.md`. Why: `decisions.md`.

## 1. Purpose and challenge fit

A programmable **hardware-debugging and reverse-engineering** chip for serial buses. It speaks a
protocol from a short program, plays any side of it, and can fuzz a device **on its own**.

The challenge (Jane Street protocol-emulator ASIC) asks for a general-purpose, programmable
emulator of UART, SPI and I2C. How the design covers that:

| The challenge asks | We deliver |
|---|---|
| Emulate UART, SPI, I2C | all three, in **every role** (controller/transmitter, target/receiver, monitor), from one program each |
| General purpose, not fixed protocol logic | a timing-engine ISA. The only protocol helpers are generic ones: CRC, a START/STOP/select detector, an optional line CODEC. |
| Novelty | **role switching** (one description, every role) + an **on-chip learning bus fuzzer** (corpus distillation) + MEASURE (timing capture; autobaud in Plus) |
| Open source, documented AI use | repo + `decisions.md` + the writeup |

## 2. Use cases

Roles: P0 = party 0 (e.g. the controller), P1 = party 1 (e.g. the target), MON = monitor (isa §2).

| Use | Example | Roles |
|---|---|---|
| Impersonate a device | a fake I2C sensor or SPI flash, to test a microcontroller's driver | P1 (fuzz the replies) |
| Drive a device | read or write a real sensor or EEPROM | P0 |
| Sniff a bus | log a microcontroller ↔ sensor conversation, with timing | MON |
| Fuzz a device | find hidden registers, unlock sequences, hangs | P0 + fuzz unit; engine 1 can log as MON |
| Measure | pulse widths, response latency (MEASURE L1); unknown baud rates (autobaud, Plus) | any role |
| Self-test | the two engines run a protocol against each other on-chip (LOOPBACK) | P0 + P1 |

## 3. Operating modes

| Mode | What happens |
|---|---|
| Hosted | a host (the TT demo board's RP2350, or a PC through the Basys 3 bridge) loads programs, sets roles and moves data |
| Standalone | at power-up it boots from an external SPI flash (with a CRC check), runs, and reports on a UART text pin (`architecture.md` §6–7) |
| Test | TESTMODE strap and the debug registers for bring-up (`architecture.md` §2, §5) |

One protocol runs at a time.

## 4. Scope tiers, cut order and milestones (deadline Jan 18; we submit by Jan 17)

| Tier | Contents | Rule |
|---|---|---|
| **Core (never cut)** | 2 engines + the ISA core; UART; SPI with CS (SELECT bus); I2C (START/STOP, let-go, readback, owner-side stretching, lead blocks, `BR ROLE`); MEASURE L1; capture log; host SPI; DBG_HALT + readback, SCRATCH, TESTMODE, LOOPBACK, GPIO override; verification (model co-sim, GL test, formal on small blocks) | must pass on the FPGA before Plus or Novel RTL starts |
| **Novel** | on-chip fuzz unit | **gate: Core passes on the FPGA by Dec 15**, and the model shows learning beats the baseline. Otherwise ship the host-driven fuzzer (same algorithm). |
| **Plus** | CRC unit; DBG_STEP, DBG_EXEC, BREAK_PC; standalone boot + REPORT; MEASURE L2 (`PROF`, autobaud) | after Core is verified, in that order |
| **Stretch** | low-speed USB, sniffer first. It needs a CODEC lane (a line encoder/decoder) and the SE0 bus kind (USB's end-of-packet: both lines low); isa §4.7, §4.10 | only if everything above is done and area allows |
| **Later (no v1 hardware)** | clock divider (signals > 1.3 ms per profile), SWITCHED push-pull (PP) lines (full-speed SWD, USB device), WAIT_ANY/CHOICE, ARB, a 3-line TAP lane | reserved in the encoding |

**Cut order if behind:** Stretch → PROF → standalone/REPORT → Plus debug → CRC → fuzz-unit RTL
(host-driven fuzzing stays) → never Core.

**Milestones.** Build early, then improve; `main` stays submittable throughout.

| Milestone | By | Done when |
|---|---|---|
| M1: first end-to-end chip | Nov 29 | one engine, host SPI and the program SRAM run a UART program; hardened, precheck and GL clean; co-sim passes on UART (so the model's UART path must be on ISA v1 first) |
| M2: Core complete | Dec 13 | the Core tier passes on the FPGA. That meets the Dec 15 gate with two days to spare. |
| Improvement rounds | from M2 to Jan 10 | Novel first if the gate is met (Core on the FPGA by Dec 15, and the model shows learning beats the baseline), then Plus in the order above, then Stretch. Each round ends submittable. |
| Freeze and submit | Jan 11–17 | final harden, full GL regression, final docs; submit by Jan 17 |

- Every merge keeps gds, precheck and the GL test green (and co-sim, once it exists). The
  supervisor tags each milestone and round; a late failure means submitting the last good tag.
- Each Novel, Plus and Stretch block sits behind a Verilog parameter, so a cut is a one-line
  change.

## 5. Protocols and limits

The limits assume the 40 MHz sign-off clock (§7) and `tolerance_ppm = 20000`, i.e. at most 2% total clock-rate difference
between any two parties. They follow from isa §6 (hand-checked; the compiler gives the exact
answer).

| Protocol | Roles | Limits |
|---|---|---|
| UART 8N1 | tx, rx, mon (autobaud in Plus) | ~635 baud to ~2.9 Mbaud (bit ≥ 14 cycles, isa §6 rule T1). Back-to-back frames need bit ≥ 19 cycles (~2.1 Mbaud, isa §6 rule T5-watch). At ≤ 1% drift, ~4 Mbaud. 115200 baud is 347 cycles. |
| SPI modes 0–3 | controller, target, mon; CS framing via SELECT | Controller: H ≥ 2 by rule; the real limit is the device. Target mode 3: H ≥ 6 (3.33 MHz) with one `BR ROLE` between bytes. Target mode 0: H ≥ 7 unrolled, ≥ 9 with a branch loop (~2.2 MHz). |
| I2C | controller, target, mon; repeated START, NACK, mid-byte STOP, address match | 100 kHz (H = 200). Fast mode at H = 52 (≤ 385 kHz); with stretch tracking and a 300 ns rise time, the real rate is ~345 kHz. Data hold ~300 ns (`ldelay` 12). In Fast mode the repeated-START path allows at most ~1 instruction before `abort_pc`'s COND. **As target the chip doesn't stretch** (UNDERRUN sends 0xFF). |
| Paper-checked (Later tier) | PS/2 host/MON, JTAG controller, SWD (open-drain, ≤ ~1 MHz), 1-Wire | fit the ISA (protocol review, 11-05) |

H = a clock half-period, in engine cycles.

## 6. Not on the chip

- No program checking at load time. The compiler's rules (isa §5–6) are the check; the chip
  has timeouts and error flags.
- No protocol-specific modes. The generic helpers are listed in §1.

## 7. Resources

| Item | Value |
|---|---|
| Clock | 40 MHz (sign-off clock, decided 11-11) |
| Engines | 2, identical, any role |
| Program memory | 512 instructions, shared, interleaved fetch |
| Fuzzer / capture memory | 2 KB SRAM: 32 corpus slots + 8K-bit bitmap, or 512 capture records |
| Per engine | 8 registers, profile 7, TX FIFO 4, RX FIFO 8 |
| Shared by both engines | 8 profiles, 4 lanes, 8 lines, CRC and bus config |
| Area | ~46% of the 8x4 core (estimate, architecture §9) |

## 8. Open

| Item | Notes |
|---|---|
| Real engine area | first engine synthesis (devOps) |
| TT demo-board specifics | pad voltage, pin conflicts (architecture §8) |
