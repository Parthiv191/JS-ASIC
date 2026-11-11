# Plan

Owner: supervisor role. Breaks the milestones in chip-spec §4 into tasks. Date changes to a
milestone are proposed to verification, which owns chip-spec. Replaces the old plan in
`docs/superpowers/plans/` (kept for its task designs).

Status as of **2026-11-11**. Dates marked * are the supervisor's proposal, not yet agreed by the
owning role.

## 1. Milestones and gates

| Milestone | By | Done when (chip-spec §4) | Status |
|---|---|---|---|
| M1 first end-to-end chip | Nov 29 | one engine, host SPI and the program SRAM run a UART program; hardened, precheck and GL clean; co-sim passes on UART | On track: one engine with FIFOs hardens clean on `main` (#38); left: co-sim on UART (#25; harness #16 merged) |
| M2 Core complete | Dec 13 | Core passes on the FPGA | At risk: FPGA harness in draft (#46); Core may not fit 8x4 as built (risk 1) |
| **Gate** | **Dec 15** | Core on the FPGA, **and** the fuzzer model beats the baseline; otherwise the fuzz-unit RTL is cut | Model half on track: learning beats the baseline in the model, 5/5 seeds on each device with partial feedback (#34); Core-on-FPGA half at risk |
| Improvement rounds | Jan 10 | Novel, then Plus, then Stretch; each round ends submittable | — |
| Freeze, submit | Jan 11–17 | final harden, full GL regression, final docs; hard deadline Jan 18 | — |

Tags on `main`: `m1-first-chip`, `m2-core`, `r1-…` per round. The last good tag is what we
submit if a later round fails.

## 2. Tasks

| # | Task | Owner | Depends on | Date | Status |
|---|---|---|---|---|---|
| V1 | v1 encoding and image format | verification | — | Nov 9 | Done (#5) |
| V2 | trace contract (architecture §5) | verification | V1 | Nov 10 | Done (#7) |
| V3 | model to v1: fetch/issue, start-up, EVT, FIFO depths | verification | V1 | Nov 10 | Done (#8, #17, #18, #19) |
| V3b | model to v1: clocked XFER, START/STOP, MEAS (UART path complete for M1) | verification | V3 | Nov 20* | Mostly done: clocked XFER, duplex, OD data, clock stretching, bus detector, START/STOP, MEAS (#29–#33), gated lines (#39); in review: lead blocks (#40), library I2C/SPI (#42), rule coverage (#44) |
| V4 | co-sim harness, model vs RTL trace | verification | V2, D4 | Nov 18* | Done (#16) |
| V4b | co-sim on the UART program, all roles, RTL pin drives, gate level | verification | V4, D5 | Nov 27* | In review #25: pins every cycle, gate level, FIFOs, the library UART |
| V5 | FPGA bring-up (Basys 3): layers 1–5, then LOOPBACK | verification | D4 | Dec 5* | Draft #46: Basys 3 harness |
| V6 | Core on the FPGA: UART, SPI, I2C against real devices | verification | V5, D7 | Dec 13 | Not started |
| V7 | fuzzer instructions (FZ): behaviour and cycle costs in isa.md | verification | — | Dec 1* | Open |
| V8 | host software (`host/`): load an image, run, read back | verification | D2 | Nov 27* | Draft #46: host driver (needed for R8) |
| D1 | CI path filter | devOps | — | Nov 10 | Done (#9) |
| D2 | 2 × 512x32 SRAM on `main` at 8x4 | devOps | — | Nov 12* | Done (#10) |
| D3 | RTL bring-up layers 1–4: TESTMODE, host SPI, GPIO override, LOOPBACK | devOps | D2 | Nov 12* | Done (#11, #12) |
| D4 | engine 0: fetch/issue, untimed ops, timed steps | devOps | D3 | Nov 14* | Done (#13, #14) |
| D5 | FIFOs, PUSH/POP, `src`/`dst` = FIFO, TXDATA/RXDATA (a host-fed UART needs them) | devOps | D4 | Nov 21* | Done (#38) |
| D6 | first full place-and-route with engine 0: area and slow-corner timing | devOps | D4 | Nov 21* | Done: one engine on `main` after #36, 219.9K µm² cells after PnR, slow corner −2.76 ns at 20 ns |
| D7 | clocked lanes, bus detector (START/STOP, SELECT), engine 1 (layer 6) | devOps | D5 | Dec 5* | **At risk** (risk 1). Order (#45): the clocked XFER on one engine first, then the area pass, then engine 1 (built locally, fails sign-off) |
| D8 | capture log, DBG_HALT and readback | devOps | D7 | Dec 9* | Not started |
| D9 | area and timing verdict at engine 1's harden | devOps | D7 | Nov 18* | **Due**: the two-engine run reached it early (risk 1). Clock decided: 40 MHz (#45) |
| D10 | fuzzer model (`toolchain/emu/fuzz/`) and the baseline comparison | devOps | V7 | Dec 12* | Stage 1 done (#34): beats the baseline; the LFSR draw sequence confirmed (#41) |
| S1 | `challenge.md`, `plan.md` | supervisor | — | Nov 10 | Done (#20) |
| S2 | `info.md` and `info.yaml` pinout to architecture §8 | supervisor | D3 | Nov 13* | Done (#26) |
| S3 | `writeup.md` skeleton; `demo.md` for the FPGA demo | supervisor | — | Nov 20* | Skeletons done (#21); filled in as work lands |
| S4 | M1 evidence and the `m1-first-chip` tag | supervisor | V4b, D6 | Nov 29 | — |
| S5 | Dec 15 gate report: met or not, with evidence and the cut order | supervisor | V6, D10 | Dec 14 | — |

## 3. Risks

Each risk names the role that decides it and the date it's decided by.

| # | Risk | Effect | Mitigation | Decides | By |
|---|---|---|---|---|---|
| 1 | **Area: Core may not fit 8x4 as built.** devOps's local two-engine run with FIFOs (`e1d`, 11-11): 419.8K µm² cells (repair buffers 102K), 47.9% utilization with the macros; fails sign-off on hold (−0.10 ns, fast corner), 2 slew and 1 cap violations; detailed routing alone takes 2h37m, past CI's 2-hour gds limit. Metal3, the only horizontal layer, is the bottleneck. Post-PnR area runs 1.6–1.7× Yosys. The clocked XFER, detector, capture and REPORT are still to come | two engines are Core, and chip-spec §4's cut order cuts nothing in Core | order (#45): the clocked XFER on the one-engine build, then the engine-area RTL pass, then engine 1; devOps: gds timeout, hold margins. Fallback trims (#45), only if D9 says two engines still don't fit: RX FIFO 8 → 4, fewer debug-only readable registers (TB, NOW, IRQ_ID). Lanes stay 4 and duplex SPI stays Core | devOps (D9 verdict), then verification (Core scope) | Nov 18* |
| 2 | Timing: sign-off clock 40 MHz (#45). `main` (one engine) +2.24 ns at 25 ns; the two-engine run about +0.5 ns | little margin once the clocked XFER, detector and engine 1 are in | devOps reports slow-corner slack at 25 ns in each hardware PR | devOps | at D9, Nov 18* |
| 3 | The fuzzer result holds only in the model | the gate also needs Core on the FPGA | V5 harness in draft #46; the host-driven fuzzer ships either way | verification (gate call) | Dec 15 |
| 4 | FPGA bring-up starts late | M2 and the gate slip together | V5 and the host driver (V8) in draft #46 | verification | Dec 5* |

## 4. Weekly status

| Week of | Done | Next | Risks | Gate |
|---|---|---|---|---|
| Nov 11 | engine untimed half (#13), bring-up layers 1–4 (#10–#12), model V3b mostly done (#29–#33), fuzzer model beats the baseline (#34), timed steps (#14), area pass (#36), FIFOs (#38) | the area decision (risk 1), co-sim (#16 merged, #25), FPGA bring-up (#46); sign-off clock 40 MHz (#45) | two engines fail sign-off (risk 1); thin timing margin | model half met; FPGA half not started |
| Nov 10 | v1 encoding, trace port, model v1 timing (#5–#8, #17–#19), CI filter (#9); engine stack in review (#10–#14), co-sim draft (#16) | merge #10–#14, FIFOs, co-sim | area trend, slow corner, no fuzzer model | not started; 5 weeks out |

## 5. Open asks

Requests between roles, collected from the `For <role>:` lines in handoffs and PR descriptions.
Each status PR refreshes the table and closes rows that are done.

| # | Ask | From | To | Since | Status |
|---|---|---|---|---|---|
| A1 | Wire co-sim into CI: `make -C test/cosim` after test, `make GATES=yes` after gl_test | verification | devOps | 11-10 | Agreed; waits on #25 |
| A2 | `.gitattributes` with `docs/design/decisions.md merge=union`, so appended rows stop conflicting | verification | devOps | 11-11 | Agreed |
| A3 | The D9 area verdict for two engines plus FIFOs (risk 1), with a date | supervisor, verification | devOps | 11-11 | Open; Nov 18 proposed |
| A4 | A retry or cache on CI's apt installs (the runner DNS outage of 11-11 failed every test and docs job for ~6 h) | verification | devOps | 11-11 | Agreed |
| A5 | Engine order: the clocked XFER on one engine, then the area pass, then engine 1 | verification | devOps | 11-11 | Open (#45) |
| A6 | `toolchain/emu/fuzz/campaign.py`: the "~0.8 s at 50 MHz" comment to 40 MHz | verification | devOps | 11-11 | Open (#45) |
| A7 | `src/uart_tx.v` is unused but still in `info.yaml` `source_files` and `test/Makefile` | supervisor | devOps | 11-10 | Open |
| A8 | Competitor comparison, posted to the board with sources | verification | supervisor | 11-11 | Open; by 11-12 |
| A9 | Agree or change the dates marked * in §2 | supervisor | verification, devOps | 11-10 | Open |
| A10 | The demo board MCU: architecture §8 and chip-spec §3 say RP2040, the brief says RP2350 (challenge Q1, working answer RP2350) | supervisor | verification | 11-10 | Open |
| A11 | Review `docs/demo.md` (**Proposed**), which verification runs on the FPGA | supervisor | verification | 11-10 | Open |

Closed: Core-scope trims for risk 1 (supervisor → verification, answered in #45); `clock_hz` to
40 MHz and challenge.md's timing row (verification → supervisor, this PR); the LFSR draw sequence
(devOps → verification, confirmed in #41).
