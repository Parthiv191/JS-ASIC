# Decision log

One line per decision, newest last. When a decision is revised, the old row stays and the new row
says "revises". "Claude (delegated)" = Parthiv asked Claude to decide, on evidence.

| Date | Decision | Why | Who | Spec |
|---|---|---|---|---|
| 10-26 | TT IHP template, 6x4 tiles, top `tt_um_prot_emu_SAP`, 50 MHz target | challenge rules; template default | Parthiv | info.yaml |
| 11-02 | Build for IHP CMOS5L, not sg13g2 | the challenge requires CMOS5L | Manthan | plan |
| 11-03 | Role-switching design over the PIO-style draft | more novel, drift-free deadline timing, the monitor helps verification | Parthiv | isa |
| 11-03 | Novel feature: role switching + learning bus fuzzer | the combination nobody has | Parthiv | bus-fuzzer |
| 11-03 | I2C START/STOP as COND kinds, detected as SDA changing while SCL is high | v0 couldn't tell START/STOP from data (F1) | Parthiv | isa §4.2, §4.7 |
| 11-03 | OD data lines let go after the next leading clock edge | fake ACK / stuck bus (F2) | Parthiv | isa §4.3 |
| 11-03 | Clock stretching both ways: the owner waits for high; an unready target holds low | real sensors/EEPROMs stretch (F3) | Parthiv | isa §4.3 |
| 11-03 | Early end: the target assumes more data and exits on START/STOP (sticky flags + a "why" read) | same pattern as MCU I2C hardware | Parthiv | isa §4.1, §4.7 |
| 11-03 | MEASURE level 1 + level 2 (autobaud); level 2 first to cut | headline feature; level 1 almost free | Parthiv | isa §4.2, §4.8 |
| 11-03 | RX full = drop + OVERFLOW; LT unsigned; shift `b & 15`; 16-bit LI | UART practice / RV32I / free fix | Parthiv | isa §3, §4.9 |
| 11-04 | D2: `T2-window` at an anchor stays as written | slack would allow false alarms | Manthan | isa §6 |
| 11-04 | Purpose: hardware debugging + reverse engineering, one protocol at a time | focus | Parthiv | chip-spec §1 |
| 11-04 | Works standalone: boots from flash; the learning fuzzer with corpus distillation is on-chip | the fuzzer is the point; no PC needed | Parthiv | chip-spec §3 |
| 11-04 | Scope: UART/SPI/I2C + MEASURE first; PS/2, JTAG, SWD etc. later | finish the core first | Parthiv | chip-spec §4 |
| 11-04 | Low-speed USB is a stretch goal; check feasibility, sniffer first | fits debugging/RE | Parthiv | chip-spec §4, isa §4.10 |
| 11-04 | SRAM for every big memory | ~10× denser than flops (E1F) | Parthiv | architecture §3 |
| 11-04 | Clock not frozen: 50 MHz target, 40 MHz fallback | programs count cycles | Parthiv | chip-spec §8 |
| 11-04 | No program checking on the chip at load | the compiler checks; the chip has timeouts | Parthiv | chip-spec §6 |
| 11-04 | Ask Jane Street whether 8x4 tiles are allowed | more SRAM columns | Parthiv (action) | — |
| 11-04 | 8 registers (r0–r7), 16-bit | I2C, MEASURE and the fuzzer compete for 4; +64 flops per engine | Parthiv | isa §1, §3 |
| 11-05 | FIFOs TX 4, RX 8 (flops); column 2 doubles as a deep capture buffer when not fuzzing | the host can't drain SPI bursts; fuzzing and capture never run together | Parthiv | isa §1, architecture §3 |
| 11-05 | Per engine: 8 profiles, 4 lanes, 8 lines; the ISA fields stay 4-bit | ~580 flops saved per engine; programs need ≤ 8/4/8 | Parthiv | isa §1 |
| 11-05 | I2C details: OD readback (dst = wire value), lead blocks, O1–O3 rule fixes, `bus_confirm` register | real ACKs/arbitration; host-decided write length | Parthiv | isa §4.3, §4.7, §5, §6 |
| 11-05 | Two instruction shapes: T (owner, line/lane, prof) and U (rd [26:24], ra/line [23:21], sub) | fits 8 registers + 16-bit LI; fixed register slots keep decode fast | Parthiv | isa §3 |
| 11-05 | Capture log: 32-bit records {type, Δt, value} written by the engine | timing is what reverse engineering needs | Parthiv | isa §7, architecture §7 |
| 11-05 | Fuzzer: hang inputs saved and pinned; 32 slots + 8K-bit bitmap; both length modes; findings as records | standalone keeps findings; fingerprints lost ≈ n/2m (0.6% at n = 100); flop cost scales with slots | Claude (delegated) | bus-fuzzer §2–3 |
| 11-05 | CHOICE, ARB, WAIT_ANY reserved, no v1 hardware; CODEC fields defined now, built only if feasible | reserving is free; avoids a re-encode | Claude (delegated) | isa §3, §4.10 |
| 11-05 | Host interface: SPI target (mode 0, ≤ clk/8) | the RP2040 and the Basys 3 bridge both do SPI; fast enough to drain capture | Claude (delegated) | architecture §4 |
| 11-05 | Silicon debug: halt, step, registers; safe reset (all pins inputs until RUN); boot flash on ui/uo; REPORT 115200 text | the pins are the only window after tapeout; never fight a device; any terminal reads it | Claude (delegated) | architecture §2, §5–8 |
| 11-05 | Shared program SRAM, interleaved: engine 0 on even cycles, engine 1 on odd | flops are 10× worse per bit; only 2 wide columns fit; frees column 2 | Parthiv | isa §4.0, architecture §3 |
| 11-05 | Memory: 2 × RM_IHPSG13_1P_512x32 (col 1 program 512 words, col 2 fuzzer/capture); 2 engines | E1 passed precheck and the GL test (run 41994812789); 1024×32 → ~78% utilization | Parthiv | architecture §3 |
| 11-05 | Fetch rule: instructions start only on their engine's parity; a 1-word prefetch; untimed (incl. taken branches) = U; ISSUE ≤ 2; abort 3–4 | deterministic and cheap; the SRAM output feeds only flops; the review showed the "prefetch whatever the parity" wording was impossible | Claude (delegated) | isa §4.0 |
| 11-05 | (superseded below) `CLKDIV ≥ 1` ticks only on own cycles | a fixed even/odd schedule otherwise starves an engine | Claude (delegated) | — |
| 11-05 | Bus detector generalized: `bus_kind` I2C / SELECT (SPI CS) / SE0 (USB, [P]) | SPI (a must) couldn't see CS; one mechanism covers all three | Claude (delegated) | isa §4.7 |
| 11-05 | **Revises 11-03:** cut target-side clock stretching. A target never stalls: an empty FIFO sends ONES + UNDERRUN; a full one drops + OVERFLOW. | glitch-prone (the chip sees SCL 3 cycles late), the hardest feature to verify, low value (the fuzzer uses staging, the host can pre-fill) | Claude (delegated) | isa §4.3, §4.9 |
| 11-05 | Per-lane `stretch` bit and `ldelay` (data launch/release delay; I2C ~300 ns) | stretch isn't I2C-only; real devices need SDA hold after SCL falls | Claude (delegated) | isa §2, §4.3 |
| 11-05 | R2 applies only outside role blocks; any role block may exit to `abort_pc`; T5-abort accepts a COND the role does | the review showed lead blocks and target address-mismatch exits were otherwise unwritable | Claude (delegated) | isa §5, §6 |
| 11-05 | 24-bit time with a clamp rule; CRC 8 bits/cycle; the fuzz fingerprint has its own folder + token queue | wrap safety; 16 serial CRC stages miss 20 ns; avoids a shared-CRC collision | Claude (delegated) | isa §4.1, §4.4, bus-fuzzer §2 |
| 11-05 | Debug bundle: SCRATCH, TESTMODE strap, GPIO override, LOOPBACK, DBG_EXEC, breakpoint, first-violation PC, trace port | each layer can be brought up alone, even with a dead SRAM or SPI | Claude (delegated) | architecture §2, §5 |
| 11-05 | Config tables write-only to the host; even-aligned registers; snapshots; sticky host_err | saves ~20–30K µm² of readback mux; no torn reads; debuggable failures | Claude (delegated) | architecture §4 |
| 11-05 | Boot image: CRC-16 trailer, RUN held back until it passes, flash wake sequence, BOOT OK/E<n> on REPORT | one bad bit could otherwise run garbage, with no visible error | Claude (delegated) | architecture §6 |
| 11-05 | Scope tiers + cut order (Core / Novel / Plus / Stretch / Later) | ~10 weeks left, RTL not started; Core must ship | Claude (delegated) | chip-spec §4 |
| 11-05 | **Round-2 review fixes** (3 reviewers): | | | |
| 11-05 | **Revises the CLKDIV row:** no clock divider in v1 (Later tier). U = 2 always. | Core doesn't need it (UART goes down to 794 baud); it doubled every timing case to verify | Claude (delegated) | isa §1, §4.0 |
| 11-05 | **Revises the 11-05 per-engine tables row:** one **shared** set of image tables; per engine only the role, profile 7 (PROF-writable) and state | both engines run one image; saves ~30K µm² (the area review put the total at 71–78% without it) | Claude (delegated) | isa §2, architecture §1, §4 |
| 11-05 | **Bus exits are normal**: they log a BUS record only, with no ABORT/VIOL and no fuzzer hang | otherwise every I2C/SPI frame end lit ERR and filled the corpus with false hangs | Claude (delegated) | isa §4.1, §4.7, bus-fuzzer §3 |
| 11-05 | Add `BR ROLE` (a branch on the role mask) | lets followers loop with one branch; a 4 MHz mode-3 SPI target becomes feasible | Claude (delegated) | isa §3 |
| 11-05 | Timing rules made exact: HOLD `tb := tb + nom`; stall re-base + `T5-stall`; T5-watch formula; T5-overrun for planned > 0; OD owners use the follow offsets; abort offset; start-up (a free-running phase bit, `now = 0` on the first own cycle, engine 1 one clk behind) | the model and RTL would otherwise differ by 1–2 cycles | Claude (delegated) | isa §4.0–4.3, §6 |
| 11-05 | Failure table per kind; ILLEGAL kind; HALT end state; ERR = VIOL & mask; `gated` PP lines (SPI MISO); duplex pushes the packed value | undefined behaviour removed; shared SPI buses | Claude (delegated) | isa §2, §4.1, §4.3, §7 |
| 11-05 | Fuzzer: a per-iteration event mask instead of event tokens; a normative LFSR draw sequence; 32-bit mask; 2-entry findings log; length 0 = empty, 0x00 past a slot's length; clear before RUN | determinism; no garbage SRAM on the wire; ~25K µm² saved | Claude (delegated) | bus-fuzzer §2–3 |
| 11-05 | Host: read-ahead without side effects, index windows, tables written only while stopped, multicycle read mux. Flash at clk/8, sampled 5 clk after the fall. All outputs from flops; TESTMODE covers `uio`. | lost RX words; a smaller read mux; flash round-trip delay; glitch-free pins; pad test with no SPI | Claude (delegated) | architecture §2, §4, §6 |
| 11-05 | Tiers: CRC and DBG_STEP/EXEC/BREAK → Plus; autobaud is Plus; **the fuzz-unit RTL starts only if Core passes on the FPGA by Dec 15** | schedule risk is high (the model is still v0, RTL not started) | Claude (delegated) | chip-spec §4 |
| 11-05 | First-harden checks: power-bridge congestion, fail on GRT overflow, IR drop | E1 proved placement, not routability with full logic | Claude (delegated) | architecture §9 |
