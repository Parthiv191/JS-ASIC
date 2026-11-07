# ISA v1: cycle-level contract

Status: v1 r2 (2026-11-05), owner Parthiv. This is the contract between the compiler, the Python
model (`toolchain/emu`), the fuzzer and the RTL. Where the RTL and the model disagree, this doc
decides. Where it is silent, the model decides and this doc gets updated. Why each rule exists:
`decisions.md`.

Tags: **[P]** Proposed, **[O]** Open. Everything else is Decided.

**The model is still v0** (1-cycle untimed instructions, 4 registers, fractional clocks). It must
implement this v1 doc, §4.0 especially, before RTL co-simulation.

## 1. Constants and per-engine state

| Name | Value | Notes |
|---|---|---|
| `SYNC_LAT` | 2 | 2-flop input synchronizer: at cycle `c` an engine sees the wire as it was at `c−2` |
| `EDGE_LAT` | 3 | `SYNC_LAT + 1`: a drive in cycle `t` is on the wire from `t+1` and seen at `t + EDGE_LAT` |
| `QUANT` | 1 | measurement slack on every window check |
| `ISSUE` | 2 | max cycles from a timed step's completion to its successor's start (§4.0) |
| `U` | 2 | cycles per untimed instruction (§4.0). There is no clock divider in v1. |
| Registers | 8 × 16 bit | `r0..r7` |
| Lines | 8, shared table | logical `L0..L7`, mapped to pins by `LINE.pin` (`architecture.md` §4) |
| Profiles | 8 built (field allows 16), one table shared by both engines; **profile 7 is per-engine**: initialised from the shared PROF7 at RUN, then written only by `PROF` | `(min, nom, max)`, 16-bit cycles (max 1.3 ms at 50 MHz); entry 0 = `IDLE` = `(0, 0, INF)` |
| Lanes | 4 built (field allows 16), shared table | §2 |
| Program | 512 words, one image shared by both engines | branch targets are 12-bit (JPIN 11-bit) |
| FIFOs | TX 4, RX 8 (16-bit words) | the model must use these depths |
| Time | `now`, `tb`, anchors: 24 bit | §4.1 time rule |
| CRC | 16-bit state | §4.4 |
| `INF` | `0xFFFF` | `max = INF`: no timeout |

The chip reports its sizes (architecture §4). The compiler rejects images that don't fit.

## 2. Roles, owners, lines, lanes, header

- **Roles:** `P0 = 0`, `P1 = 1`, `MON = 2`. A description names two parties; party 0 is P0.
- **Step owner codes:** `0 P0`, `1 P1`, `2 SHARED` (OD only), `3` reserved.
  - An engine **does** a step iff `role ≠ MON` and (`owner == role` or `owner == SHARED`).
  - Otherwise it **follows** the step.
- **Line config:** `kind ∈ {PP, OD}`, `owner ∈ {P0, P1, SHARED (OD only), SWITCHED (reserved,
  for bidirectional PP lines)}`, `idle ∈ {0, 1}`, `gated`.
  - `gated` (PP only): the owner drives the line only from its first owned step on it until
    the next STOP on a SELECT bus, or an abort; otherwise the line is released. For example, SPI
    MISO on a shared bus.
  - When an engine starts, each non-gated PP line its role owns is driven to `idle`; every other line is
    released. Before RUN, every pin is an input.
  - `drive(L, v)`: PP outputs `v`; OD pulls low for 0 and releases for 1.
  - An undriven line reads what the pin reads. External pull-ups, or LOOPBACK mode
    (architecture §2), make it 1.
  - Two drivers on a PP line is contention. The compiler prevents it; the model reports it.
- **Lane config** (architecture §4 holds the byte layout):

| Field | Bits | Meaning |
|---|---|---|
| `data` | 3 | data line |
| `clk` | 3 + valid | clock line; none = an unclocked lane |
| `cpol`, `cpha` | 1 + 1 | clock mode |
| `data2` | 3 + valid | second data line: duplex on a clocked lane, or the complement with the CODEC |
| `stretch` | 1 | the clock owner waits for `clk` to really rise (§4.3); `clk` must be OD |
| `ldelay` | 4 | data launch/release delay after the launching clock edge, in cycles (§4.3); I2C uses ~300 ns |
| `nrzi`, `stuff_n`, `d2_comp` | 1 + 3 + 1 | CODEC for USB-LS **[P]** (§4.10) |

- **Image header:**
  - lines, lanes, profiles, `abort_pc`, CRC config, `prof_shift` (default 4);
  - the bus: `bus_kind` (`NONE`, `I2C`, `SELECT`, `SE0` [P]), `bus_a`, `bus_b` (lines),
    `bus_confirm` (cycles; default 15 for I2C, 1 for SELECT, 33 for SE0);
  - `tolerance_ppm` (compiler only).
- **Shared vs per-engine:** both engines run one image, so the header tables are one shared
  copy. Per engine: the role, profile 7, and all execution state.

## 3. Instruction word

Two shapes, selected by the opcode:

```
T (COND HOLD XFER): [31:27] op | [26:25] owner | [24:21] line/lane | [20:17] prof | [16:5] operand | [4:0] flags
U (all others):     [31:27] op | [26:24] rd    | [23:21] ra/line   | [20:17] sub  | [16:5] operand | [4:0] flags
```

Flags: `CHK = 1<<0`, `RESYNC = 1<<1`, `ABORT = 1<<2`, `MSB = 1<<3` (XFER only), `REG = 1<<3`
(ALU only), `IRQ = 1<<4` (MARK only). The decode depends on the opcode.

| Op | Code | Shape | Fields |
|---|---|---|---|
| `NOP` | 0 | U | – |
| `COND` | 1 | T | line (0 for START/STOP) · operand `[1:0]` FALL 0 / RISE 1 / START 2 / STOP 3 · `[2]` MEAS · `[5:3]` rd · ABORT |
| `HOLD` | 2 | T | line · operand `[0]` level · ABORT |
| `XFER` | 3 | T | **lane** (always; unclocked lanes have no `clk`) · operand `[3:0]` n−1 · `[7:4]` src · `[11:8]` dst · CHK, RESYNC, ABORT, MSB |
| `LI` | 4 | U | rd · imm16 = `{sub, operand}` |
| `ALU` | 5 | U | rd, ra · sub = aluop · operand = imm12 (zero-extended), or `[2:0]` rb with REG |
| `BR` | 6 | U | ra · sub ALWAYS 0 / Z 1 / NZ 2 / TO 3 / **ROLE 4** (taken iff the engine's role is in the mask held in the `ra` field) · operand = target |
| `ROLE_IF` | 7 | U | operand `[2:0]` role mask (P0, P1, MON) · `[11:3]` count |
| `PUSH` / `POP` | 8 / 9 | U | PUSH ra → RX FIFO (full: §4.9) · POP rd ← TX FIFO (stalls while empty) |
| `MARK` | 10 | U | operand = id · IRQ |
| `RAW` | 11 | U | sub = SET 0 / REL 1 / GET 2 / DELAY 3 / JPIN 4 / EVT 5 · line in `[23:21]` · rd for GET/EVT · operand: SET `[0]` level · DELAY n (≥ U) · JPIN `[11]` level, `[10:0]` target · EVT `[5:0]` clear mask |
| `HALT` | 12 | U | – |
| `CRC` | 13 | U | sub = RESET 0 / FEED 1 / GET 2 · ra (FEED) / rd (GET) · FEED operand `[3:0]` n−1 |
| `FZ` | 14 | U | sub = NEXT 0 / TOK 1 / END 2 · rd / ra (`bus-fuzzer.md`) |
| `PROF` | 15 | U | sub = 7 (the engine's own profile; other values reserved) · ra (§4.8; the first Plus item to cut) |

- Reserved (executing one is ILLEGAL, §4.1), with no v1 hardware: `CHOICE 16`, `ARB 17`, `WAIT_ANY 18`, `CODEC 19` (the CODEC
  lives in the lane config). Codes 20–31 are free.
- `src`: ZERO 0, ONES 1, FIFO 2, FZ 3, r0–r7 = 8–15.
- `dst`: DISCARD 0, FIFO 2, r0–r7 = 8–15; other codes reserved.
- `aluop`: ADD 0, SUB 1, AND 2, OR 3, XOR 4, SHL 5, SHR 6, EQ 7, NE 8, LT 9.
  - Comparisons write 0/1. Results wrap to 16 bits, `LT` is unsigned, and shifts use `b & 15`.

## 4. Execution and time

### 4.0 Fetch and issue (interleaved program SRAM)

- A free-running phase bit `ph` toggles every clk cycle from reset. Engine `e`'s **own cycles**
  are those with `ph = e`; it may read the program SRAM only on them.
- **Start:**
  - A RUN write takes effect on the next `ph = 0` cycle. When an engine's RUN bit rises, its first own cycle `s0` sets `now := 0`, `tb := 0`, the anchors `:= 0`
    and presents address 0.
  - Instruction 0 starts at `now = 2`.
  - Engines started by one RUN write are therefore 1 clk apart, engine 1 behind. The model runs
    engine `e` with phase offset `e`.
- There is no clock divider in v1: an engine cycle is a clk cycle, and `now` counts them. Slow
  signals (> 1.3 ms per profile) are a Later-tier feature.
- **Fetch.**
  - An instruction may **start only on an own cycle**.
  - In its start cycle it presents its successor's address: `pc+1`, or the target that
    BR/JPIN/ROLE_IF decides in that cycle.
  - The word lands in the engine's prefetch register two clk cycles later.
  - The successor starts on the first own cycle ≥ max(predecessor completion + 1, word arrival).
- **Costs that follow:**
  - **Untimed instructions, taken branches included:** exactly `U` cycles, except FZ ops, which
    have fixed totals (`bus-fuzzer.md` §3). `ROLE_IF` costs `U`
    whether or not it skips; a skip continues at `pc + 1 + count`, and the skipped words take no
    time. Role blocks don't nest.
  - **`RAW DELAY n`:** `n` cycles, rounded up to a multiple of `U`.
  - **Timed step** started at `t`, completing at `c`: its successor starts on the first own cycle
    ≥ max(c + 1, t + 2), so `ISSUE` ≤ 2.
  - **Effects:** an untimed instruction acts in its start cycle. That covers RAW SET's drive,
    RAW GET/JPIN/EVT sampling, register writes and MARK timestamps; the successor sees them all.
  - **Stalls:** only `POP` (empty TX FIFO) stalls. It retries on every own cycle and completes
    on the first one with data. `PUSH` never stalls (§4.9), and FZ ops have fixed costs.
  - **Abort** decided in cycle `t`: `abort_pc` is fetched on the first own cycle ≥ t + 1 and
    starts 2 cycles later. That is a cost of 3 or 4, plus `FZ_HANG_CYCLES` for a fatal abort while the
    fuzz unit is armed (`bus-fuzzer.md`). Bus exits never add it.
- **Debug halt** stops the engine at an instruction boundary: `now`, `tb` and the detector
  freeze. Resuming refetches the PC; a word in flight is dropped. Debug is not cycle-exact
  against the model.

### 4.1 Time and the timeline
- **Inputs.** Synchronized inputs are sampled every engine cycle. `edge(L)` means the value
  differs from the previous cycle's. Edges during untimed instructions aren't latched, except by
  the bus detector (§4.7) and the OD let-go tracker (§4.3). The synchronizer is preloaded with each line's idle level when RUN
  rises.
- **Timeline `tb`.** Timed steps are scheduled against `tb`, not against instruction count, so
  untimed instructions between steps don't move wire timing.
  - An owner acts at the planned time.
  - A follower re-anchors on each owner event: `tb := detect − EDGE_LAT`.
- **Issue slot and overrun.**
  - A timed step may start up to `ISSUE` cycles after its planned start (`tb`). An action
    planned at offset 0 (HOLD, XFER bit 0) then happens in its start cycle, and the timeline
    doesn't move. A COND instead sets `tb` to its action cycle, since followers anchor on its
    edge.
  - Any other late action logs `OVERRUN(now − planned)` and happens immediately.
  - `IDLE` steps never overrun.
  - A clock owner's clocked XFER never overruns. Instead it sets `tb := max(tb, start − H)` for
    `cpha = 1`, and `tb := max(tb, start)` for `cpha = 0` (bit 0 at `max(start, tb)`, a full `H`
    before the first edge).
  - **Stall re-base.** If an owned step starts late because its owner stalled (`POP`, or a
    `src = FIFO` wait), it sets `tb := max(tb, start)` and logs nothing. `T5-stall` (§6) keeps
    the followers safe.
- **Window accumulation.** Each follower keeps an anchor and the accumulated `(min, nom, max)`
  of the HOLD/XFER bits since it. These are 24-bit, and `max` carries an INF flag.
  - An edge check compares `e − anchor` against `[min − QUANT, max + QUANT]`.
  - A COND timeout without ABORT re-anchors at its new `tb`; an abort re-anchors at `now`.
  - RESYNC moves only the sampling anchor.
- **24-bit time.** All time comparisons are signed 24-bit differences. In every engine cycle,
  if `now − tb > 2^22`, set `tb := now − 2^22` (the anchors likewise). OVERRUN values and `m`
  saturate at `0xFFFF`. The compiler rejects `A > 2^22`.
  - RTL note: keep each time point as a saturating age counter (`now − x`). The clamp then comes
    free, and every check is one compare.
- **Timeout flag `to`.** It is cleared when a timed step starts and set on TIMEOUT without
  ABORT. `BR TO` tests it, and is legal only in role blocks.
- **Abort.** Triggered by a fatal follow failure on an ABORT step (TIMEOUT, LEVEL, EDGE,
  CLK_PHASE), or a **bus exit** (§4.7).
  - A fatal abort logs `ABORT(pc)`, a VIOL bit and a first-violation entry. A bus exit is
    **normal**: it adds no record of its own (the detector already logged the BUS record), sets no
    VIOL bit and causes no fuzzer hang.
  - Both release OD lines, set `tb := now` and go to `abort_pc` (§4.0).
  - A partial XFER writes no `dst` and pushes nothing.
  - Only the failing role jumps (`T5-abort`). `abort_pc` is 0 unless set; the compiler warns on
    ABORT without it.
- **Event register `evt`** (sticky): bit 0 START, 1 STOP (these two are the bus flags of §4.7), 2 TIMEOUT, 3 MISMATCH (LEVEL / EDGE /
  CLK_PHASE), 4 OVERFLOW, 5 UNDERRUN.
  - `RAW EVT rd, mask` does `rd := evt`, then clears the masked bits, the bus flags included.
    `rd` is tainted.

**Failure handling** (kind codes in §7):

| Kind | Raised by | With ABORT | Without ABORT |
|---|---|---|---|
| OVERRUN | an owner action, or a follower sample, past its planned cycle | logged only | logged only |
| EARLY | a followed COND edge before `min − QUANT` | logged; the step completes | same |
| TIMEOUT | a bounded followed COND, a clocked follower, a stretch wait | fatal abort | `to := 1`. A COND gets `tb := tb + nom`. A clocked XFER or stretch wait gets `tb := now`, writes no `dst` and pushes nothing. The step ends. |
| LEVEL | a followed HOLD mismatch | fatal abort | logged; continue |
| EDGE | an unclocked XFER with CHK | fatal abort | logged; continue |
| CLK_PHASE | a clocked follower with CHK (the first phase isn't checked) | fatal abort | logged; continue |
| OVERFLOW, UNDERRUN | FIFOs (§4.9) | never aborts | logged |
| ILLEGAL | a reserved opcode, `FZ` on engine 1 | the engine halts | the engine halts |

- An edge and a timeout in the same cycle: the edge wins.
- A clocked follower's timeout counts from the step start for the first edge, and from the last
  edge after that.
- **HALT** (and ILLEGAL): the engine stops. PP lines keep their level, OD lines are released,
  the FIFOs stay readable, and STATUS shows halted.

### 4.2 `COND kind, line, owner, prof [, meas=rd]` and `HOLD line, level, owner, prof`
- **COND do:** at `max(now, tb + nom)` (overrun check if bounded) drive the post-edge level, then
  `tb :=` action cycle.
  - START = FALL on `bus_a`; STOP = RISE on `bus_a` (I2C and SELECT).
- **COND follow, FALL/RISE:** on the edge detected at `c`, `e = c − EDGE_LAT` and `m = e − tb`.
  - If `min > 0` and `m < min − QUANT`, log `EARLY(m)`. Then `tb := e`, re-anchor, complete.
  - If `max < INF` and `now − EDGE_LAT > tb + max + QUANT` with no edge: TIMEOUT. With ABORT,
    abort; otherwise `tb := tb + nom` and complete.
  - `IDLE` waits forever.
- **COND follow, START/STOP:** completes when its flag is set, including one set before the step.
  - `m = start_e − tb` (or `stop_e`), the EARLY check, then `tb :=` that anchor and re-anchor.
  - START clears both the START and STOP flags; STOP clears both too.
  - The timeout check uses `now − EDGE_LAT − bus_confirm`. MEAS uses `max(m, 0)`.
  - A followed START ignores STOP. A followed STOP that sees START is a bus event (§4.7); without
    ABORT it keeps waiting.
- **MEAS:** on completion, `rd := min(max(m, 0), 0xFFFF)`. A do uses `action − tb`; a TIMEOUT writes
  `0xFFFF`. `rd` is tainted.
- **HOLD do:** at `max(now, tb)` drive `level`, then `tb := tb + nom`. The timeline doesn't move
  inside the issue slot. Completes that cycle.
- **HOLD follow:** sample at `tb + nom//2 + EDGE_LAT`. A mismatch logs LEVEL (and aborts with
  ABORT). Then `tb := tb + nom`.

### 4.3 `XFER n, lane, owner, src, dst, msb, prof`
- **`dst` = the wire value, in every role.** The owner reads its own bits back at the point a
  follower in its position samples them:
  - on an OD line, someone else's 0 shows up;
  - on a PP line it equals `src`.
- `dst = FIFO` pushes only in follow mode. The owner takes `src` at step start.
- **Empty `src = FIFO`:**
  - An owner that owns the lane's clock, or an unclocked lane, waits for data (a clock owner
    doesn't log OVERRUN; §4.1).
  - An owner that doesn't own the clock **sends ONES**, logs UNDERRUN and consumes nothing.
- Bit `k` is `value >> k` (LSB first) or `value >> (n−1−k)` (MSB).
- **Unclocked lane** (`P = nom`):
  - Do: bit `k` at `max(now, tb + k·P)`, then `tb := tb + n·P`.
  - Follow: sample bit `k` at `a + (k − ak)·P + P//2 + EDGE_LAT`, from the anchor `(a, ak)`
    (initially `(tb, 0)`).
  - An edge seen before sample `k` belongs to boundary `k`. CHK checks it against the window
    anchor plus the accumulated window plus `k·(min, max)` of this step. Owners and followers
    both accumulate; a COND or a clocked XFER re-anchors the window. Out of window: `EDGE(m)`,
    and with ABORT a fatal abort.
  - RESYNC: an in-window edge re-anchors `(a, ak) := (e, k)`.
  - Then `tb := a + (n − ak)·P`. RTL note: compute every `k·P` incrementally; no multiplier.
- **Clocked lane** (`H = nom`): bit `k`'s leading edge (clock → `1−cpol`) is at `tb + (2k+1)H`,
  its trailing edge (→ `cpol`) at `tb + (2k+2)H`.
  - `cpha = 0`: capture on the leading edge, launch on the trailing (bit 0 at step start).
    `cpha = 1`: launch on the leading edge, capture on the trailing. Bit 0 with `cpha = 0` goes
    out at `max(start, tb)` with no `ldelay`; `ldelay` applies only to launches triggered by an
    edge.
  - The clock owner (not MON) makes the edges, then `tb := tb + 2nH`.
  - **Launch:** a data owner that owns the clock launches at the edge time + `ldelay`. Any other
    data owner launches at `max(detect, e + ldelay)`, where `e = detect − EDGE_LAT`.
  - **Sample:** a follower that owns the clock samples at `capture_k + EDGE_LAT`. Any other
    follower samples in the cycle it detects the capture edge.
  - Clock followers complete on the `n`-th trailing edge, with `tb := detect − EDGE_LAT`.
    - CHK logs `CLK_PHASE` for a phase shorter than `min − QUANT`.
    - With `max < INF`, an edge missing for `max + QUANT` logs TIMEOUT (abort with ABORT).
  - The clock owner completes at the later of its last trailing edge and its last sample.
- **Duplex** (clocked, with `data2`): each data line follows the rules above with its own owner
  (from the line config).
  - `dst := (v2 << n) | v1`, so `n ≤ 8`.
  - A `dst = FIFO` push sends that same packed value, in every engine that drives fewer than all
    of the step's data lines.
- **OD let-go.** After an XFER in which it drove an OD data line `D`:
  - the engine keeps the last bit until the lane's next leading clock edge as it sees it (the
    clock owner: the cycle it drives that edge; others: the cycle they detect it);
  - it releases `D` `ldelay` cycles after that edge (same rule as launch), unless it launches a
    new bit on `D` then;
  - any explicit drive of `D` cancels the pending release;
  - a per-lane tracker watches the clock every cycle, whatever instruction is running, so a
    release can happen during later HOLD, COND or untimed instructions.
  - With I2C (cpol 1, cpha 1) a release never happens while SCL is high.
- **Clock stretching (lanes with `stretch`).**
  - After each release of `clk` planned at `t`, the clock owner waits until it reads `clk = 1` at
    cycle `c`, and takes `e = c − EDGE_LAT`.
  - If `e > t`, the rest of the step and `tb` shift by `e − t`.
  - The wait is bounded by `max`: TIMEOUT, then ABORT, or `to` set, `tb := now` and the step
    ends.
  - RTL note: when `e > t`, add the shift to the step's offset counter; don't re-add it to
    every planned time.
  - `HOLD clk 1` and `COND RISE clk` on a stretch lane's clock wait the same way.
  - **The chip never stretches as a target** (v1).

### 4.4 `CRC`
- Header: `crc = (width 1–16, poly, init, xorout, lsb_first)`, catalogue form. Default
  CRC-16/CCITT-FALSE.
- `RESET`: state := `init` (reflected if `lsb_first`).
- `FEED ra, n`: shift in the low `n` bits of `ra`, MSB first (LSB first with `lsb_first`).
  - The hardware does 8 bits per clk cycle, which fits inside the instruction's cost.
  - RTL note: keep the state and poly left-justified, and apply `lsb_first` as a bit reverse.
- `GET rd`: `rd := state ^ xorout`.
- Check values for "123456789": CCITT-FALSE `0x29B1`, MODBUS `0x4B37`, CRC-5/USB `0x19`.

### 4.5 `MARK id [IRQ]`
Logs `MARK(id)`. With IRQ it logs `IRQ(id)` instead, latches `id` and raises the IRQ until the
host clears it.

### 4.6 `FZ` and `src = FZ`
- Costs and semantics: `bus-fuzzer.md`. `FZ` is legal only in a single-party role block
  (`R3-fz`), and only on engine 0.
- `src = FZ` sends the next staged byte (0 when exhausted).

### 4.7 The bus: detector and bus events
- **The detector** runs every engine cycle, in every role except the owner of the bus's
  **master line**: `bus_b` (SCL) for I2C, `bus_a` (CS) for SELECT. For SE0 it runs in every
  role. It recognizes:

| `bus_kind` | Candidate at cycle `c` | Confirmed if, for the next `bus_confirm` cycles… | Sets |
|---|---|---|---|
| I2C | `edge(bus_a)` while `bus_b` read 1 now and in the previous cycle | `bus_b` stays 1 | fall = START, rise = STOP |
| SELECT | `edge(bus_a)` | `bus_a` keeps its new level | fall = START (CS active), rise = STOP |
| SE0 [P] | `bus_a` and `bus_b` both 0 (first cycle) | both stay 0 (default 33 = 1 bit) | STOP (end of packet) |

  - On confirmation (the end of cycle `c + bus_confirm`): set the flag, and its own anchor
    (`start_e` or `stop_e`) `:= c − EDGE_LAT`. The flag is visible from the next cycle.
  - A failed confirm drops the candidate. A new candidate replaces the old one.
  - `start_e` and `stop_e` take part in the 24-bit clamp. COND START anchors on `start_e`, COND
    STOP on `stop_e`.
- **Bus exits.** During any timed step except a followed COND START/STOP (those handle their
  flags as in §4.2), a set START or STOP
  flag (set now or earlier) means:
  - with ABORT: a **bus exit** to `abort_pc` (§4.1). It's normal, not a fault, and the flag
    stays set;
  - without ABORT: nothing happens.
- On SELECT, `gated` lines are released on STOP (§2).
- **Target pattern.** `abort_pc` is an unbounded followed `COND START` (`T5-abort`).
  - After a STOP (or CS rising), it waits for the next START.
  - After a repeated START, its flag is still set, so it completes at once.
  - For SPI, CS rising mid-frame ends the frame through the same path.

### 4.8 `PROF p, ra` (MEASURE level 2; Plus tier)
- `v = max(ra, 1)` and `s = prof_shift`. Then:
  - `nom := v`
  - `min := v − (v >> s)`
  - `max := min(v + (v >> s), 0xFFFE)`
- `p` must be 7, the engine's private profile. While the engine runs, the host can't write
  profiles: tables are written only while stopped.
- Compiler: a `dynamic=(lo, hi)` profile skips T1–T5 in v1, with a note in the output.

### 4.9 FIFOs full or empty
- **RX full:** a push (PUSH, or a followed XFER with `dst = FIFO`) drops the word, logs OVERFLOW
  (a data-loss kind, never an abort), and sets `evt` bit 4 and its VIOL bit. The chip never stalls on a
  full RX FIFO.
- **TX empty:** `POP` stalls. Otherwise §4.3: the clock owner waits, and a non-owner of the clock
  sends ONES and logs UNDERRUN. Guidance: a target should use `src = FIFO`, not POP + register,
  so a slow host can't desync it.

### 4.10 CODEC lanes **[P]** (USB-LS; stretch tier, built only if feasible)
- `nrzi`: a 0 is a transition, a 1 is none.
- `stuff_n`: after N identical bits, a stuffed opposite bit is sent (or removed on receive).
  Stuffed bits take their own bit slot and extend `tb`.
- `d2_comp`: `data2` is driven as the complement of `data`, and `data2` is allowed on an
  unclocked CODEC lane.
- The NRZI level and the stuff count persist across XFERs. They reset to the line's idle level
  (J) at a COND or an abort.
- On receive, `data2` is ignored except by the SE0 detector.
- A receive stuff error logs EDGE (kind 4).
- T1 credit: on a CODEC lane, RESYNC counts as an anchor every `stuff_n + 1` bits
  (`A ≤ (stuff_n + 1)·P`), because stuffing guarantees an edge that often.

## 5. Well-formedness rules (compiler)

| Rule | Statement |
|---|---|
| `R1-owner` | A timed step's owner field is the owner of its data line: COND/HOLD's line, XFER's lane `data`. It is a party, or `shared` on OD; on a SHARED OD line the owner field may be either party or `shared`. The clock and `data2` drivers come from their line owners. A lane's clock has a party owner, and a duplex `data2` owner differs from `data`'s. |
| `R1-shared` | `shared` only on OD lines |
| `R1-bus` | COND START/STOP is owned by the master line's owner (I2C: the `bus_b` owner; SELECT: the `bus_a` owner). It is follow-only for SE0. |
| `R2-branch` | Outside role blocks, `BR Z/NZ` tests only clean registers: written by `XFER dst`, `LI`, `ALU` on clean inputs, or `CRC GET` of a clean state. Taints: `POP`, `RAW GET/EVT`, `COND meas`, any write in a role block, and every register at `abort_pc`. The CRC state is a ninth taint slot; it's a forward dataflow analysis. **Inside a role block any branch is allowed.** `RAW JPIN` and `BR TO` are only legal in role blocks. `BR ROLE` is a role split like `ROLE_IF`: the analysis follows each role's path, and `R3-escape` applies to its target. |
| `R3-timed-in-role` | Role blocks contain no timed steps, except lead blocks |
| `R3-lead` | `lead(party)`: one party. Every timed step in it is owned by that party, or uses a lane whose clock that party owns. It leaves either by its fall-through exit (the leader carries on with the next step; no timed step may lie on that path) or by a path that runs a bus COND START/STOP and then branches to `abort_pc`. The block's CFG fall-through successor is a timed step with ABORT, which the other roles follow speculatively. It may not have CHK, and its profile `max` must be INF. |
| `R3-raw-drive` | `RAW SET/REL` only in a role block without MON; on a PP line, also only if the block's mask ⊆ {line owner} |
| `R3-escape` | No branch into a role block from outside. A branch out of a role block may go only to its exit or to `abort_pc`. `abort_pc` is not inside a block body. |
| `R3-fz` | `FZ` only in a block for exactly one party, the same party everywhere |
| `S-*` | Names resolve; fields in range; tables fit the chip's sizes; program ≤ 512; `data2` only on a clocked lane (or a CODEC lane with `d2_comp`); duplex `n ≤ 8`; CRC config valid; unclocked ABORT only with CHK or an active bus (`S-flags`); immediate shift < 16; `PROF` p = 7; `RAW DELAY n ≥ U`; `BR ROLE` mask ≠ 0; `gated` only on PP lines |
| `S-bus` | I2C: `bus_a` and `bus_b` are OD, and START/STOP steps are used with a cpol 1, cpha 1 lane on (`bus_a`, `bus_b`). SELECT: `bus_a` is a PP line. COND START/STOP need `bus_kind ≠ NONE` and line field 0. `stretch` needs an OD `clk`. |

## 6. Timing rules (compiler)

`d = tolerance_ppm / 1e6`. `A` = nominal cycles since the last guaranteed anchor, the max over CFG
paths. Anchors are a followed COND (START/STOP included), a clocked XFER, and an exit into
`abort_pc` from a bus event or a lead block. HOLD and unclocked XFER add their duration. `A`
unbounded around a loop fails `T1-unanchored`.

`L` (used in T3) = the start cycle of a step relative to its `tb`. That is the predecessor's
completion offset + `ISSUE` + `U` × (untimed instructions between them), worst case over paths,
with `RAW DELAY n` counted as `n` rounded up to a multiple of `U` and FZ ops at their fixed
costs. `POP` counts `U` (a stall is covered by `T5-stall`). `A` and `L` are computed per role.

| Rule | Check |
|---|---|
| `T1-sample` | Follower sample of slot `k`: `ceil(d·(A + (k+1)·P)) + QUANT + ISSUE < P//2` |
| `T2-window` | A bounded COND, and the accumulated CHK windows: `min ≤ nom − ceil(d·(A + nom)) − QUANT` (COND: only if `min > 0`) and (`max = INF` or) `max ≥ nom + ceil(d·(A + nom)) + QUANT`. D2: a CHK unclocked XFER at `A = 0` is rejected; drop `check` or put a timed step first. |
| `T3-latency` | Clocked, clock followers: `H − ceil(d·H) ≥ 2`. A data owner that isn't the clock owner: `H ≥ EDGE_LAT + 1 + ldelay + ceil(d·2H)`. Its bit 0 with `cpha = 0`: `H ≥ L + 1 + ceil(d·(A + H))`. |
| `T4-profile` | `0 ≤ min ≤ nom ≤ max ≤ INF`. `nom ≥ 1` for HOLD and XFER. `H ≥ 2`. Stretch lanes: `H ≥ EDGE_LAT + 2`. `ldelay ≤ H − EDGE_LAT − 2`. |
| `T5-watch` | `L + ceil(d·(A + obs)) ≤ obs + EDGE_LAT`, where `obs` is the owner's planned offset of the first observable event: `nom` (COND), `H` (clocked XFER first edge), `nom//2` (HOLD / unclocked XFER first sample) |
| `T5-overrun` | Owner start ≤ planned offset if it is > 0, or ≤ `ISSUE` if it is 0. Planned offset: `nom` for COND, 0 for HOLD and unclocked XFER. Clocked XFER is exempt (§4.1). |
| `T5-timeout` | A bounded COND without ABORT is followed only by an unbounded COND |
| `T5-abort` | The first timed step a role can reach from `abort_pc` is an unbounded COND that it follows **or does** |
| `T5-stall` | After an owner's possible stall (`POP`, `src = FIFO`), its next owned step must be an unbounded COND, a clocked XFER it clocks, or inside a lead block, so followers wait for it |

- Completion offsets relative to the step's end `tb`:
  - COND: do 0, follow `E` (START/STOP: `E + bus_confirm + 1`).
  - HOLD: do `−nom`, follow `−nom + nom//2 + E`.
  - Unclocked XFER: do `−P`, follow `−P + P//2 + E`.
  - Clocked XFER: clock follower `E`; clock owner with the data 0; clock owner following the
    data `max(0, E − H)` (cpha 0) or `E` (cpha 1). Add `E` on a stretch lane.
  - **OD owners** read their data back, so they complete at the follow offsets: unclocked
    `−P + P//2 + E`, clocked at the last capture `+ E`. PP owners don't sample.
  - Stretched HOLD/COND on a stretch clock: `+E`. A timed-out followed COND:
    `max − nom + QUANT + E + 1`.
  - Abort into `abort_pc`: the decision cycle + 4 (+ `FZ_HANG_CYCLES` for a fatal abort while armed). For a bus
    exit the decision is `bus_e + E + bus_confirm + 1`, which sets the timing of I2C's repeated START
    (see chip-spec §5).
  - Reset counts as a predecessor completing at 0 with `A = 0`, so instruction 0 starts at `now = 2`.
- The rules are conservative bounds, and the **fuzz loop is the arbiter**. Any accepted program
  that false-alarms, corrupts data or hangs in tolerance is a rule bug or a model bug.

## 7. Codes and observability

**Event kinds.** The same codes are used for VIOL bits, capture EVENT records and the fuzzer's
per-iteration event mask:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| OVERRUN | EARLY | TIMEOUT | LEVEL | EDGE | CLK_PHASE | ABORT (fatal) | OVERFLOW | UNDERRUN | BUS exit | ILLEGAL |

- VIOL bits 0–8 and 10 are sticky. Kind 9 (bus exit) is normal: it appears only in the fuzzer's
  event mask, never in VIOL or in EVENT records.
- The ERR pin is the OR of VIOL bits under a host mask (default: all).
- FIRST_VIOL records the first fatal or timing kind (0–6, 10).

**Capture record** (32 bit): `[31:28] type | [27:16] Δt | [15:0] value`.

| Type | Value |
|---|---|
| 0 DATA | the followed XFER's wire value (duplex: packed) |
| 1 BUS | 0 START, 1 STOP (I2C, SELECT), 2 end of packet (SE0) |
| 2 EVENT | `[3:0]` kind, `[15:4]` pc |
| 3 MARK | id |
| 4 MEAS | the measured value |
| 5 FINDING | `[1:0]` 0 new / 1 hang / 2 hang not saved, `[6:2]` slot, `[15:7]` iteration mod 512 |
| 15 TIME_EXT | `Δt >> 12` (saturating) |

- `Δt` is stamped when the record is generated, as `(t − p) >> shift`, where `p` is the stamp
  of the last *queued* record.
  - If that is ≥ 4096, a TIME_EXT record (its own `Δt` = 0) is queued first and takes a
    queue slot.
  - Cycles are engine cycles (= clk).
- Records are written by the engine selected for capture (architecture §7):
  - one per followed XFER, confirmed bus event, event, MARK and MEAS;
  - at most one per cycle, through a 2-entry queue in this priority: BUS, EVENT, DATA, MEAS,
    MARK;
  - several EVENT records in one cycle go in kind order;
  - when full, records are dropped and `CAP_LOST` is counted.
- A decoder maps DATA records to steps by program order. The host has the image. A partial XFER
  cut by a bus exit emits no DATA record.

**Model trace.** The model also emits `StepRecord(pc, op, mode, value, cycle, tb)` per timed step,
plus every event, sample point and branch outcome. Co-simulation compares at step boundaries,
against the RTL trace port (architecture §5).

## 8. History
The v0 text (r2/r3 notes, Task 7 audit notes, the mapping from the PIO-style team draft) is in git
history: `isa-v0.md` at `de24585`.
