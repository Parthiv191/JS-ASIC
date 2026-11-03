# ISA v0: cycle-level contract

Status: v0 r2 (2026-11-02). This is the contract between the description compiler, the
Python reference model (`toolchain/emu/model`), the fuzzers, and the engine RTL (Phase 5+).
Sources: `role_switching_protocol_emulator_proposal.md` §5, §8, §9, and the team ISA draft
`protocol_emulator_ISA(1).xlsx` (mapped in §8). Where the RTL and the model disagree, this
document decides. Where it is silent, the reference model decides and this document gets
updated.

r2 changes: program depth 64 (team draft), `CRC` and `FZ` opcodes, the `IRQ` flag on `MARK`,
branch on timeout (`BR TO`), duplex lanes, the `src = FZ` source, and §8.

r3 notes (2026-11-03), no encoding change:
- The SRAM program memory and its fetch timing (§1). The §4 costs are unchanged.
- R3-fz requires a block for a single party (§5).
- An open item on `T2-window` at an anchor (§6).

Implementation status (toolchain, 2026-11-03). The encodings of every r2 feature exist in
`emu.isa`; the DSL, compiler and model reach them in these tasks of the plan:

| Feature | Lands in |
|---|---|
| `COND`, `HOLD`, unclocked `XFER`, `BR TO`. The R2-branch rule for `BR TO` is already in, and the model raises on `BR TO` until Task 7. | Task 7 |
| Clocked and duplex lanes (`data2`) | Task 8 |
| `CRC`, `MARK` with `IRQ` | Task 9 |
| `FZ`, `src = FZ` | Task 20 |

## 1. Constants

| Name | Value | Meaning |
|---|---|---|
| `SYNC_LAT` | 2 | Two-flop input synchronizer: at engine cycle `c` an engine sees the wire as it was at its cycle `c-2`. |
| `EDGE_LAT` | 3 | `SYNC_LAT + 1`. An owner that drives a line in its cycle `t` changes the wire from cycle `t+1`; an equal-clock follower first sees it at cycle `t + EDGE_LAT`. |
| `QUANT` | 1 | Measurement uncertainty (cycles) allowed on every window check. |
| Lines | 8 | Logical lines `L0..L7` per engine. |
| Registers | 4 × 16 bit | `r0..r3`. |
| Profiles | 16 | Timing-table entries `(min, nom, max)`, 16-bit cycle counts. Entry 0 is `IDLE` = unbounded. |
| Lanes | 16 | Lane table entries for `XFER`. |
| `PROG_DEPTH` | 64 words | Program memory per image (team draft). Branch targets stay 12-bit, so a deeper (SRAM) program memory needs no re-encode. The compiler takes the depth as a parameter. An engine on the SRAM program memory (below) has 256 words. |
| CRC | 16-bit state | One CRC unit per engine, configured in the image header (§4.4). |
| FIFOs | 4 × 16 bit (RTL) | TX (host → engine) and RX (engine → host). The model's FIFOs are unbounded; Phase 5 checks depth. |
| `INF` | `0xFFFF` | A profile `max` of `INF` means no timeout. |

**SRAM program memory.** Two `RM_IHPSG13_1P_256x16` macros give 256 words × 32 bits
(`src/prog_mem.v`; CI run 41584775180 passed harden, precheck and gate-level tests on
CMOS5L). A read returns its word one cycle after the address. The engine hides that latency
by **fetch-ahead**: in the cycle it executes an instruction, it already presents the next pc.
That next pc is always known by then:
- Branch targets, `ROLE_IF` skip counts and `abort_pc` are immediates or header fields.
- Branch conditions read registers, the `to` flag or synchronized pins, which are already
  valid in that cycle.
- A stalled instruction holds its address, and the macro holds its output.

The issue slot of §4 already keeps the next instruction from starting in the cycle its
predecessor completed. So every cost in §4 holds unchanged. The CHASE test in `test/test.py`
drives the macro's address from its own output every cycle, which is this path, and it meets
timing at 20 ns.

## 2. Roles, owners, lines

- Role register: `P0 = 0`, `P1 = 1`, `MON = 2`. A description names two parties; party 0 is `P0`, party 1 is `P1`.
- Step owner field: `P0 = 0`, `P1 = 1`, `SHARED = 2` (open-drain only, reserved for ARB), `NONE = 3` (untimed/raw).
- An engine **does** a step iff `role != MON and (owner == role or owner == SHARED)`; otherwise it **follows** it.
- Line config (image header): `kind ∈ {PP, OD}`, `owner ∈ {P0, P1}` (OD may also be `SHARED`), `idle ∈ {0, 1}`.
  - At reset an engine drives each PP line whose owner equals its role to `idle`; every other line is released (`Z`).
  - Undriven lines read `1` (pull-up).
  - `drive(L, v)`: PP sets the output to `v`. OD pulls low for `v = 0` and releases for `v = 1`.
  - Two drivers on a PP line is **contention**. The compiler's rules make it impossible; the model reports it if it happens anyway.
- Lane config: `(data line, clock line or none, cpol, cpha, data2 line or none)`. The **clock owner** of a lane is the owner of its clock line. `data2` makes the lane **duplex** (§4.3).

## 3. Instruction word

`[31:27] op · [26:25] owner · [24:21] pin · [20:17] prof · [16:5] operand(12) · [4:0] flags`

Flags: `CHK = 1<<0` (log window violations), `RESYNC = 1<<1` (re-anchor on in-window edges), `ABORT = 1<<2` (jump to the abort vector on a fatal follow failure), `REG = 1<<3` (ALU second operand is a register), `IRQ = 1<<4` (MARK raises the host interrupt).

| op | code | owner | pin | prof | operand | flags |
|---|---|---|---|---|---|---|
| `NOP` | 0 | – | – | – | – | – |
| `COND` | 1 | step owner | line | profile | `[1:0]` kind: FALL=0, RISE=1 (START=2, STOP=3 reserved for Phase 4) | ABORT |
| `HOLD` | 2 | step owner | line | profile | `[0]` level | ABORT |
| `XFER` | 3 | step owner | lane | profile | `[3:0]` n−1 · `[6:4]` src · `[9:7]` dst · `[10]` msb_first | CHK, RESYNC, ABORT |
| `LI` | 4 | – | `[1:0]` rd | – | imm12, zero-extended | – |
| `ALU` | 5 | – | `[1:0]` rd · `[3:2]` ra | aluop | imm12, or `[1:0]` rb with REG | REG |
| `BR` | 6 | – | `[1:0]` ra | `[1:0]` cond: ALWAYS=0, Z=1, NZ=2, TO=3 | target | – |
| `ROLE_IF` | 7 | – | – | – | `[2:0]` role mask (bit0 P0, bit1 P1, bit2 MON) · `[11:3]` count | – |
| `PUSH` | 8 | – | `[1:0]` ra | – | – | – |
| `POP` | 9 | – | `[1:0]` rd | – | – | – |
| `MARK` | 10 | – | – | – | id | IRQ |
| `RAW` | 11 | – | line | rawop: SET=0, REL=1, GET=2, DELAY=3, JPIN=4 | SET: `[0]` level · GET: `[1:0]` rd · DELAY: n · JPIN: `[11]` level, `[10:0]` target | – |
| `HALT` | 12 | – | – | – | – | – |
| `CRC` | 13 | – | `[1:0]` ra / rd | crcop: RESET=0, FEED=1, GET=2 | FEED: `[3:0]` n−1 | – |
| `FZ` | 14 | – | `[1:0]` ra / rd | fzop: NEXT=0, TOK=1, END=2 | – | – |

Reserved opcodes: `CHOICE = 16`, `ARB = 17`, `WAIT_ANY = 18`, `CODEC = 19`. Codes 15 and 20–31 are free.

`src`: ZERO=0, ONES=1, FIFO=2, FZ=3 (next staged fuzz byte, §4.6), r0..r3 = 4..7. `dst`: DISCARD=0, FIFO=2, r0..r3 = 4..7 (1 and 3 reserved).
`aluop`: ADD=0, SUB=1, AND=2, OR=3, XOR=4, SHL=5, SHR=6, EQ=7, NE=8, LT=9 (comparisons write 0/1). Results wrap to 16 bits.

## 4. Execution and time

- `now` counts engine cycles from reset; it advances only on cycles where the engine's clock enable is high.
- Untimed instructions (`NOP LI ALU BR ROLE_IF PUSH POP MARK RAW HALT CRC`) take exactly one cycle. `POP` with an empty FIFO stalls until a word arrives, then takes its cycle. `RAW DELAY n` takes `n` cycles (`n ≥ 1`). `ROLE_IF` takes one cycle and, if the role is not in the mask, skips the next `count` instructions, which then take no time. `FZ` costs are fixed per sub-op (§4.6).
- The engine samples its synchronized inputs every cycle, whatever it is executing. `edge(L)` means the synchronized value of `L` differs from the previous cycle's. Edges that happen while an untimed instruction is running are not latched.
- **Timeline register `tb`.** Timed steps (`COND HOLD XFER`) are scheduled against `tb`, not against instruction count, so untimed instructions between steps do not move wire timing. An owner acts at the planned time. A follower keeps `tb` as its estimate of the owner's timeline and re-anchors it whenever it observes an owner event: `tb := detect_cycle − EDGE_LAT`.
- **Issue slot and overrun.** An instruction cannot start in the cycle its predecessor completed, so a timed step may begin one cycle after its planned start (`tb`) and an action planned *at the step's start* (offset 0) then happens in that cycle. The timeline does not move: later actions stay at their planned times and `tb` advances from the planned start. Any other action that happens after its planned cycle logs `OVERRUN(now − planned)` and is performed immediately. `IDLE` steps never overrun. A follower that reaches a planned sample point late also logs `OVERRUN`.
- **Window accumulation.** Each follower keeps an anchor (cycle of the last re-anchoring event) and the accumulated `(min, nom, max)` of every HOLD/XFER bit since then. An edge check compares `e − anchor` against the accumulated `[min − QUANT, max + QUANT]`.
- **Timeout flag `to`.** Every timed step clears `to` when it starts and sets it if it logs `TIMEOUT` and has no ABORT. `BR TO` jumps iff `to` is set. An owner never times out, so `to` differs between roles and `BR TO` is legal only inside a role block (R2-branch).
- **Abort.** A fatal follow failure on a step with `ABORT` logs `ABORT(pc)`, releases OD lines, sets `tb := now`, and jumps to the image's `abort_pc`.

### 4.1 `COND kind, line, owner, prof`

- **Do:** act at `max(now, tb + nom)` (overrun check if bounded). Drive the line to the post-edge level (FALL→0, RISE→1). `tb :=` action cycle. Completes in the action cycle.
- **Follow:** watch for the matching edge. On detection at cycle `c`: `e = c − EDGE_LAT`, `m = e − tb`. If `min > 0` and `m < min − QUANT`, log `EARLY(m)`. `tb := e`, re-anchor, complete. If `max < INF` and `now − EDGE_LAT > tb + max + QUANT` with no edge yet, log `TIMEOUT`; with ABORT, abort; otherwise `tb := tb + nom` and complete. `IDLE` (profile 0) waits forever.

### 4.2 `HOLD line, level, owner, prof`

- **Do:** at `max(now, tb)` drive the line to `level`. `tb := start + nom`. Completes in that cycle, so later untimed instructions overlap the hold.
- **Follow:** sample at cycle `tb + nom//2 + EDGE_LAT`, the midpoint of the owner's slot. A mismatch logs `LEVEL(sampled)` and aborts if ABORT is set. `tb := tb + nom`. Completes in the sample cycle.

### 4.3 `XFER n, lane, owner, src, dst, msb_first, prof`

Every role writes the **wire value** of the transfer into a register `dst`, so all roles branch on the same value. A `dst = FIFO` push happens only in follow mode, so the host receives what the engine followed and gets no echo of what it sent. The owner takes `src` at step start, and a `FIFO` source stalls while empty. Bit `k` is `value >> k` (LSB first) or `value >> (n−1−k)` (MSB first).

**Unclocked lane** (bit period `P = nom`):
- **Do:** drive bit `k` at `max(now, tb + k·P)`. `tb := tb + n·P`. Completes at the last bit's drive. The value is `src` masked to `n` bits.
- **Follow:** sample bit `k` at `a + (k − ak)·P + P//2 + EDGE_LAT`, where `(a, ak)` is the anchor (initially `(tb, 0)`). An edge seen before sample `k` sits at boundary `k`. With CHK, an out-of-window edge logs `EDGE(m)`. With RESYNC, an in-window edge re-anchors `(a, ak) := (e, k)`. `tb := a + (n − ak)·P`. Completes at the last sample.

**Clocked lane** (half period `H = nom`): bit `k` has a leading edge at `tb + (2k+1)H` (clock → `1−cpol`) and a trailing edge at `tb + (2k+2)H` (clock → `cpol`). `cpha = 0` captures on the leading edge and launches bit `k+1` on the trailing edge (bit 0 at step start). `cpha = 1` launches on the leading edge and captures on the trailing edge.
- **Clock owner** (role = clock owner, not MON): generates the edges at the planned times. `tb := tb + 2nH`.
- **Data owner = clock owner:** launches at the planned times.
- **Data owner ≠ clock owner:** with `cpha = 0`, drives bit 0 at instruction start; every later launch happens in the cycle its launch edge is detected.
- **Data follower = clock owner:** samples bit `k` at `capture_k + EDGE_LAT`.
- **Data follower ≠ clock owner:** samples the data line in the cycle the capture edge is detected.
- **Clock followers** complete when the `n`-th trailing edge is detected, then set `tb := detect − EDGE_LAT` and re-anchor. With CHK, a clock phase shorter than `min − QUANT` logs `CLK_PHASE(m)`. If `max < INF`, an edge missing for `max + QUANT` cycles logs `TIMEOUT`, and with ABORT the engine aborts.
- **The clock owner** completes at the later of its last trailing edge and its last sample.

**Duplex lane** (clocked lane with `data2`, e.g. SPI MOSI + MISO). Both data lines launch and capture on the same clock edges, and each follows the data rules above with its own line owner: an engine drives `data` iff its role owns `data`, drives `data2` iff its role owns `data2`, and follows every data line it does not drive. The step's owner field is the owner of `data` (R1). The engine drives `src` on whichever line it owns. Every role writes `dst := (v2 << n) | v1`, where `v1` and `v2` are the wire values of `data` and `data2`, so `n ≤ 8`. In follow mode a `dst = FIFO` push sends the value of the followed line: `v1` or `v2` for a party, and the packed value for `MON`.

### 4.4 `CRC`

The image header holds `crc = (width 1..16, poly, init, xorout, lsb_first)`, with `poly`, `init` and `xorout` written as in the usual CRC catalogue (normal, MSB-first form). The default is CRC-16/CCITT-FALSE (`16, 0x1021, 0xFFFF, 0x0000, false`). `lsb_first` is the catalogue's `refin = refout = true`; mixed reflection is not supported.
- `RESET`: state := `init` (its `width`-bit reversal if `lsb_first`). The state also resets this way at engine reset.
- `FEED ra, n`: shift the low `n` bits of `ra` into the state. Without `lsb_first`: bit `n−1` first, shifting left with `poly`. With `lsb_first`: bit 0 first, shifting right with the bit-reversed `poly`. One cycle for any `n`.
- `GET rd`: `rd := state ^ xorout`.

Check values for "123456789" fed as eight `n = 8` bytes: CRC-16/CCITT-FALSE `0x29B1`; CRC-16/MODBUS (`16, 0x8005, 0xFFFF, 0, true`) `0x4B37`; CRC-5/USB (`5, 0x05, 0x1F, 0x1F, true`) `0x19`.

### 4.5 `MARK id` with `IRQ`

Without IRQ, a `MARK` logs `MARK(id)`. With IRQ, it logs `IRQ(id)` instead, latches `id` in the host-readable IRQ status, and asserts the engine's IRQ output until the host clears it. The model has no host clear; it records the event.

### 4.6 `FZ` and `src = FZ`

The engine side of the learning bus fuzzer. The fuzz unit, the sub-op semantics, the auto-tokens and the fixed cycle costs (`FZ_NEXT_CYCLES`, `FZ_END_CYCLES`, checked against the SRAM macro in bus-fuzzer.md §3.1) are specified in `bus-fuzzer.md`. `FZ` is legal only inside a role block for a single party (R3-fz). There is one fuzz unit, attached to one engine; an `FZ` on an engine without it is an error. An `XFER` with `src = FZ` drives the next staged byte (0 once the stage is exhausted). It is legal anywhere, because only the owner reads `src`.

## 5. Well-formedness rules (compiler)

| Rule | Statement |
|---|---|
| `R1-owner` | Every timed step has an owner that is a party, or `shared`. A step that drives a PP line (COND/HOLD line, XFER data line) must be owned by that line's owner. A lane's clock line must have a party owner. A duplex lane's `data2` line must have a party owner different from `data`'s. |
| `R1-shared` | `shared` is only legal on OD lines. |
| `R2-branch` | `BR Z/NZ` may only test registers whose value is the same in every role ("clean"): written by `XFER dst`, `LI`, `ALU` over clean inputs, or `CRC GET` from a clean CRC state, outside role blocks. `POP`, `RAW GET`, and every write inside a role block taint the register. The CRC state is tracked like a fifth register: `FEED` of a tainted register, or any `CRC` op inside a role block, taints it. Taint is a forward dataflow over the CFG: tainted on any incoming path means tainted. `RAW JPIN` and `BR TO` are only legal inside a role block. |
| `R3-timed-in-role` | Role blocks (`only(...)`) contain no timed steps. |
| `R3-raw-drive` | `RAW SET/REL` on a PP line only inside a role block whose mask ⊆ {line owner}. |
| `R3-escape` | No branch leaves a role block or enters one from outside. A branch inside a block may target the block's `ROLE_IF`, its body, or its exit (the pc after the block, where every role resumes; when blocks are adjacent this is the next block's `ROLE_IF`, so that role check still runs). |
| `R3-fz` | `FZ` only inside a role block whose mask is exactly one party, and the same party for every `FZ` in the description. There is one fuzz unit, and its SRAM does one access per cycle. |
| `S-*` | Structure: labels resolve, names exist, field ranges, table sizes, no nested role blocks, program length ≤ the target's program depth (`S-range`), `data2` only on a clocked lane, duplex `n ≤ 8`, CRC config in range. |

## 6. Timing-compatibility rules (compiler)

`d = tolerance_ppm / 1e6` is the largest relative clock-rate difference between any two parties. `A` is the nominal number of cycles since the last guaranteed anchor, the maximum over all CFG paths. Anchors are a followed `COND` and a clocked `XFER`. HOLD and unclocked XFER add their duration. If `A` can grow without bound around a loop, the step fails `T1-unanchored`.

| Rule | Check |
|---|---|
| `T1-sample` | For every follower sample of slot `k` (HOLD: `k = 0`, `P = nom`; unclocked XFER: last bit): `ceil(d·(A + (k+1)·P)) + QUANT + 1 < P // 2`. |
| `T2-window` | For a bounded COND, and for the accumulated edge windows of `CHK` steps: `min ≤ nom − ceil(d·(A + nom)) − QUANT` and (`max = INF` or) `max ≥ nom + ceil(d·(A + nom)) + QUANT`. |
| `T3-latency` | Clocked XFER, clock followers: `H − ceil(d·H) ≥ 2`. If a data line's owner is not the clock owner (including a duplex `data2`): `H ≥ EDGE_LAT + 1 + ceil(d·2H)`. For bit 0 with `cpha = 0`: `H ≥ L + 1 + ceil(d·(A + H))`, where `L` is the cycle (relative to the step's `tb`) at which that data owner starts the step: its previous-step completion offset + 1 + the untimed cycles it runs in between. |
| `T4-profile` | `0 ≤ min ≤ nom ≤ max ≤ INF`. `nom ≥ 1` for HOLD and XFER. `H ≥ 2`. |
| `T5-watch` | A follower must already be executing a step when its first observation happens: the owner's event for COND (`start_f + ceil(d·(A+nom)) ≤ event + EDGE_LAT`), the first edge of a clocked XFER (`≤ H + EDGE_LAT`), or the first sample of HOLD / unclocked XFER (`≤ nom//2 + EDGE_LAT`). `start_r` is computed per role as the previous timed step's completion offset for that role + 1 + that role's untimed cycles in between (max over paths; an untimed loop makes it unbounded). |
| `T5-overrun` | The owner of a bounded step must start in time: `start_o ≤ max(planned_offset, 1)`, where the planned offset is `nom` for COND and 0 for HOLD and XFER. |

**Open item (plan decision D2, needed for Task 7).** Take a `CHK` unclocked `XFER` that starts right at an anchor (`A = 0`). Its boundary-0 window is `[0, 0]`, and `T2-window` asks it to contain `0 ± QUANT`. Since `0 > −1`, the step is rejected. This follows the rule as written; it is not an analyzer bug. Two ways to change it:
- Give the window at an anchor `±QUANT` slack in T2. The engine's check already widens every window by `QUANT` (§4).
- Skip boundary 0 when `A = 0`.

Until D2 is decided, such a step does not compile.

Untimed cycles count each instruction's cost from §4: one per untimed instruction, `n` for `RAW DELAY n`, and the fixed `FZ` costs.

Completion offsets, relative to the step's end `tb` (used by T3/T5): COND do 0, follow `E`; HOLD do `−nom`, follow `−nom + nom//2 + E`; unclocked XFER do `−P`, follow `−P + P//2 + E`; clocked XFER: clock follower `E`, clock owner that owns the data 0, clock owner that follows the data `max(0, E − H)` for cpha 0 and `E` for cpha 1. A duplex clock owner always follows one data line, so its offset is the follow value. Reset acts as a predecessor completing at −1 with `A = 0`.

The rules are conservative static bounds. **The fuzz loop is the arbiter:** any accepted program that the model runs, in tolerance, into a false alarm, a data mismatch or a hang is a compiler-rule bug or a model bug. Either way it becomes a regression case.

## 7. Model-only observability

The reference model also emits, per engine: a `StepRecord(pc, op, mode, value, tick)` for every completed timed step, every violation/MARK/IRQ/ABORT event, every follower sample point (used for margin coverage), and every conditional branch outcome. Phase 5 decides which of these the RTL event log exposes. The co-simulation compares at step boundaries.

## 8. Mapping from the team ISA draft

The team draft (`protocol_emulator_ISA(1).xlsx`) is a PIO-style ISA: 16-bit words, explicit
delay/side-set bits, X/Y/OSR/ISR. This ISA keeps the draft's requirements but meets them with
roles and deadline timing (`tb`). Cells marked **gap** are not met in v0; the last column says
where they land.

### 8.1 What each protocol needs (draft "protocols" sheet)

| Protocol | Draft instruction(s) | Role ISA | Status |
|---|---|---|---|
| UART send | pull · set pins · out pins 1 · set X / ldi · jmp X-- · delay | `HOLD` start, `XFER 8 src=FIFO` (whole byte, per-bit timing from the profile), `HOLD` stop. No bit counter or delay loop. | v0 (Task 10) |
| UART receive | wait pin · in pins 1 · push · jmp pin | The same description in the other role: `COND fall` waits, `XFER dst=FIFO` samples mid-bit, `HOLD` stop with `ABORT` catches framing errors. | v0 (Task 10) |
| SPI | side-set clock · out + in in one loop · wait pin · auto push/pull · set pins | Clocked duplex lane: one `XFER` per word, the clock owner generates SCK, both directions move at once (§4.3). `HOLD` on CS. `COND` on CS in the peripheral role. | v0 (Tasks 8, 10) |
| I2C | set pin directions (OD) · read pins back · wait on clock pin (stretching) · ldi + jmp X!=Y · irq · wait with timeout | OD lines; followed steps read the real line; `LI` + `ALU NE` + `BR NZ` match the address; `MARK IRQ`; bounded `COND` + `ABORT`/`BR TO`. START/STOP and clock stretching are a **gap**. | Phase 4 |

### 8.2 Instructions (draft "instructions" sheet)

| Draft | Role ISA | Notes |
|---|---|---|
| `JMP always` | `BR ALWAYS` | |
| `JMP !X`, `!Y` | `BR Z ra` | any of r0..r3 |
| `JMP X--`, `Y--` | `ALU SUB` + `BR NZ` | 2 words, but untimed, so wire timing does not move |
| `JMP X!=Y` | `ALU NE rd, ra, rb` + `BR NZ rd` | 2 words |
| `JMP pin` | `RAW JPIN line, level, target` | role block only (the pin differs between roles) |
| `JMP timeout` | `BR TO` | role block only |
| `WAIT pin/flag` | `COND` (edge, with a window profile) | waits for an edge, not a level |
| `WAIT` with timeout | `COND` with bounded `max`, plus `ABORT` or `BR TO` | timeout lives in the profile, not in Y |
| `IN pins n` | followed `XFER n` (`RAW GET` for raw sampling) | `n` = 1..16 |
| `OUT pins n` | owned `XFER n` | |
| `PUSH` / `PULL` | `PUSH` / `POP`, or `XFER dst=FIFO` / `src=FIFO` | XFER moves whole words, so there is no autopush threshold |
| `MOV` copy / invert / reverse | `ALU OR rd, ra, 0` / `LI` + `ALU XOR` / `XFER msb_first` | reversal is a wire-order flag |
| `IRQ` (set) | `MARK id` with `IRQ` | |
| `IRQ` wait/clear (sync between state machines) | **gap** | engines synchronize through the wire (`COND`) |
| `SET pins` | `HOLD` (timed) or `RAW SET` (role block) | |
| `SET X`, `LDI` | `LI` | 12-bit zero-extended immediate |
| `CRC` reset / feed / copy | `CRC RESET` / `FEED ra, n` / `GET rd` | feeds any register, not only OSR/ISR |
| delay / side-set bits | profiles (16-bit `min/nom/max`) and clocked lanes | no 3-bit delay cap |
| `1010–1111` free (MEASURE?) | **gap** | candidate: a `COND` flag that writes the measured `m` to a register (tainted). Phase 4. |

### 8.3 Registers and settings (draft "registers" sheet)

| Draft | Role ISA |
|---|---|
| X, Y (16) | r0..r3 (16) |
| OSR, ISR (16) | none: `XFER` shifts internally between the wire and a register or FIFO |
| PC (6) | PC; 64-word default depth, 12-bit targets |
| CRC (16) | CRC state (16) plus header config |
| TX/RX FIFOs (4 × 16) | same |
| clock divider | per-engine clock enable (`EngineSpec.clock`) |
| base pins (out/set/in/side) | the line table (8 logical lines → pins) |
| SIDE_COUNT | not needed: clocks come from clocked lanes |
| shift direction, auto push/pull | `XFER msb_first`; `src/dst = FIFO` |
| open-drain mask | `LineCfg.kind = OD` |
| CRC polynomial | `crc` header config |
| wrap top/bottom | `BR ALWAYS` (one untimed cycle, absorbed by `tb`) |
| host interface | open; Phase 5 |

### 8.4 Draft issues and how this ISA resolves them

| # | Draft issue | Resolution |
|---|---|---|
| 1 | Y is both the WAIT timeout counter and a compare register | The timeout is the profile's `max`; registers stay free. |
| 2 | No JMP_PIN setting; WAIT source codes undefined | `RAW JPIN` and `COND` name their line in the instruction. |
| 3 | Source/destination codes differ between IN, OUT, MOV and SET | One `src` table and one `dst` table. |
| 4 | 3-bit delay (max 7) | 16-bit profiles. |
| 5 | 5-bit bit counts against 16-bit shift registers | `n−1` in 4 bits: 1..16. |
| 6 | LDI extension unspecified | `LI` zero-extends 12 bits. |
| 7 | Number of state machines undecided | The model runs any number of engines. The chip count (2 or 3; a monitor needs its own) is a Phase 6 area decision, bounded by the macro limit below. |

The draft's open area question also applies here: a 32-bit word makes 64 words in flops
about 2× the draft's ~6K cells. The SRAM answer is now tested (§1): two
`RM_IHPSG13_1P_256x16` macros per engine. The power grid allows at most 4 macros on the 6x4
die, or 3 if the untested slot under the pin cluster fails. They are shared between the
engines' program memories and the fuzzer's storage. That split is plan decision D1.
