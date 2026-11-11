# Plan

Owner: supervisor role. Breaks the milestones in chip-spec §4 into tasks. Date changes to a
milestone are proposed to verification, which owns chip-spec. Replaces the old plan in
`docs/superpowers/plans/` (kept for its task designs).

Status as of **2026-11-10**. Dates marked * are the supervisor's proposal, not yet agreed by the
owning role.

## 1. Milestones and gates

| Milestone | By | Done when (chip-spec §4) | Status |
|---|---|---|---|
| M1 first end-to-end chip | Nov 29 | one engine, host SPI and the program SRAM run a UART program; hardened, precheck and GL clean; co-sim passes on UART | On track, at risk on the FIFOs (D5) |
| M2 Core complete | Dec 13 | Core passes on the FPGA | At risk: FPGA bring-up not started |
| **Gate** | **Dec 15** | Core on the FPGA, **and** the fuzzer model beats the baseline; otherwise the fuzz-unit RTL is cut | At risk: no fuzzer model yet (D10) |
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
| V3b | model to v1: clocked XFER, START/STOP, MEAS (UART path complete for M1) | verification | V3 | Nov 20* | In progress |
| V4 | co-sim harness, model vs RTL trace | verification | V2, D4 | Nov 18* | Draft #16, waits on #11–#14 |
| V4b | co-sim on the UART program, all roles, RTL pin drives, gate level | verification | V4, D5 | Nov 27* | Not started |
| V5 | FPGA bring-up (Basys 3): layers 1–5, then LOOPBACK | verification | D4 | Dec 5* | Not started (`fpga/` CI exists) |
| V6 | Core on the FPGA: UART, SPI, I2C against real devices | verification | V5, D7 | Dec 13 | Not started |
| V7 | fuzzer instructions (FZ): behaviour and cycle costs in isa.md | verification | — | Dec 1* | Open |
| V8 | host software (`host/`): load an image, run, read back | verification | D2 | Nov 27* | Not started (needed for R8) |
| D1 | CI path filter | devOps | — | Nov 10 | Done (#9) |
| D2 | 2 × 512x32 SRAM on `main` at 8x4 | devOps | — | Nov 12* | Open #10 |
| D3 | RTL bring-up layers 1–4: TESTMODE, host SPI, GPIO override, LOOPBACK | devOps | D2 | Nov 12* | Open #11, #12 |
| D4 | engine 0: fetch/issue, untimed ops, timed steps | devOps | D3 | Nov 14* | Open #13, #14 |
| D5 | FIFOs, PUSH/POP, `src`/`dst` = FIFO, TXDATA/RXDATA (a host-fed UART needs them) | devOps | D4 | Nov 21* | Not started |
| D6 | first full place-and-route with engine 0: area and slow-corner timing | devOps | D4 | Nov 21* | Partial: #13 gds passed; #14 253.5K µm², slow corner −2.90 ns; area branch 219.9K, −2.76 ns |
| D7 | clocked lanes, bus detector (START/STOP, SELECT), engine 1 (layer 6) | devOps | D5 | Dec 5* | Not started |
| D8 | capture log, DBG_HALT and readback | devOps | D7 | Dec 9* | Not started |
| D9 | area and timing verdict at engine 1's harden; sign-off clock 50 or 40 MHz | devOps | D7 | Dec 6* | Not started |
| D10 | fuzzer model (`toolchain/emu/fuzz/`) and the baseline comparison | devOps | V7 | Dec 12* | **Not started** |
| S1 | `challenge.md`, `plan.md` | supervisor | — | Nov 10 | Done (#20) |
| S2 | `info.md` and `info.yaml` pinout to architecture §8 | supervisor | D3 | Nov 13* | Not started |
| S3 | `writeup.md` skeleton; `demo.md` for the FPGA demo | supervisor | — | Nov 20* | Skeletons in review |
| S4 | M1 evidence and the `m1-first-chip` tag | supervisor | V4b, D6 | Nov 29 | — |
| S5 | Dec 15 gate report: met or not, with evidence and the cut order | supervisor | V6, D10 | Dec 14 | — |

## 3. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Area: post-PnR cells run 1.6–1.7× Yosys (#11, #13). #14 is 253.5K µm² after PnR (Yosys 167K); devOps's area branch cuts it to 219.9K. With the macros that's ~31% of the 1.208M µm² core for one engine without FIFOs, clocked lanes, the detector or engine 1 | two engines plus the rest may land near the 60% target | devOps reports PnR area per PR; the area branch first; the verdict at D9; cut order in chip-spec §4 |
| Timing: slow corner −2.90 ns at 20 ns on #14, −2.76 ns on the area branch; +2.24 ns at 25 ns | 40 MHz fallback | decided at D9 (decision 11-10); no ISA change |
| No fuzzer model, 5 weeks to the gate | the fuzz-unit RTL is cut by rule | start D10 after V7; the host-driven fuzzer ships either way |
| FPGA bring-up starts late | M2 and the gate slip together | V5 starts as soon as #11–#14 merge |
| The engine stack #10–#14 lands in one go | co-sim and the FIFOs wait on it | merge in order as each goes green |

## 4. Weekly status

| Week of | Done | Next | Risks | Gate |
|---|---|---|---|---|
| Nov 10 | v1 encoding, trace port, model v1 timing (#5–#8, #17–#19), CI filter (#9); engine stack in review (#10–#14), co-sim draft (#16) | merge #10–#14, FIFOs, co-sim | area trend, slow corner, no fuzzer model | not started; 5 weeks out |
