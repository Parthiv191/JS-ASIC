# Chip architecture

Status: v1 (updated 2026-11-10), owner: verification role (§9: devOps role). What the chip is for: `chip-spec.md`.
Instructions: `isa.md`. Why: `decisions.md`. Process: IHP CMOS5L via Tiny Tapeout, 8x4 tiles
(core ~1.2M µm², scaled from 6x4's ~902K; the first 8x4 harden gives the exact figure), 40 MHz
target. Tier tags (Core/Plus/…) as in chip-spec §4.

## 1. Blocks

```
 ui_in[2:0] host SPI ─►┌───────────┐  cfg bus  ┌────────────────────────────────────────────────┐
 uo_out[0]  MISO    ◄──│ host port │──────────►│ global regs · shared image tables · debug ·     │
 flash pins ──────────►│ boot ctrl │           │ boot ctrl · capture writer · REPORT UART       │
                       └───────────┘           └───┬──────────────┬──────────────────┬──────────┘
                ┌──────────────┐  ph=0        ┌────▼─────┐   ┌────▼─────┐      ┌──────▼─────┐
                │ SRAM col. 1  │─────────────►│ engine 0 │   │ engine 1 │      │ fuzz unit  │
                │ program 512w │──── ph=1 ────┼──────────┼──►│          │      │ (engine 0) │
                └──────────────┘  ph=1        └──┬───▲───┘   └──┬───▲───┘      └──────┬─────┘
                ┌──────────────┐                 │   │          │   │                 │
                │ SRAM col. 2  │◄── one client: fuzz unit | capture writer + REPORT | host
                └──────────────┘    pins: per-pin drive flops → wired-AND merge → uio[7:0]
```

- **2 identical engines, any role.** Engine 0 also owns the fuzz unit.
- **One shared copy of the image tables** (both engines run the same image): 8 profiles, 4
  lanes, 8 lines, CRC config, bus config, `abort_pc`.
- **Per engine:**
  - role, profile 7 (PROF-writable);
  - r0–r7, `now`, `tb`, anchors (24-bit age counters), `evt`;
  - the prefetch register and a step register;
  - TX FIFO 4 × 16, RX FIFO 8 × 16.

## 2. Clocks, reset, IO

| Rule | Why |
|---|---|
| One clock, no dividers, no gated clocks | one clock tree; simple STA |
| Reset: async assert, sync release (2 flops); every flop is reset through its own pin. IR and prefetch reset to NOP and load only while running. | no X in gate-level sim |
| Every external input has 2 synchronizer flops: host SPI, flash MISO, the straps, `uio_in` | metastability; gives `SYNC_LAT` |
| **Every output comes straight from a flop.** Each engine has one drive flop per pin (the line→pin map is applied before the flops). `uio_oe` = OR of the per-pin flops, `uio_out` = NOR of the "pull low" flops, ERR is registered. The TESTMODE mux is the only other gate. | glitch-free pins; no false edges on real devices |
| Each engine's synchronizer input is `low ? 0 : high ? 1 : (LOOPBACK ? 1 : uio_in)` | on-chip peers see each other with the model's `EDGE_LAT` |
| Until RUN (and during boot), every `uio` is an input. The one exception is GPIO override (§4), which drives the pins in any state. `uo_out` reset levels: MISO 0, IRQ 0, ERR 0, flash SCK 0, flash CS_N 1, flash MOSI 0, REPORT 1, ACT 0. | never fight a device; UART and flash idle correctly |
| **LOOPBACK** (PIN_CTRL): `uio_oe` is forced to 0, and undriven lines read 1 inside the chip | self-test with no board and no pull-ups |
| **TESTMODE** strap (ui_in[7] high at reset). With ui_in[6] = 1, `uio` outputs the registered `ui_in` (all outputs). With ui_in[6] = 0, `uo_out` shows the registered `uio_in`. | tests clock, reset and every pad with no SPI and no SRAM |
| Keep the logic in front of `uo_out` shallow (TT adds ~4 ns of external delay) | the SRAM trial build's worst setup path went to an output pin |

## 3. Memory

| Column | Macro | x, y (µm, from the 6x4 trial) | Clients |
|---|---|---|---|
| 1 | `RM_IHPSG13_1P_512x32_c2_bm_bist` | 405.92, 483.84 | engine 0 when `ph = 0`, engine 1 when `ph = 1` (`isa.md` §4.0); the host only while both are stopped or halted |
| 2 | same | 855.52, 483.84 | **one at a time** (`MEM2_MODE`): 0 = the fuzz unit · 1 = the capture writer + REPORT reader · host access when idle |

- This layout passed precheck and the GL test on 6x4 (run 41994812789). Re-check it on 8x4.
- `A_DOUT` drives only flops (both prefetch registers and the host read register), which
  respects its ≤ 64 fF limit.
- **Column 2 in capture mode** is a record FIFO. The writer appends; REPORT drains behind it in
  idle cycles, and the writer has priority. Records that don't fit are **dropped** and counted
  in `CAP_LOST`.
- Changing `MEM2_MODE` while column 2 is busy is refused (`host_err`).
- Column 2 powers up as random data. FZ_CTRL.clear (256 cycles) must finish before a RUN that
  fuzzes; a RUN during the clear sets `host_err` and is ignored.

## 4. Host interface (SPI target)

**Timing:**
- Mode 0, MSB first, SCK ≤ clk/8. MOSI and SCK are synchronized.
- MOSI is sampled on the synchronized SCK rise. MISO moves to its next bit right after that
  sample.
- The next read byte is fetched ahead, so **read-ahead never has side effects**. RXDATA pops
  only once the high byte's last bit has shifted out.
- CS high ≥ 4 clk between frames. A CS rise resets the frame; a partial program word (fewer
  than 4 bytes) is discarded.
- **Frame:** `CMD [7] read · [6] pair` (the address toggles only bit 0, for FIFO streams), then
  `ADDR[15:8]`, `ADDR[7:0]`, then data.
  - The address auto-increments, except in pair mode. A read returns 1 dummy byte first.
  - Reading the low byte of a multi-byte register snapshots it.
- Illegal accesses set the sticky `STATUS.host_err`:
  - writing the tables or column 1 while an engine runs;
  - writing column 2 while it's busy;
  - any write during boot;
  - a TXDATA write while that engine's TX FIFO is full (the word is dropped);
  - DBG_STEP or DBG_EXEC writes to an engine that isn't halted, and REG_DATA writes to a
    running engine.
- The read mux is a 2-cycle (multicycle) path. Large readable blocks use index windows, which
  keeps the mux small.

**Register map** (16-bit little-endian at even addresses; W = write-only, R = readable, W1 = write 1 to
trigger, W1C = write 1 to clear):

| Addr | Contents |
|---|---|
| **0x0000 global** | 00 ID (0x5E) · 01 VERSION · 02 SCRATCH (RW) · 03 STATUS (boot_done, boot_err, host_err, mem2_busy, testmode, loopback) · 04 RUN · 05 SOFT_RESET (W1) · 06 IRQ (W1C) · 07 ERR_CLR (W1) · 08 DBG_HALT · 09 DBG_STEP (W1) · 0A MEM2_MODE · 0B PIN_CTRL (gpio_override, loopback) · 0C PINS (R) · 0D PIN_OE (R) · 0E PIN_OUT (W) · 0F PIN_OE_SET (W) · 10 CAP_CTRL (enable, engine, Δt shift, report_en) · 12 CAP_COUNT · 14 CAP_LOST · 16 REPORT_DIV (default 434 = 115200) · 18 ERR_MASK · 1A sizes (program/64, profiles, lanes, engines) |
| **0x0400 shared tables (W, while stopped)** | 00–2F PROF[0..7] (min, nom, max LE; PROF0 ignored; PROF7 is copied into each engine's private slot at RUN) · 30–37 LINE[0..7], 1 byte each: `pin[2:0] od[3] owner[5:4] idle[6] gated[7]`; an unused slot is a released OD line, `0x68 + pin`, on a pin no line uses (the free pins in ascending order: the chip merges the slots per pin, so a slot on a line's pin would make that line OD); with no lines that is `0x68 + i`, each slot's reset value · 38–47 LANE[0..3], 4 bytes each: `{b0: data[2:0] clk[5:3] has_clk cpol · b1: cpha data2[3:1] has_d2 stretch nrzi · b2: ldelay[3:0] stuff_n[6:4] d2_comp · b3: 0}` · 4A ABORT_PC · 4C CRC width, 4D flags (bit 0 lsb_first), 4E poly, 50 init, 52 xorout · 54 BUS_KIND (0 NONE, 1 I2C, 2 SELECT, 3 SE0) · 55 BUS_AB (a[2:0], b[5:3]) · 56 BUS_CONFIRM · 57 PROF_SHIFT |
| **0x1000–0x17FF** | program SRAM, 512 words × 4 bytes, little-endian (a word commits on its 4th byte) |
| **0x2000 + 0x100·e** engine e | 00 ROLE (W) · 02 STATUS (running, halted, FIFO flags) · 03 FIFO counts · 04 PC · 06 VIOL (W1C, `isa.md` §7) · 08 EVT · 0A IRQ_ID · 0C FIRST_VIOL_PC · 0E FIRST_VIOL_KIND · 10 BREAK_PC (bit 15 = enable) · 12 REG_SEL (W) · 14 REG_DATA (r[REG_SEL]; writable while stopped or halted) · 16 TB · 1A NOW (3 bytes each) · 20 DBG_EXEC (W, 4 bytes; the 4th runs it) · 30 TXDATA (pair; pushes on the high byte) · 32 RXDATA (pair) |
| **0x3000–0x37FF** | column 2 SRAM, 512 × 4 bytes, host access when idle |
| **0x4000 fuzz unit** | 00 FZ_CTRL (W1: clear bitmap, reset) · 01 FZ_STATUS (busy, ready, token_lost) · 02 FZ_LEN · 03 FZ_MODE (wire, bitmap size) · 04 LFSR_SEED · 06 FINDINGS_COUNT · 08 MASK (4 bytes, 1 bit per byte position) · 0C FINDING[0..1] (4 bytes each) · 14 STAGE_SEL (W) · 15 STAGE_DATA · 16 SLOT_SEL (W) · 18 SLOT_META (length, energy, pinned; RW while stopped) |

**Register behaviour** where the map is silent (stopped = RUN bit 0; halted = HALT, ILLEGAL or
DBG_HALT):

| Register | Behaviour |
|---|---|
| 0x0000–0x000F | 1-byte registers |
| VERSION | 0x01 |
| STATUS | bit 0 boot_done (1 when no boot runs), 1 boot_err, 2 host_err, 3 mem2_busy, 4 testmode, 5 loopback |
| RUN | bit `e` = engine `e`. A 0 → 1 starts it (`isa.md` §4.0), also after a HALT. 1 → 0 stops it and releases its lines; its state stays readable. r0–r7 keep their values (`isa.md` §1). |
| SOFT_RESET | also clears that engine's RUN bit, so the engine is left stopped |
| IRQ | one W1C bit per engine |
| ERR_CLR | clears `STATUS.host_err` |
| ERR pin | the OR of the VIOL bits under ERR_MASK (all bits until ERR_MASK is built) |
| FIRST_VIOL_PC/KIND | re-arm when the host clears VIOL to 0 |
| engine STATUS | bit 0 running, 1 halted, 2 TX full, 3 TX empty, 4 RX full, 5 RX empty |
| FIFO counts | TX count in [2:0] (0–4), RX count in [7:4] (0–8); bit 3 reads 0 |
| RXDATA | reading an empty RX FIFO returns 0 and pops nothing |
| REG_DATA | a write commits on the high byte |
| PIN_CTRL | bit 0 gpio_override, bit 1 loopback; readable |
| GPIO override | the host's drive flops replace the engines' drives (no merge), before RUN too. They take PIN_OUT and PIN_OE_SET one cycle after the write. |
| PINS | the synchronized value the engines see (§2's formula), so LOOPBACK can be checked from the host alone |
| PIN_OE | what the chip drives, before the LOOPBACK mask |
| read-only registers | writes are ignored and don't set host_err |

An image's table and program bytes come from the toolchain: `Image.host_writes()` in
`toolchain/emu/image.py`.

## 5. Debug and trace

**Core:**
- **DBG_HALT** stops an engine at an instruction boundary; resuming refetches the PC.
- Always readable: PC, VIOL, EVT, the first violation's PC and kind, registers (through the
  REG window), `tb`, `now`.
- SCRATCH, TESTMODE, LOOPBACK, GPIO override.
- **SOFT_RESET** resets only that engine's state and FIFOs, never the host port or globals.

**Plus:**
- **DBG_STEP** runs one instruction. A blocking step may never return; SOFT_RESET recovers.
- **BREAK_PC** halts before running the instruction at that PC. It compares against the
  registered fetched PC.
- **DBG_EXEC** runs one host-written **untimed, non-branch** instruction on a halted engine
  through its prefetch register. It changes neither the PC nor the prefetch, and needs no SRAM.

**Bring-up order** (each layer checked on its own):
1. TESTMODE: clock, reset, every pad.
2. SPI ID/SCRATCH: host port.
3. GPIO override: pin drivers.
4. Program SRAM write/read.
5. One engine, a tiny program.
6. LOOPBACK, two engines.
7. External devices.

**RTL trace port** (simulation only; Core). Signals named `tr_*` inside each engine instance,
which the co-sim (`test/cosim/`) reads by hierarchy. They need no top-level pins, and synthesis
removes them because nothing loads them. All are valid in the clk cycle of their pulse.

| Signal | Bits | Meaning | Model record (isa §7) |
|---|---|---|---|
| `tr_issue` | 1 | pulse: an instruction starts (§4.0); `tr_pc` is its pc | instruction start `(pc, cycle)` |
| `tr_step` | 1 | pulse: a timed step completes | `StepRecord` |
| `tr_pc` | 9 | the pc of the instruction executing now | `pc` |
| `tr_op` | 2 | with `tr_step`: 1 COND, 2 HOLD, 3 XFER | `op` |
| `tr_do` | 1 | with `tr_step`: 1 = did the step, 0 = followed it | `mode` |
| `tr_value` | 16 | with `tr_step`: HOLD the level, XFER the wire value (duplex: packed), COND the value MEAS writes (the clamped measurement; 0xFFFF on a TIMEOUT), 0 without MEAS. With `tr_mark`: the id | `value` (none = 0) |
| `tr_now` | 24 | the engine's `now` | `cycle` |
| `tr_tb` | 24 | `tb`; with `tr_step`, its value after the step | `tb` |
| `tr_evt` | 11 | pulses, one bit per event kind code (isa §7), in the cycle the event is logged | `Event(kind, pc, cycle)` |
| `tr_mark` | 1 | pulse: MARK runs | `MARK` event |
| `tr_br`, `tr_taken` | 1 + 1 | pulse: a conditional decision (BR Z/NZ/TO/ROLE, RAW JPIN, ROLE_IF), in the instruction's start cycle; whether it left the fall-through path (ROLE_IF: skipped) | `Branch(pc, taken, cycle)` |
| `tr_sample`, `tr_line` | 1 + 3 | pulse: a follower samples a line; which line | `Sample(pc, line, cycle)` |
| `tr_sample2`, `tr_line2` | 1 + 3 | pulse: in the same cycle, a duplex engine that follows both data lines (MON) samples `data2`; which line. `tr_sample`/`tr_line` keep the first followed line | the second `Sample` of that cycle |
| `tr_halted` | 1 | level: the engine has halted (HALT or ILLEGAL) | `halt_cycle` |

- **What the co-sim compares, per engine:** every pulse with `(tr_pc, tr_now)` and its fields,
  in order, against the model's records. Times are compared modulo 2^24.
- **Not compared:** debug halt, step and exec (not cycle-exact, §4.0), and anything after the
  first mismatch.

## 6. Standalone mode (Plus)

1. **Strap.** If `BOOT` (ui_in[4]) is high when reset releases, the boot controller drives the
   flash pins at **SCK = clk/8**.
   - Each MISO bit is sampled from the synchronizer **5 clk after the SCK fall that launched
     it**. That covers the output flop, the TT pad delay, the flash's output time and 2 sync
     flops.
2. **Wake the flash.**
   - Wait 2^16 clk (~1.3 ms).
   - `0xAB` (release power-down), wait 256 clk.
   - `0x66 0x99` (reset), wait 2048 clk.
3. **Read** with `0x03 000000`.
4. **Image format:**
   - `"PE"`, version 1;
   - records `{addr_hi, addr_lo, len, len bytes}`, replayed as host writes;
   - `len = 0` ends the records;
   - a CRC-16/CCITT-FALSE trailer (big-endian) over everything from `"PE"` through the
     terminator.
5. **RUN** writes are held back until the CRC passes.
6. **Errors:**
   - `E1` bad magic, `E2` version, `E3` CRC, `E4` a record crossing the end of its register
     block or SRAM.
   - On error: print `BOOT E<n>` on REPORT, set boot_err, blink ACT and wait for a host.
   - On success: `BOOT OK`.
   - The host is locked out until boot is done.
- Any 3.3 V SPI NOR flash works (e.g. W25Q), through an adapter cable, because the signals are
  split across the `ui` and `uo` headers.

## 7. Capture and REPORT

- **The writer** builds the records of `isa.md` §7 for the engine chosen by `CAP_CTRL.engine`
  and writes them to column 2 (`MEM2_MODE` = 1). The Δt unit is a prescaler, not a barrel
  shift.
- **REPORT** (Plus) streams records as text, 8 hex digits + `\n`.
  - At 115200 that's ~1,280 records/s; REPORT_DIV goes down to 16 (~3 Mbaud).
  - The 512-record buffer absorbs bursts. 400 kHz I2C makes ~40K records/s and fills it in
    ~13 ms, so use the host for long captures.
- **In fuzz mode** capture is off. REPORT prints FINDING records and the boot result.
- Records never go into the RX FIFO.

## 8. Pins

| Pin | Use |
|---|---|
| ui_in[0..2] | host SCK, MOSI, CS_N |
| ui_in[3] | flash MISO |
| ui_in[4] | BOOT strap |
| ui_in[5] | spare |
| ui_in[6] | TESTMODE direction |
| ui_in[7] | TESTMODE strap |
| uo_out[0] | host MISO |
| uo_out[1] | IRQ |
| uo_out[2] | ERR |
| uo_out[3..5] | flash SCK, CS_N, MOSI |
| uo_out[6] | REPORT |
| uo_out[7] | ACT: engine 0's running state through one flop. It runs from `now = 1` through its HALT's start cycle, so ACT reads 1 from `now = 2` through that cycle + 1 |
| uio[7:0] | protocol pins IO0–IO7. OD lines need **external** pull-ups: 2.2–4.7 kΩ for I2C (the Basys 3's internal ones are too weak for 400 kHz), and 1.5 kΩ on D− for USB-LS. |

**Verify against the TT demo-board docs:**
- the MCU (the RP2350, as the brief says; challenge Q1) drives every pin; the DIP switches are
  on `ui_in`; `uo_out` drives a 7-segment display;
- whether the MCU sets the straps before releasing `rst_n` and leaves `uio` tristated;
- the QSPI Pmod sits on `uio`;
- 3.3 V pads, any pad pull-ups, and 40 MHz through the IHP mux.

The MCU can host (PIO SPI, which the RP2040 and RP2350 both have) and act as the protocol peer
on `uio`, which makes a self-contained bring-up rig.

## 9. Physical design plan

| Rule | Evidence |
|---|---|
| Macros: one per column, x = 91.2 + 44.96·k, never over x = 366.6–370.7 | `src/pdn_cfg.tcl` bridges run edge to edge |
| 8x4: both macros at the right end, x = 810.56 (column 1, k = 16) and 1260.16 (column 2, k = 26), y = 483.84; this supersedes §3's 6x4 positions | in the middle they walled off an 866 µm band of Metal1–4 (the only horizontal layer, Metal3, overflowed): two-engine global-route overflow 4329 → 389 at the same settings, and 0 with `GRT_ADJUSTMENT` 0.05 (local runs e2a, e2b, e2d) |
| ≥ ~20 µm between a macro and the edge its VDDARRAY! bridges run to | run 41975804926 (the power-grid check failed, error PSM-0069) |
| Macro signal pins are on the bottom edge. Logic goes below the macros; the TT pins are top-left. | LEF |
| One client per macro, with its flops next to it; `A_DOUT` drives only flops | run 41975807753 (slew/cap) |
| Buffer nets > 120 µm (`DESIGN_REPAIR_MAX_WIRE_LENGTH`) | run 41980432163 |
| No flop memories > ~32 words | the flop-memory trial: 128 words ≈ 200K µm², hitting the 2 h CI limit |
| RTL timing rules: 24-bit age counters (one compare per check); registered window thresholds; no multipliers; the abort decision registered; CRC 8 bits/cycle; a pipelined host read mux; a register at every block boundary. Estimated worst path: COND do `max(now, tb + nom)` ≈ 11–14 ns. | the Nov 5 hardware review |

**First-harden checks** (devOps). The SRAM trial had almost no logic, so these are still open:

| Check | How |
|---|---|
| **Routing congestion** from the macro power bridges | the downward VDD!/VSS! bridges take up to ~50% of Metal4 across ~65% of the 6x4 die width (re-check on 8x4). Count the bridge shapes in the DEF. If congested, end the downward bridges a few rows below the halo and re-run check_power_grid. |
| Fail on global-route overflow | done: `src/config.json` sets `GRT_ALLOW_CONGESTION = 0` (the template's 1 let overflow pass silently) |
| Placement blockages | the ~13 µm sliver between the macros, the right-edge sliver, the rows above the macros |
| **IR drop** | one VPWR/VGND pin pair at x ≈ 368 feeds the right two-thirds of the 6x4 die, and more of the wider 8x4 die. Run OpenROAD's power-grid analysis (PSM) with realistic activity. If needed, promote the full-height straps left of macro 1 to pins (verify how TT connects power). |

**Area estimate**, at the flop-memory trial's ~49 µm² per flop-bit and 15.6 µm²/cell +20%:

| Block | Area |
|---|---|
| macros | 159K |
| 2 engines (~700 state bits + ~3.5K logic cells each) | ~200K |
| shared tables | ~40K |
| fuzz unit (~800 flops + comparator tree, byte muxes, shifter) | ~95K |
| host + regs + boot + capture + REPORT + debug | ~55K |
| **total** | **~550K (~46% of 8x4; was ~61% of 6x4)** |

8x4 puts it under the 60% target, but the estimate is rough, so the first engine synthesis is still
the gate. Levers, in order, if it comes in over:
1. CODEC out.
2. PROF out.
3. 16 fuzz slots.
4. Fewer readable registers.
5. 2 lanes instead of 4 (this limits JTAG).

## 10. Open

| Item | Decide by |
|---|---|
| Engine area: the timed engine is ≈ 220K µm² after place and route (#14), ~2× §9's estimate, projecting ~65% of the core against the 60% target. Scope cuts wait for devOps's engine area pass. | engine 1's harden (devOps) |
| Congestion and IR drop with full logic | first full harden |
| The macro positions and §9 rules, which come from 6x4 | first 8x4 harden (devOps) |
| TT demo-board facts | §8 |
| Sign-off clock: **40 MHz, decided 11-11.** One engine missed 50 MHz by 2.76 ns at the slow corner (#36); two engines with FIFOs pass 40 MHz with ~0.5 ns to spare. The clock comes from the board, so this changes no silicon and no cycle counts; protocol rates scale by 0.8 (chip-spec §5). | decided |
