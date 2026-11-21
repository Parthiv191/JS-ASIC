# Challenge compliance

Owner: supervisor role. Maps every requirement in the brief and every Tiny Tapeout (TT) and IHP
submission rule to where the repo meets it. Re-checked against `main` weekly.

Brief: https://blog.janestreet.com/protocol-emulator-asic-competition/ (posted Sep 10, 2026;
"Update November 9th" read on 2026-11-10; re-checked 2026-11-21, no newer update and no submission form yet). Status: **Met**, **Partial**, **Not yet**, **Open**
(a question to ask).

## 1. Facts from the brief

| Fact | Source |
|---|---|
| 8x4 tiles is the limit, confirmed Nov 9 for the March 2027 CMOS5L shuttle; tile sizes are final | update of Nov 9 |
| Submissions close **Jan 18, 2027**; we submit by Jan 17 (chip-spec §4) | rules |
| A final submission form appears on the brief closer to the deadline; nothing to submit yet | submission |
| The chip must be reprogrammable after fabrication, within its timing and I/O limits | challenge |
| Start with UART, SPI, I2C. Stretch goals: low-speed USB and 10 Mbit Ethernet | challenge |
| Novel verification is valued: formal, constrained random and AI-assisted are named; any tools, described in the write-up | verification |
| Judging has no fixed weights: flexibility first (protocols it wasn't designed for), then protocols shown, what the architecture uniquely enables, how it was designed and verified. The write-up weighs heavily | judging |
| Selected designs announced early February 2027; tapeout on the March 2027 shuttle | dates |
| After the deadline only small non-functional fixes (e.g. a re-harden) are allowed | rules |

## 2. Requirements

| # | Requirement | Where we meet it | Status |
|---|---|---|---|
| R1 | Open-source, general-purpose protocol emulator | timing-engine ISA, no fixed protocol logic (chip-spec §1); Apache 2.0 `LICENSE` | Partial: engine 0 on `main` with timed steps, FIFOs, clocked lanes, duplex, clock stretching, the bus detector and gated lines (#13, #14, #38, #48, #50, #51); engine 1 to come (plan risk 1) |
| R2 | Reprogrammable after fabrication | programs load over host SPI into the program SRAM (architecture §4); standalone boot from flash is Plus | Partial: the host loads the program SRAM and tables over SPI on `main` (#11, #13, #14); host software (R8) to come |
| R3 | UART, SPI, I2C | all three in every role (chip-spec §5) | Partial: library UART, SPI and I2C (with a register read) in every role (#42, #73); the RTL matches the model on all three in co-sim (#25, #53, #56); not yet on the FPGA (#46) |
| R4 | Stretch: low-speed USB, 10 Mbit Ethernet | USB-LS sniffer is Stretch (chip-spec §4); Ethernet not planned | Not yet; Ethernet out of scope |
| R5 | Controller and target roles (a plus) | role switching: one description, every role | Partial |
| R6 | IHP 130 nm CMOS5L through the TT IHP Verilog template | decisions 11-02; CI on the CMOS5L template | Met |
| R7 | At most 8x4 tiles | `info.yaml` `tiles: "8x4"` | Met |
| R8 | Program loading is part of the design; any documented host | host SPI (architecture §4); host software in `host/` | Not yet: the host driver is in draft #46 |
| R9 | Use `rst_n`; state is lost when deselected, so programs reload | reset sync and safe reset (architecture §2) | Met in the RTL (#11); programs reload after reset through the host |
| R10 | Software side: assembler or toolchain plus example programs | `toolchain/` (compiler, model); `toolchain/emu/protocols/` | Partial: UART, SPI, I2C and an I2C register read (`toolchain/emu/protocols/`); more protocols planned |
| R11 | Public repo with RTL, tests and a passing GDS and precheck from the TT flow | CI `gds.yaml` (harden, precheck, `gl_test`) | Partial: gds, precheck and `gl_test` pass on `main` with engine 0 (latest 7085811); engine 1 to come |
| R12 | Architecture and ISA documentation | `docs/design/architecture.md`, `isa.md` | Met (kept current by verification) |
| R13 | Verification write-up | `docs/design/verification.md`; `docs/writeup.md` | Partial: co-sim per pin and cycle at RTL and gate level (#16, #25), random programs with a rule cross-check (#58, #77, #79), mutation testing of the model (#62–#74); `writeup.md` to fill in |
| R14 | Describe AI use in the write-up | `decisions.md` "Claude (delegated)" rows; `docs/writeup.md` | Partial |
| R15 | Document any external parts the design needs | architecture §8 (pull-ups); `docs/info.md` | Met: no on-chip pull-ups (TT, 11-21, Q3), so `info.md` lists every pull-up the board must supply |
| R16 | Stick to the TT flow; a custom GDS action only for the 6-hour limit, still passing precheck | CI uses the TT flow | Met |
| R17 | Gate-level simulation strongly recommended | CI `gl_test` | Met |
| R18 | One entry per team | one repo | Met |

## 3. Tiny Tapeout and IHP submission rules

| Rule | Where | Status |
|---|---|---|
| Tile size in `info.yaml` | `tiles: "8x4"` | Met |
| `info.yaml` fields: title, author, description, language, `clock_hz`, `top_module` (`tt_um_` prefix), `source_files`, pinout | `info.yaml` | Met: description and pinout per architecture §8 (#26); `clock_hz` see the timing row |
| Datasheet `docs/info.md`: how it works, how to test, external hardware; images < 512 KB each, < 1 MB in all | `docs/info.md` | Met (#26); updated as features land |
| Pins: 8 in, 8 out, 8 bidirectional, plus clock and reset; no analog pads | architecture §8 | Met (#26) |
| Pad speed (TT Discord, 11-10): ~60 MHz out, ~75 MHz in; estimates until the first CMOS5L chips return, end of 2027 (TT, 11-21) | the fastest pin toggles at clk/2 = 20 MHz at 40 MHz; host SPI and flash SCK at ≤ clk/8 | Met |
| Demo board I/O is 3.3 V; external parts go on a Pmod | architecture §8 | Partial: no on-chip pull-ups or pull-downs (TT, 11-21); the rest of architecture §8 "Verify against the TT demo-board docs" still to check |
| IHP SRAM macros allowed, several, if they fit and pass precheck | 2 × `RM_IHPSG13_1P_512x32` (architecture §3) | Met on the harness (#10 re-checks at 8x4) |
| Precheck clean | CI `gds.yaml` | Met on every passing gds run |
| Timing at `clock_hz` (40 MHz) | sign-off clock 40 MHz (verification, 11-11, #45); slow corner on `main` (one engine) +2.24 ns at 25 ns; devOps's local two-engine run about +0.5 ns | Met on `main`; the two-engine margin is thin (plan risk 2) |
| Open-source license: **Apache 2.0 is required** for TT designs (11-21) | `LICENSE` (Apache 2.0), `NOTICE` (our copyright and the third-party files); every file in the repo is Apache-2.0 (§6) | Met |

## 4. Open questions

| # | For | Question | Why |
|---|---|---|---|
| Q1 | Jane Street | Which MCU is on the demo board: RP2350 (the brief, Nov 9) or RP2040 (architecture §8, chip-spec §3)? **Not asked (team decision 11-10). Working answer: RP2350**, as the brief says. Host SPI uses PIO, which both chips have, so nothing in the design depends on it | host software; the pin check in architecture §8 |
| Q2 | Jane Street | Is a design that ships the host-driven fuzzer (our Dec 15 cut rule) judged the same way? **Not asked (team decision 11-10). Working answer: yes.** The brief has no fixed weighting and says fewer features with a strong architecture and verification can compete. If the cut happens, the writeup says so plainly, with the gate evidence | the Novel tier's fallback |
| Q3 | Tiny Tapeout | Pad pull-ups, and the pad round-trip delay on CMOS5L. **Answered 11-21** (TT): March 2027 is the first CMOS5L shuttle, so ~10 ns round trip and ~60–66 MHz are the best numbers until its chips return at the end of 2027; the pads have **no pull-ups or pull-downs**. What follows is in §5 | host SPI at clk/8, flash sampling (architecture §6) |

Draft messages for the human to send live in the supervisor's handoff, not here.

## 5. Q3: what TT's answer means

**Flash sampling margin** (architecture §6, at 40 MHz, clk = 25 ns, flash SCK = clk/8 = 200 ns):

| Step | Time after the SCK fall's flop edge |
|---|---|
| MISO is read from the synchronizer, 5 clk after the launching fall | 125 ns |
| so it must reach the first sync flop before edge 3 (2 sync flops) | < 75 ns |
| the path: pad round trip (~10 ns, TT estimate) + the flash's clock-to-output (W25Q: ≤ 6–8 ns; ≤ 15 ns for older 3.3 V parts) + flop and board | ~20–30 ns |
| **setup margin** | **~45–55 ns (about 2 clk)**; it still holds if the real pads are 4× slower than the estimate |
| the next bit changes at the next SCK fall, 8 clk after launch | **hold margin ~5 clk** |

Flash SCK runs at 5 MHz, far under the pads' ~60 MHz. The estimate doesn't need to be measured
before tapeout.

**No on-chip pull-ups or pull-downs:**
- **Open-drain lines** (I2C SDA and SCL, USB-LS D−) need external pull-ups on the board, as
  architecture §8 already says.
- **Unused `uio` pins float.** Until RUN every `uio` is an input. Nothing reads a line no program
  uses, so a floating pin costs nothing logically; a 10–100 kΩ pull-up keeps it quiet if wanted.
- **The straps** `ui[4]` BOOT and `ui[7]` TESTMODE must be driven by the board (the DIP switches or
  the MCU). A floating TESTMODE strap could enter the pad test at reset.
- **Flash MISO** floats while CS_N is high; it is only sampled with CS_N low. With BOOT high and no
  flash fitted, the magic check (E1) or the CRC (E3) stops the boot.

## 6. Apache 2.0

TT requires designs to be Apache 2.0 (11-21). The repo already is: `LICENSE` is Apache 2.0, the
toolchain's package says `Apache-2.0`, and the toolchain has no runtime dependencies.

| What's in the repo | Licence | Kept as Apache asks |
|---|---|---|
| our RTL, toolchain, tests and docs | Apache-2.0 | `LICENSE`, `NOTICE` |
| TT IHP Verilog template files | Apache-2.0 | `NOTICE` |
| `src/pdn_cfg.tcl`, from 2AMLogic | Apache-2.0 (no upstream NOTICE file) | source and commit in the file's header and in `NOTICE` |
| `test/sram/*.v`, IHP SRAM models | Apache-2.0 | headers kept, unmodified; `NOTICE` |
| IHP PDK cells and SRAM macros, used through the flow | Apache-2.0 | not copied into the repo |

**Rules for anything new** (proposed to every role, plan A15):
- Bring in only Apache-2.0, MIT, BSD or ISC files. Keep their headers and add a `NOTICE` line in
  the same PR.
- No GPL, LGPL, CC-BY-SA, non-commercial or unlicensed code. Other entries' repos are often
  unlicensed: read them, but don't copy from them.
- Don't add licence headers to our own files (team rule). `LICENSE` and `NOTICE` cover them.
- AI-assisted code is ours, under the same licence. Its use is documented in `decisions.md` and
  the writeup (R14).
