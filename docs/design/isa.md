# ISA v1: cycle-level contract

Status: v1 (2026-11-05), owner: verification role. This is the contract between the compiler, the Python
model (`toolchain/emu`), the fuzzer and the RTL. Where the RTL and the model disagree, this doc
decides. Where it is silent, the model decides and this doc gets updated. Why each rule exists:
`decisions.md`.

Tags: **[P]** Proposed, **[O]** Open. Everything else is Decided.

**Model status:** the encoding, the image, fetch and issue timing (§4.0), reserved codes (§3),
`BR ROLE`, registers across RUN (§1), start-up drives (§2), `evt` and `RAW EVT`, the 24-bit clamp
(§4.1), an OD owner's readback (§4.3), the FIFO depths with OVERFLOW (§4.9) and the stall re-base
(§4.1) and clocked XFER, duplex and OD data with the let-go included (§4.3, with CLK_PHASE,
TIMEOUT and UNDERRUN) and clock stretching, the I2C and SELECT bus detector with COND START/STOP and
bus exits (§4.7), COND MEAS, `gated` lines and lead blocks (`R3-lead`) are v1. The model keeps fractional clocks only to stand in for external devices.

## 1. Constants and per-engine state

| Name | Value | Notes |
|---|---|---|
| `SYNC_LAT` | 2 | 2-flop input synchronizer: at cycle `c` an engine sees the wire as it was at `c−2` |
| `EDGE_LAT` | 3 | `SYNC_LAT + 1`: a drive in cycle `t` is on the wire from `t+1` and seen at `t + EDGE_LAT` |
| `QUANT` | 1 | measurement slack on every window check |
| `ISSUE` | 2 | max cycles from a timed step's completion to its successor's start (§4.0) |
| `U` | 2 | cycles per untimed instruction (§4.0). There is no clock divider in v1. |
| Registers | 8 × 16 bit | `r0..r7`. They keep their values across RUN; reset and SOFT_RESET clear them. The host can write them while the engine is stopped or halted (architecture §4). |
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
- **Line config:** `kind ∈ {PP (push-pull), OD (open-drain)}`, `owner ∈ {P0, P1, SHARED (OD only), SWITCHED (reserved,
  for bidirectional PP lines)}`, `idle ∈ {0, 1}`, `gated`, and `pin`: the uio pin it maps to (default: its
  index). Each line has its own pin; the image rejects two lines on one pin.
  - `gated` (PP, idle 1): the owner drives the line only from the first step that drives it
    until the next STOP on a SELECT bus, or an abort; otherwise the line is released and reads
    the pull-up. For example, SPI MISO on a shared bus.
    - Each engine has one gate for its gated lines. It is shut at RUN, and opens at the start of
      each timed step. While it is shut, drives to gated lines (RAW SET too) do nothing.
    - The gate shuts, and the engine's gated lines are released, when its own detector confirms
      a STOP on a SELECT bus, and with an abort, a bus exit or HALT (§4.1). The line is undriven
      from the next cycle. A step cut by a STOP drives none of its later bits.
    - A release in the cycle a timed step starts wins: the detector runs at the end of the cycle
      (§4.7), so that step drives nothing.
    - The SELECT master runs no detector, so it may not own a gated line (`S-line`).
  - Before RUN, every pin is an input. In `s0` (§4.0) each non-gated PP line the engine's role owns is
    driven to `idle`, so the drive is on the wire from `now = 1`, like any drive; every other line stays
    released. At `now = 0` a line reads what the pin reads: an owned idle-0 line with a pull-up reads 1,
    and every engine of that RUN, its owner too, sees that 1 at `now = 2` and `idle` from `now = 3` (§4.1).
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
  - the bus: `bus_kind` (`NONE 0`, `I2C 1`, `SELECT 2`, `SE0 3` [P]), `bus_a`, `bus_b` (lines),
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
- **Every reserved code is ILLEGAL**, not only opcodes: a reserved `sub` (ALU 10–15, BR 5–15,
  RAW 6–15, CRC 3–15, FZ 3–15, PROF ≠ 7), `src` 4–7, `dst` 1 and 3–7, step owner 3, and an
  unbuilt index (COND/HOLD line ≥ 8, XFER lane ≥ 4, profile ≥ 8). Unused bits are ignored. The
  compiler never emits one; `emu.isa.reserved()` is the list.
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
    and presents address 0. `r0..r7` keep their values (§1).
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
    have fixed totals (§4.6). `ROLE_IF` costs `U`
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
  rises: the engine sees `idle` at `now` 0–1 and the wire from `now = 2` (as it was at `now = 0`).
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
    the followers safe. A clock owner's clocked XFER re-bases by its own rule (above) instead,
    whoever owns the data.
    - A POP stall lasts until the next timed step: an owned one re-bases to its start; a followed
      one takes its timing from the other party, and the stall re-bases nothing later.
    - A `src = FIFO` step's start is the cycle it takes the word.
- **Window accumulation.** Each follower keeps an anchor and the accumulated `(min, nom, max)`
  of the HOLD/XFER bits since it. These are 24-bit, and `max` carries an INF flag.
  - An edge check compares `e − anchor` against `[min − QUANT, max + QUANT]`.
  - Sums past `2^22` are outside the spec: the compiler rejects `A > 2^22` (§6), and a CHK
    result there may differ between the model and the RTL.
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
  - Both release OD and gated lines (§2), set `tb := now` and go to `abort_pc` (§4.0).
  - A partial XFER writes no `dst` and pushes nothing.
  - Only the failing role jumps (`T5-abort`). `abort_pc` is 0 unless set; the compiler warns on
    ABORT without it.
- **Event register `evt`** (sticky; it keeps its bits across RUN, and reset and SOFT_RESET clear it): bit 0 START, 1 STOP (these two are the bus flags of §4.7), 2 TIMEOUT, 3 MISMATCH (LEVEL / EDGE /
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
| ILLEGAL | a reserved code (§3), `FZ` on engine 1 | the engine halts in the instruction's start cycle | same |

- An edge and a timeout in the same cycle: the edge wins.
- A clocked follower's timeout counts cycles from the step start for the first edge, and from
  the cycle it detected the last edge after that: TIMEOUT when `now − ref > max + QUANT`. A
  timeout without ABORT re-anchors at its new `tb` (`now`).
- **HALT** (and ILLEGAL): the engine stops. PP lines keep their level, OD and gated lines are released,
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
  `0xFFFF`; an abort writes nothing. `rd` is tainted.
- **HOLD do:** at `max(now, tb)` drive `level`, then `tb := tb + nom`. The timeline doesn't move
  inside the issue slot. Completes that cycle.
- **HOLD follow:** sample at `tb + nom//2 + EDGE_LAT`. A mismatch logs LEVEL (and aborts with
  ABORT). Then `tb := tb + nom`.

### 4.3 `XFER n, lane, owner, src, dst, msb, prof`
- **`dst` = the wire value, in every role.** The owner reads its own bits back at the point a
  follower in its position samples them:
  - on an OD line, someone else's 0 shows up, and the owner completes on its last readback. On a
    clocked lane the readback is at the capture points: the clock owner's at capture + EDGE_LAT,
    a clock follower's when it sees the capture edge;
  - on a PP line it equals `src`, and the owner completes on its last bit.
  - Readback isn't a follower's sample: it adds no `tr_sample` (architecture §5).
- `dst = FIFO` pushes only in follow mode. The owner takes `src` at step start.
- **Empty `src = FIFO`:**
  - An owner that owns the lane's clock, or an unclocked lane, waits for data, checking every
    cycle (a step isn't tied to own cycles), and re-bases (§4.1; a clock owner doesn't log
    OVERRUN).
  - An owner that doesn't own the clock **sends ONES**, logs UNDERRUN and consumes nothing. It
    checks at the step start, so the whole word is ONES.
- Bit `k` is `value >> k` (LSB first) or `value >> (n−1−k)` (MSB).
- **Unclocked lane** (`P = nom`):
  - Do: bit `k` at `max(now, tb + k·P)`, then `tb := tb + n·P`.
  - Follow: sample bit `k` at `a + (k − ak)·P + P//2 + EDGE_LAT`, from the anchor `(a, ak)`
    (initially `(tb, 0)`).
  - At most one sample per cycle: bit `k` is sampled at the later of its planned cycle and one
    cycle after bit `k − 1`. A late sample (after an OVERRUN) logs `OVERRUN(now − planned)`.
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
  - The clock owner (not MON) makes the edges, then `tb := tb + 2nH`. Every other engine tells
    the edges apart by level: a leading edge is the clock going to `1−cpol` (`S-lane` makes the
    clock idle at `cpol`). A clock follower counts from its first leading edge: an edge to `cpol`
    before it is the clock settling to its idle level (an idle-0 clock's start-up drive, §2)
    and is ignored.
  - **Launch:** a data owner that owns the clock launches at the edge time + `ldelay`. Any other
    data owner launches at `max(detect, e + ldelay)`, where `e = detect − EDGE_LAT`. It never
    logs OVERRUN, its `cpha = 0` bit 0 included: T3 bounds how late that can be.
  - **Sample:** a follower that owns the clock samples at `capture_k + EDGE_LAT`. Any other
    follower samples in the cycle it detects the capture edge.
  - Clock followers complete on the `n`-th trailing edge, with `tb := detect − EDGE_LAT`.
    - CHK logs `CLK_PHASE(m)` when `m`, the cycles between two edges it detects in the step, is
      below `min − QUANT`. The first edge has no phase to check.
    - With `max < INF`, an edge missing for `max + QUANT` logs TIMEOUT (abort with ABORT).
  - The clock owner completes at the later of its last trailing edge and its last sample.
- **Duplex** (clocked, with `data2`): each data line follows the rules above with its own owner
  (from the line config). An engine sends its own `src` on the data line it owns and samples the
  other, so the clock owner of a duplex step always follows a data line and completes at its last
  sample.
  - `dst := (v2 << n) | v1`, so `n ≤ 8`.
  - A `dst = FIFO` push sends that same packed value, in every engine that drives fewer than all
    of the step's data lines.
- **OD let-go.** After a clocked XFER in which it drove an OD data line `D` (also one that timed
  out after driving it):
  - the engine keeps the last bit until the lane's next launching edge as it sees it (the clock
    owner: the cycle it drives that edge; others: the cycle they detect it). That is the next
    leading edge with `cpha = 1`. With `cpha = 0` it is a trailing edge: for an XFER that ran to
    the end, its own last edge, because the next leading edge captures the next step's bit 0;
  - it releases `D` `ldelay` cycles after that edge (same rule as launch; after a stretched
    edge, from its real rise), unless it launches a new bit on `D` then. The let-go is taken the cycle after the XFER completes, so the release
    is at the later of that cycle and the time above (it matters with `cpha = 0`, whose edge is
    the step's own last one);
  - any explicit drive of `D` cancels the pending release;
  - one tracker per engine, for the lane of its last let-go, watches that lane's clock every
    cycle, whatever instruction is running, so a release can happen during later HOLD, COND or
    untimed instructions. Taking a new let-go releases at once every line the old one held that
    the new one doesn't. `R1-od-lane` allows one clocked lane with OD data per description, and
    on one lane the lane's own edges release the old let-go before a new XFER completes (except
    after a TIMEOUT before its first launching edge). So the tracker behaves as per-line ones
    would; across two lanes, a taken let-go could release SDA while SCL is high.
  - With I2C (cpol 1, cpha 1) a release never happens while SCL is high.
- **Clock stretching (lanes with `stretch`).**
  - After each release of `clk` (an edge it drives to 1) planned at `t`, the clock owner waits
    until it reads `clk = 1` at cycle `c ≥ t + EDGE_LAT`, and takes `e = c − EDGE_LAT`.
  - If `e > t`, the rest of the step and `tb` shift by `e − t`.
  - While it waits it makes no later edge and takes no sample; a sample on that release (a
    capture edge) is taken when it sees it. A launch stays tied to its launching edge: the edge's
    real drive cycle + `ldelay`, so a stretch delays it with its edge.
  - The wait is bounded by `max`, counted as the followers count: from the previous edge
    (`t − H`) in a clocked XFER, from `tb` in a COND RISE. TIMEOUT when
    `now − EDGE_LAT − ref > max + QUANT`, then ABORT, or `to` set, `tb := now` and the step
    ends.
  - RTL note: when `e > t`, add the shift to the step's offset counter; don't re-add it to
    every planned time.
  - `COND RISE clk` done on a stretch lane's clock waits the same way, and its `tb` is the real
    rise. `HOLD clk 1` there is rejected (`S-bus`): a HOLD's followers sample its midpoint
    rather than anchor on the edge, so they would keep the unstretched timeline.
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
The fuzz unit is attached to engine 0. Its algorithm is in `bus-fuzzer.md` §2–3; this is what a
program sees.

| Op | Effect | Cycles (total, `U` included) |
|---|---|---|
| `fz.next rd` | pick and mutate a seed into staging, rewind the read pointer, arm; `rd :=` the length (1–32) | 48 |
| `fz.tok ra` | queue `ra[7:0]` as a token; nothing while disarmed | 2 |
| `fz.end rd` | finish the fingerprint; `rd := 1` if it is new, else 0; disarm. While disarmed: `rd := 0` | 40 |

- The cycles are fixed whatever the outcome, and the compiler counts them as untimed cycles (§6).
- `FZ` is legal only in a role block for one party, the same party everywhere (`R3-fz`). On
  engine 1 it is ILLEGAL. On engine 0 with the unit not enabled (`MEM2_MODE` ≠ 0, or
  `FZ_STATUS.ready` 0), each op takes its cycles and does nothing else, and `rd := 0`, so a
  `fz.next` length of 0 means the unit is off.
- `src = FZ` sends the next staged byte, one per XFER, taken when the step takes its source:
  an XFER with `n` < 8 sends its low `n` bits, and one with `n` > 8 sends the byte
  zero-extended. In wire mode the length comes first. It reads 0 once the message is exhausted,
  and always 0 without a unit (engine 1 included). The fuzzing party sends in every `src = FZ`
  XFER (`R3-fz`): as its owner, or as the owner of a duplex lane's data2. Any other sender
  reads 0.
- **While the unit is armed**, engine 0:
  - queues a step's tokens when it completes: what the engine received from the other party.
    - An XFER gives the bits of each data line the engine follows. On one data line that is
      `value[7:0]`, then `value[15:8]` if `n` > 8, and nothing for the line's owner. On a duplex
      lane (`dst` = `(v2 << n) | v1`), the data line's `n` bits, then data2's: so the data owner
      (an SPI controller) gets the reply on data2, and data2's owner gets the request. An XFER
      that ends with no value (a TIMEOUT without ABORT) gives none.
    - A followed COND gives `0x40 + bucket(m)`, with `m` = `tb` after the step minus `tb` before
      it, clamped to 0–0xFFFF as MEAS is (a followed edge can come before `tb` under drift), or
      0xFFFF on a TIMEOUT. A COND it does gives none.
  - sets the bit of every event kind it logs (§7, bus exits included) in the unit's event mask.
  - on a fatal abort (§4.1): the unit saves staging as a hang and disarms, and the abort takes
    `FZ_HANG_CYCLES` = 12 cycles more. A bus exit is not a hang: the unit stays armed, and the
    program ends the iteration later. Not with `fz.next` or `fz.end` between `abort_pc` and the
    first timed step there (`T5-abort`): the timing rules don't time that path yet (§6).
- While disarmed (after `fz.end` or a hang), `src = FZ` keeps reading the last staging.
- The model never loses a token. A step queues at most 2, an XFER that queues 2 takes many
  cycles, and the queue drains one a cycle (`bus-fuzzer.md` §2), so at most 3 wait at once and
  `FZ_STATUS.token_lost` stays 0. The RTL must keep up as well.

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
  - A failed confirm drops the candidate. A new candidate replaces the old one, except in the old
    one's confirm cycle: a window that held is confirmed first, then the new edge is a candidate.
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
| `R1-owner` | A timed step's owner field is the owner of its data line: COND/HOLD's line, XFER's lane `data`. It is a party, or `shared` on OD; on a SHARED OD line the owner field may be either party or `shared`. The clock and `data2` drivers come from their line owners. A lane's clock has a party owner. A duplex step has a party owner, and `data2` belongs to the other party. |
| `R1-shared` | `shared` only on OD lines |
| `R1-od-lane` | At most one clocked lane with an OD data line (`data` or `data2`) per description: the engine has one OD let-go tracker (§4.3) |
| `R1-bus` | COND START/STOP is owned by the master line's owner (I2C: the `bus_b` owner; SELECT: the `bus_a` owner). It is follow-only for SE0. |
| `R2-branch` | Outside role blocks, `BR Z/NZ` tests only clean registers: written by `XFER dst`, `LI`, `ALU` on clean inputs, or `CRC GET` of a clean state. Taints: `POP`, `RAW GET/EVT`, `COND meas`, `XFER dst` on a lane with an OD data line (`data` or `data2`; the wire can differ from what was sent), any write in a role block, and every register at program entry (until written; §1) and at `abort_pc`. The CRC state is a ninth taint slot; it's a forward dataflow analysis. **Inside a role block any branch is allowed.** `RAW JPIN` and `BR TO` are only legal in role blocks. `BR ROLE` is a role split like `ROLE_IF`: the analysis follows each role's path, and `R3-escape` applies to its target. |
| `R2-edge` | A COND FALL or RISE that its owner does comes where the wire is at the pre-edge level (1 before FALL, 0 before RISE) on every path its owner can take; START and STOP likewise on `bus_a`, and on I2C with SCL high. A COND do drives the post-edge level (§4.2), so without it there is no edge and the others wait for one. The compiler tracks every party's drive: a PP line is its owner's, an OD line the wired-AND of all (it starts released, whatever its idle), and an OD RISE needs everyone else released. A drive is known from the idle level at reset, a HOLD, a COND, an XFER from ZERO or ONES (a PP data line keeps its last bit; a clocked OD one holds it until the lane's next clock edge, the let-go), a lane's clock at its idle level after a clocked XFER, or RAW SET/REL. A gated line is released while its gate is shut (at RUN and after a SELECT STOP), and RAW SET does nothing then. A role block a party skips leaves unknown what its parties may drive in it. The paths from an abort or bus exit into `abort_pc` aren't checked yet (open, as in §6) |
| `R3-timed-in-role` | Role blocks contain no timed steps, except lead blocks |
| `R3-lead` | `lead(party)`: one party. Every timed step in it is owned by that party, or uses a lane whose clock that party owns; a bus COND START/STOP, owned by the master line's owner (`R1-bus`), is also allowed. It leaves either by its fall-through exit (the leader carries on with the next step; no timed step may lie on that path) or by a path that runs a bus COND START/STOP and then branches to `abort_pc`; no path halts. The block's CFG fall-through successor S is a timed step with ABORT, which the other roles follow speculatively. It may not have CHK, and its profile `max` must be INF. When S's events come from the leader (it clocks S, or owns S), the others wait in S for the exit's bus COND, so no exit path may complete S first: before its bus COND the leader makes fewer edges on S's clock than S needs (2n), or none on a COND S's line (which may not be `bus_a`); a COND START S needs a STOP exit, and from `abort_pc` the leader reaches S before any other START. Such an S is not a COND STOP, HOLD or unclocked XFER, which would complete without the leader. Nothing timed lies between the exit's bus COND and its branch to `abort_pc`. |
| `R3-raw-drive` | `RAW SET/REL` only in a role block without MON; on a PP line, also only if the block's mask ⊆ {line owner} |
| `R3-escape` | No branch into a role block from outside. A branch out of a role block may go only to its exit, or from a lead block to `abort_pc` (the `abort_to` target, under `R3-lead`). `abort_pc` is not inside a block body. |
| `R3-fz` | `FZ` only in a block for exactly one party, the same party everywhere; that party sends in every `XFER src = FZ` (its owner, or a duplex lane's data2 owner) |
| `S-*` | Names resolve (`S-label`); fields in range and program ≤ 512 (`S-range`); no role's path runs past the last word, by falling through it or by skipping a role block at the end, and the program isn't empty (`S-end`: the chip has no program length and would run what the SRAM holds next; the model halts there); tables fit the chip's sizes (`S-table`); `data2` only on a clocked lane (or a CODEC lane with `d2_comp`); duplex `n ≤ 8`; a clocked lane's clock line idles at `cpol` (an OD clock rests released, so at 1), and its `data`, `clk` and `data2` are distinct lines (`S-lane`); CRC config valid; unclocked ABORT only with CHK or an active bus (`S-flags`); immediate shift < 16; `PROF` p = 7; `RAW DELAY n ≥ U`; `BR ROLE` mask ≠ 0; `gated` only on PP lines with idle 1, and not on a line the SELECT master owns (`S-line`) |
| `S-bus` | I2C: `bus_a` and `bus_b` are two OD lines, used with a cpol 1, cpha 1 lane on (`bus_a`, `bus_b`). The master line (I2C `bus_b`, SELECT `bus_a`) has a party owner: only its owner runs no detector. SELECT: `bus_a` is a PP line. COND START/STOP need `bus_kind ≠ NONE` and line field 0. `stretch` needs an OD `clk`, and its clock is released with COND RISE, not HOLD 1. |

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
| `T2-window` | A bounded COND, and the accumulated CHK windows: `min ≤ nom − ceil(d·(A + nom)) − QUANT` (COND: only if `min > 0`) and (`max = INF` or) `max ≥ nom + ceil(d·(A + nom)) + QUANT`. The anchor rule: a CHK unclocked XFER at `A = 0` is rejected; drop `check` or put a timed step first. |
| `T3-latency` | Clocked, clock followers: `H − ceil(d·H) ≥ 2`. A data owner (of `data` or `data2`), or a party that may drive an OD data line of the lane (it can hold a let-go into this step), that isn't the clock owner: `H ≥ EDGE_LAT + 1 + ldelay + ceil(d·2H)`. Its bit 0 with `cpha = 0`: `H ≥ L + 1 + ceil(d·(A + H))`. A bounded clock follower: the first edge, seen at `F = (cpha 0: max(0, L_clk) + H; cpha 1: max(H, L_clk)) + EDGE_LAT`, needs `F + ceil(d·(A + H)) − L_lo ≤ max + QUANT`, where `L_clk` is the clock owner's latest start and `L_lo` the follower's earliest; and `H + ceil(d·H) ≤ max + QUANT`. |
| `T4-profile` | `0 ≤ min ≤ nom ≤ max ≤ INF`. `nom ≥ 1` for HOLD and XFER. `H ≥ 2`. Stretch lanes: `H ≥ EDGE_LAT + 2`. A lane with an `ldelay`: `ldelay ≤ H − EDGE_LAT − 2`. |
| `T5-watch` | `L + ceil(d·(A + obs)) ≤ obs + EDGE_LAT`, where `obs` is the owner's planned offset of the first observable event: `nom` (COND), `H` (clocked XFER first edge), `nom//2` (HOLD / unclocked XFER first sample). A followed COND START/STOP adds `bus_confirm + 1` to the right side: the detector runs every cycle and its flag is sticky, so the follower only has to be waiting by the confirm. A COND START/STOP right after another (no timed step between) needs `nom − ceil(d·nom) − QUANT ≥ bus_confirm`: an edge inside the previous one's confirm window would replace its candidate (§4.7) |
| `T5-overrun` | Owner start ≤ planned offset if it is > 0, or ≤ `ISSUE` if it is 0. Planned offset: `nom` for COND, 0 for HOLD and unclocked XFER. Clocked XFER is exempt (§4.1). |
| `T5-timeout` | A bounded COND without ABORT is followed only by an unbounded COND |
| `T5-abort` | The first timed step a role can reach from `abort_pc` is an unbounded COND that it follows, or a COND START/STOP it does (on a bus the others exit on it; any other edge of its own could land mid-frame). On a bus it has ABORT only if it is a followed COND START/STOP: a bus exit leaves its flag set, so any other ABORT step there would exit again at once, forever. No `fz.next` or `fz.end` lies between `abort_pc` and that step for a role that can abort or exit: the timing rules don't time the path from an abort or a bus exit into `abort_pc` (open), and their 40–48 cycles could miss the re-sync. |
| `T5-stall` | After an owner's possible stall (`POP`), its next timed step on every path must be its own and make the others wait for it: an unbounded COND it does, an unbounded clocked XFER it clocks (whoever owns the data), or a step inside a lead block whose successor is one of those for it (the others wait in it). MON owns no step, so nothing may follow a POP in MON. An owned `src = FIFO` XFER waits inside the step, so it must itself be one of those: on an unclocked lane, only inside a lead block |
| `T5-startup` | Instruction 0 is not a COND RISE or a clocked XFER on an idle-0 line or clock, nor a COND FALL on an idle-0 PP line. It starts at `now = 2`, when every engine sees the line's start-up 1 (§2) as a rise and a PP one falling back to idle at `now = 3`, and would take that edge as the other party's: a clock follower miscounts its bits, with no violation. An untimed instruction first (2 cycles) avoids it |

- Completion offsets relative to the step's end `tb`:
  - COND: do 0, follow `E` (START/STOP: `E + bus_confirm + 1`).
  - HOLD: do `−nom + max(0, L)` (it drives when it starts, at most `ISSUE` late), follow
    `−nom + nom//2 + E`.
  - Unclocked XFER: do `−P` (`n = 1`: `−P + max(0, L)`, as HOLD), follow `−P + P//2 + E`.
  - Clocked XFER: clock follower `E`; clock owner with the data 0; clock owner following the
    data `max(0, E − H)` (cpha 0) or `E` (cpha 1). On a stretch lane whose last edge is a release
    (`cpol = 1`), the clock owner completes when it sees it: at least `E`.
  - **OD owners** read their data back, so they complete at the follow offsets: unclocked
    `−P + P//2 + E`, clocked at the last capture `+ E`. PP owners don't sample.
  - A COND RISE done on a stretch clock: `+E`. A timed-out followed COND:
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
plus every instruction start, event, MARK, sample point and branch outcome (every conditional
decision: BR Z/NZ/TO/ROLE, RAW JPIN and ROLE_IF, with its cycle). Co-simulation
compares each of them against the RTL trace port (architecture §5).

## 8. History
The v0 text (review notes, the Task 7 audit, the mapping from the PIO-style team draft) is in git
history: `isa-v0.md` at `de24585`.
