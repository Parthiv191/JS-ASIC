# Challenge compliance

Owner: supervisor role. Maps every requirement in the brief and every Tiny Tapeout (TT) and IHP
submission rule to where the repo meets it. Re-checked against `main` weekly.

Brief: https://blog.janestreet.com/protocol-emulator-asic-competition/ (posted Sep 10, 2026;
"Update November 9th" read on 2026-11-10). Status: **Met**, **Partial**, **Not yet**, **Open**
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
| R1 | Open-source, general-purpose protocol emulator | timing-engine ISA, no fixed protocol logic (chip-spec §1); Apache 2.0 `LICENSE` | Partial: the engine is in RTL review (#13, #14) |
| R2 | Reprogrammable after fabrication | programs load over host SPI into the program SRAM (architecture §4); standalone boot from flash is Plus | Partial: host SPI and SRAM access in #11 |
| R3 | UART, SPI, I2C | all three in every role (chip-spec §5) | Partial: UART in the model at v1; SPI and I2C need clocked lanes and the bus detector |
| R4 | Stretch: low-speed USB, 10 Mbit Ethernet | USB-LS sniffer is Stretch (chip-spec §4); Ethernet not planned | Not yet; Ethernet out of scope |
| R5 | Controller and target roles (a plus) | role switching: one description, every role | Partial |
| R6 | IHP 130 nm CMOS5L through the TT IHP Verilog template | decisions 11-02; CI on the CMOS5L template | Met |
| R7 | At most 8x4 tiles | `info.yaml` `tiles: "8x4"` | Met |
| R8 | Program loading is part of the design; any documented host | host SPI (architecture §4); host software in `host/` | Not yet: `host/` doesn't exist |
| R9 | Use `rst_n`; state is lost when deselected, so programs reload | reset sync and safe reset (architecture §2) | Partial (#11) |
| R10 | Software side: assembler or toolchain plus example programs | `toolchain/` (compiler, model); `toolchain/emu/protocols/` | Partial: UART only |
| R11 | Public repo with RTL, tests and a passing GDS and precheck from the TT flow | CI `gds.yaml` (harden, precheck, `gl_test`) | Partial: the repo is public; gds passes on the bring-up design, not yet the engine |
| R12 | Architecture and ISA documentation | `docs/design/architecture.md`, `isa.md` | Met (kept current by verification) |
| R13 | Verification write-up | `docs/design/verification.md`; `docs/writeup.md` | Partial: the writeup doesn't exist yet |
| R14 | Describe AI use in the write-up | `decisions.md` "Claude (delegated)" rows; `docs/writeup.md` | Partial |
| R15 | Document any external parts the design needs | architecture §8 (pull-ups); `docs/info.md` | Partial: `info.md` describes the old bring-up design |
| R16 | Stick to the TT flow; a custom GDS action only for the 6-hour limit, still passing precheck | CI uses the TT flow | Met |
| R17 | Gate-level simulation strongly recommended | CI `gl_test` | Met |
| R18 | One entry per team | one repo | Met |

## 3. Tiny Tapeout and IHP submission rules

| Rule | Where | Status |
|---|---|---|
| Tile size in `info.yaml` | `tiles: "8x4"` | Met |
| `info.yaml` fields: title, author, description, language, `clock_hz`, `top_module` (`tt_um_` prefix), `source_files`, pinout | `info.yaml` | Partial: the description and pinout are the bring-up design's, and `source_files` changes with #11 |
| Datasheet `docs/info.md`: how it works, how to test, external hardware; images < 512 KB each, < 1 MB in all | `docs/info.md` | Not yet: describes the bring-up design |
| Pins: 8 in, 8 out, 8 bidirectional, plus clock and reset; no analog pads | architecture §8 | Met in the spec; the `info.yaml` pinout follows once #11 merges |
| Pad speed (TT Discord, 11-10): ~60 MHz out, ~75 MHz in | the fastest pin toggles at clk/2 = 25 MHz at 50 MHz; host SPI and flash SCK at ≤ clk/8 | Met |
| Demo board I/O is 3.3 V; external parts go on a Pmod | architecture §8 | Open (to check: architecture §8 "Verify against the TT demo-board docs") |
| IHP SRAM macros allowed, several, if they fit and pass precheck | 2 × `RM_IHPSG13_1P_512x32` (architecture §3) | Met on the harness (#10 re-checks at 8x4) |
| Precheck clean | CI `gds.yaml` | Met on every passing gds run |
| Open-source license | `LICENSE`, Apache 2.0 | Met |

## 4. Open questions

| # | For | Question | Why |
|---|---|---|---|
| Q1 | Jane Street | Which MCU is on the demo board: RP2350 (the brief, Nov 9) or RP2040 (architecture §8, chip-spec §3)? **Not asked (team decision 11-10). Working answer: RP2350**, as the brief says. Host SPI uses PIO, which both chips have, so nothing in the design depends on it | host software; the pin check in architecture §8 |
| Q2 | Jane Street | Is a design that ships the host-driven fuzzer (our Dec 15 cut rule) judged the same way? **Not asked (team decision 11-10). Working answer: yes.** The brief has no fixed weighting and says fewer features with a strong architecture and verification can compete. If the cut happens, the writeup says so plainly, with the gate evidence | the Novel tier's fallback |
| Q3 | Tiny Tapeout | Pad pull-ups, and the pad round-trip delay on CMOS5L. **Partly answered 11-10** (TT Discord): max pad frequency ~60 MHz out, ~75 MHz in, a bit more if duty-cycle distortion is tolerable. Pull-ups and the round-trip delay are still open | host SPI at clk/8, flash sampling (architecture §6) |

Draft messages for the human to send live in the supervisor's handoff, not here.
