# Plan

Owner: supervisor role. Breaks the milestones in chip-spec §4 into tasks. Date changes to a
milestone are proposed to verification, which owns chip-spec. Replaces the old plan in
`docs/superpowers/plans/` (kept for its task designs).

Status as of **2026-11-21**. Dates marked * are the supervisor's proposal, not yet agreed by the
owning role.

## 1. Milestones and gates

| Milestone | By | Done when (chip-spec §4) | Status |
|---|---|---|---|
| M1 first end-to-end chip | Nov 29 | one engine, host SPI and the program SRAM run a UART program; hardened, precheck and GL clean; co-sim passes on UART | **Evidence in, but one item**: one engine with FIFOs, clocked lanes and the bus detector hardens clean on `main` (gds, precheck and `gl_test` green on 7085811); co-sim passes on UART, SPI and I2C at RTL (#25, #53, #56), and at gate level on #38's netlist (44 pass). Left: gate-level co-sim on `main`'s current netlist (A12), then the `m1-first-chip` tag |
| M2 Core complete | Dec 13 | Core passes on the FPGA | **At risk**: the FPGA bitstream is built but bring-up waits on the board session (#46); engine 1 not started (risk 1, risk 5) |
| **Gate** | **Dec 15** | Core on the FPGA, **and** the fuzzer model beats the baseline; otherwise the fuzz-unit RTL is cut | Model half met: learning beats the baseline, 5/5 seeds on each device (#34); FZ ops in the model and compiler (#57). Core-on-FPGA half at risk |
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
| V3b | model to v1: clocked XFER, START/STOP, MEAS (UART path complete for M1) | verification | V3 | Nov 20 | Done (#29–#33, #39, #40, #42, #73) |
| V4 | co-sim harness, model vs RTL trace | verification | V2, D4 | Nov 18 | Done (#16) |
| V4b | co-sim on the UART program, all roles, RTL pin drives, gate level | verification | V4, D5 | Nov 27 | Done (#25); SPI (#53) and I2C (#56) too |
| V5 | FPGA bring-up (Basys 3): layers 1–5, then LOOPBACK | verification | D4 | Nov 20 | **Late**: draft #46, bitstream built; waits on the board session. LOOPBACK follows D7 |
| V6 | Core on the FPGA: UART, SPI, I2C against real devices | verification | V5, D7 | Dec 13 | Not started |
| V7 | fuzzer instructions (FZ): behaviour and cycle costs in isa.md | verification | — | Dec 1 | Done (#57) |
| V8 | host software (`host/`): load an image, run, read back | verification | D2 | Nov 27 | Draft #46 |
| V9 | random programs, the rule cross-check and mutation testing | verification | V3b | — | Done so far (#58, #61–#79); open items in isa §5 |
| D1 | CI path filter | devOps | — | Nov 10 | Done (#9) |
| D2 | 2 × 512x32 SRAM on `main` at 8x4 | devOps | — | Nov 12 | Done (#10) |
| D3 | RTL bring-up layers 1–4: TESTMODE, host SPI, GPIO override, LOOPBACK | devOps | D2 | Nov 12 | Done (#11, #12) |
| D4 | engine 0: fetch/issue, untimed ops, timed steps | devOps | D3 | Nov 14 | Done (#13, #14) |
| D5 | FIFOs, PUSH/POP, `src`/`dst` = FIFO, TXDATA/RXDATA (a host-fed UART needs them) | devOps | D4 | Nov 21 | Done (#38) |
| D6 | first full place-and-route with engine 0: area and slow-corner timing | devOps | D4 | Nov 21 | Done: 219.9K µm² cells after PnR (#36) |
| D7 | clocked lanes, bus detector (START/STOP, SELECT), engine 1 (layer 6) | devOps | D5 | Dec 5* | **At risk**. On one engine: clocked XFER (#48), duplex, the OD let-go and clock stretching (#50), the bus detector, COND START/STOP, bus exits and gated lines (#51). Left: the area pass, then engine 1 (order from #45). No devOps work since 11-12 (risk 5) |
| D8 | capture log, DBG_HALT and readback | devOps | D7 | Dec 9* | Not started |
| D9 | area and timing verdict at engine 1's harden | devOps | D7 | Nov 18* | **Overdue**. Clock decided: 40 MHz (#45) |
| D10 | fuzzer model (`toolchain/emu/fuzz/`) and the baseline comparison | devOps | V7 | Dec 12* | Stage 1 done (#34): beats the baseline; the LFSR draw sequence confirmed (#41) |
| D11 | CI on TT's `ttihp27a` flow (LibreLane dev4), co-sim in CI (A1) | devOps | — | Dec 1* | Draft #54: dev4 fails setup at the slow corner; timing-driven placement fixes it locally (+2.34 ns at 25 ns, 11-12), not pushed |
| S1 | `challenge.md`, `plan.md` | supervisor | — | Nov 10 | Done (#20) |
| S2 | `info.md` and `info.yaml` pinout to architecture §8 | supervisor | D3 | Nov 13 | Done (#26) |
| S3 | `writeup.md` skeleton; `demo.md` for the FPGA demo | supervisor | — | Nov 20 | Skeletons done (#21); demo.md agreed (#52); writeup filled in as work lands |
| S4 | M1 evidence and the `m1-first-chip` tag | supervisor | V4b, D6 | Nov 29 | Evidence in §1; tag after A12 |
| S5 | Dec 15 gate report: met or not, with evidence and the cut order | supervisor | V6, D10 | Dec 14 | — |

## 3. Risks

Each risk names the role that decides it and the date it's decided by.

| # | Risk | Effect | Mitigation | Decides | By |
|---|---|---|---|---|---|
| 1 | **Area: Core may not fit 8x4 as built.** devOps's local two-engine run with FIFOs (`e1d`, 11-11): 419.8K µm² cells, 47.9% utilization with the macros; fails sign-off on hold (−0.10 ns, fast corner), 2 slew and 1 cap violations; detailed routing alone takes 2h37m, past CI's 2-hour gds limit. One engine with the clocked lanes and the bus detector is now 37.8% (dev4, local) | two engines are Core, and chip-spec §4's cut order cuts nothing in Core | order (#45): the area pass, then engine 1. Fallback trims (#45), only if D9 says two engines still don't fit: RX FIFO 8 → 4, fewer debug-only readable registers | devOps (D9 verdict), then verification (Core scope) | **Nov 18, passed**; new date Nov 28* |
| 2 | Timing at 40 MHz (25 ns). `main` (one engine, dev3 flow) +2.24 ns placed; TT's dev4 flow −2.82 ns, **+2.34 ns** with timing-driven placement (local, 11-12) | the final harden runs on TT's current flow, so dev4 is what counts | push #54 with timing-driven placement; devOps reports slow-corner slack in each hardware PR | devOps | at D9 |
| 3 | The fuzzer result holds only in the model | the gate also needs Core on the FPGA | V5 bitstream built (#46); the host-driven fuzzer ships either way | verification (gate call) | Dec 15 |
| 4 | FPGA bring-up late | M2 and the gate slip together | the bitstream and host driver are ready (#46); the board session is the only step left | verification | Nov 28* |
| 5 | **No devOps work since 11-12.** The devOps session stopped to wait for its human's go | D7 (engine 1), D9 and #54 wait; engine 1 has 3 weeks to M2 | the next step is ready to run (#54's fix, then the area pass); the humans agree a restart date | devOps | Nov 24* |

## 4. Weekly status

| Week of | Done | Next | Risks | Gate |
|---|---|---|---|---|
| Nov 18 | clocked XFER, duplex and stretching, the bus detector and gated lines on one engine (#48, #50, #51); co-sim on SPI and I2C (#53, #56); FZ ops (#57); random programs and mutation testing (#58–#79); demo.md agreed (#52); TT's Q3 answer | gate-level co-sim on `main` and the M1 tag; restart devOps (#54, the area pass, engine 1); the FPGA board session | engine 1 not started and no devOps work since 11-12; D9 overdue | model half met; FPGA half waits on the board session |
| Nov 11 | engine untimed half (#13), bring-up layers 1–4 (#10–#12), model V3b mostly done (#29–#33), fuzzer model beats the baseline (#34), timed steps (#14), area pass (#36), FIFOs (#38), sign-off clock 40 MHz and the engine order (#45), CI apt retries (#47) | the area decision (risk 1), co-sim (#16 merged, #25), FPGA bring-up (#46); the clocked XFER on one engine (order from #45) | two engines fail sign-off (risk 1); thin timing margin | model half met; FPGA half not started |
| Nov 10 | v1 encoding, trace port, model v1 timing (#5–#8, #17–#19), CI filter (#9); engine stack in review (#10–#14), co-sim draft (#16) | merge #10–#14, FIFOs, co-sim | area trend, slow corner, no fuzzer model | not started; 5 weeks out |

## 5. Open asks

Requests between roles, collected from the `For <role>:` lines in handoffs and PR descriptions.
Each status PR refreshes the table and closes rows that are done.

| # | Ask | From | To | Since | Status |
|---|---|---|---|---|---|
| A1 | Wire co-sim into CI: `make -C test/cosim` after test, `make GATES=yes` after gl_test | verification | devOps | 11-10 | In draft #54 |
| A3 | The D9 area verdict for two engines plus FIFOs (risk 1), with a date | supervisor, verification | devOps | 11-11 | **Overdue** (Nov 18) |
| A7 | `src/uart_tx.v` is unused but still in `info.yaml` `source_files` and `test/Makefile` | supervisor | devOps | 11-10 | Open |
| A12 | Gate-level co-sim (`make -C test/cosim GATES=yes`) on `main`'s current gds netlist, the last M1 item; or #54's `cosim_gl` job once it merges | supervisor | verification | 11-21 | Open |
| A13 | architecture §8's "any pad pull-ups" check is answered: none (TT, 11-21, challenge Q3); §6 could note the flash-sampling margin at 40 MHz (challenge §5) | supervisor | verification | 11-21 | Open |
| A14 | `test/` follow-ups: drop `meas_values()`, `cosim(..., sample2=True)` for the MON data2 filters, the `devices.py` restart with CS low, confirm-0 bus flag timing, RAW REL let-go, late bits one per cycle; the random lead-block programs in #54's co-sim step | verification | devOps | 11-12 | Open |

Closed:
- A9, A10, A11: the dates, the RP2350, the demo.md review (#52; demo.md updated in this PR);
- A2, A4, A6 (#47); A5, the engine order (#45); A8, the competitor comparison (board, 11-11);
- Core-scope trims for risk 1 (#45); `clock_hz` to 40 MHz (#49); the LFSR draw sequence (#41).
