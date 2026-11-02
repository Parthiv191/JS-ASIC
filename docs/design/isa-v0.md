# ISA v0: cycle-level contract

Status: v0, the contract between the description compiler, the Python reference
model (`toolchain/emu/model`), the fuzzer, and the engine RTL (Phase 3+).
Source vision: `role_switching_protocol_emulator_proposal.md` §5, §8, §9.
Where the RTL and the model disagree, this document decides; where this
document is silent, the reference model decides and this document gets updated.

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
| Program | ≤ 4096 words | 12-bit branch targets. |
| `INF` | `0xFFFF` | A profile `max` of `INF` means no timeout. |

## 2. Roles, owners, lines

- Role register: `P0 = 0`, `P1 = 1`, `MON = 2`. A description names two parties; party 0 is `P0`, party 1 is `P1`.
- Step owner field: `P0 = 0`, `P1 = 1`, `SHARED = 2` (open-drain only, reserved for ARB), `NONE = 3` (untimed/raw).
- An engine **does** a step iff `role != MON and (owner == role or owner == SHARED)`; otherwise it **follows** it.
- Line config (image header): `kind ∈ {PP, OD}`, `owner ∈ {P0, P1}` (OD may also be `SHARED`), `idle ∈ {0, 1}`.
  - At reset an engine drives each PP line whose owner equals its role to `idle`; every other line is released (`Z`).
  - Undriven lines read `1` (pull-up).
  - `drive(L, v)`: PP sets the output to `v`. OD pulls low for `v = 0` and releases for `v = 1`.
  - Two drivers on a PP line is **contention**. The compiler's rules make it impossible; the model reports it if it happens anyway.
- Lane config: `(data line, clock line or none, cpol, cpha)`. The **clock owner** of a lane is the owner of its clock line.

## 3. Instruction word

`[31:27] op · [26:25] owner · [24:21] pin · [20:17] prof · [16:5] operand(12) · [4:0] flags`

Flags: `CHK = 1<<0` (log window violations), `RESYNC = 1<<1` (re-anchor on in-window edges), `ABORT = 1<<2` (jump to the abort vector on a fatal follow failure), `REG = 1<<3` (ALU second operand is a register).

| op | code | owner | pin | prof | operand | flags |
|---|---|---|---|---|---|---|
| `NOP` | 0 | – | – | – | – | – |
| `COND` | 1 | step owner | line | profile | `[1:0]` kind: FALL=0, RISE=1 (START=2, STOP=3 reserved for Phase 4) | ABORT |
| `HOLD` | 2 | step owner | line | profile | `[0]` level | ABORT |
| `XFER` | 3 | step owner | lane | profile | `[3:0]` n−1 · `[6:4]` src · `[9:7]` dst · `[10]` msb_first | CHK, RESYNC, ABORT |
| `LI` | 4 | – | `[1:0]` rd | – | imm12 | – |
| `ALU` | 5 | – | `[1:0]` rd · `[3:2]` ra | aluop | imm12, or `[1:0]` rb with REG | REG |
| `BR` | 6 | – | `[1:0]` ra | `[1:0]` cond: ALWAYS=0, Z=1, NZ=2 | target | – |
| `ROLE_IF` | 7 | – | – | – | `[2:0]` role mask (bit0 P0, bit1 P1, bit2 MON) · `[11:3]` count | – |
| `PUSH` | 8 | – | `[1:0]` ra | – | – | – |
| `POP` | 9 | – | `[1:0]` rd | – | – | – |
| `MARK` | 10 | – | – | – | id | – |
| `RAW` | 11 | – | line | rawop: SET=0, REL=1, GET=2, DELAY=3, JPIN=4 | SET: `[0]` level · GET: `[1:0]` rd · DELAY: n · JPIN: `[11]` level, `[10:0]` target | – |
| `HALT` | 12 | – | – | – | – | – |

Reserved opcodes: `CHOICE = 16`, `ARB = 17`, `WAIT_ANY = 18`, `CODEC = 19`.

`src`: ZERO=0, ONES=1, FIFO=2, r0..r3 = 4..7. `dst`: DISCARD=0, FIFO=2, r0..r3 = 4..7.
`aluop`: ADD=0, SUB=1, AND=2, OR=3, XOR=4, SHL=5, SHR=6, EQ=7, NE=8, LT=9 (comparisons write 0/1). Results wrap to 16 bits.

## 4. Execution and time

- `now` counts engine cycles from reset; it advances only on cycles where the engine's clock enable is high.
- Untimed instructions (`NOP LI ALU BR ROLE_IF PUSH POP MARK RAW HALT`) take exactly one cycle. `POP` with an empty FIFO stalls until a word arrives, then takes its cycle. `RAW DELAY n` takes `n` cycles (`n ≥ 1`). `ROLE_IF` takes one cycle and, if the role is not in the mask, skips the next `count` instructions, which then take no time.
- The engine samples its synchronized inputs every cycle, whatever it is executing. `edge(L)` means the synchronized value of `L` differs from the previous cycle's. Edges that happen while an untimed instruction is running are not latched.
- **Timeline register `tb`.** Timed steps (`COND HOLD XFER`) are scheduled against `tb`, not against instruction count, so untimed instructions between steps do not move wire timing. An owner acts at the planned time. A follower keeps `tb` as its estimate of the owner's timeline and re-anchors it whenever it observes an owner event: `tb := detect_cycle − EDGE_LAT`.
- **Issue slot and overrun.** An instruction cannot start in the cycle its predecessor completed, so a timed step may begin one cycle after its planned start (`tb`) and an action planned *at the step's start* (offset 0) then happens in that cycle. The timeline does not move: later actions stay at their planned times and `tb` advances from the planned start. Any other action that happens after its planned cycle logs `OVERRUN(now − planned)` and is performed immediately. `IDLE` steps never overrun. A follower that reaches a planned sample point late also logs `OVERRUN`.
- **Window accumulation.** Each follower keeps an anchor (cycle of the last re-anchoring event) and the accumulated `(min, nom, max)` of every HOLD/XFER bit since then. An edge check compares `e − anchor` against the accumulated `[min − QUANT, max + QUANT]`.
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

## 5. Well-formedness rules (compiler)

| Rule | Statement |
|---|---|
| `R1-owner` | Every timed step has an owner that is a party, or `shared`. A step that drives a PP line (COND/HOLD line, XFER data line) must be owned by that line's owner. A lane's clock line must have a party owner. |
| `R1-shared` | `shared` is only legal on OD lines. |
| `R2-branch` | `BR Z/NZ` may only test registers whose value is the same in every role ("clean"): written by `XFER dst`, `LI`, or `ALU` over clean inputs, outside role blocks. `POP`, `RAW GET`, and every write inside a role block taint the register. Taint is a forward dataflow over the CFG: tainted on any incoming path means tainted. `RAW JPIN` is only legal inside a role block. |
| `R3-timed-in-role` | Role blocks (`only(...)`) contain no timed steps. |
| `R3-raw-drive` | `RAW SET/REL` on a PP line only inside a role block whose mask ⊆ {line owner}. |
| `R3-escape` | No branch leaves a role block or enters one from outside. |
| `S-*` | Structure: labels resolve, names exist, field ranges, table sizes, no nested role blocks. |

## 6. Timing-compatibility rules (compiler)

`d = tolerance_ppm / 1e6` is the largest relative clock-rate difference between any two parties. `A` is the nominal number of cycles since the last guaranteed anchor, the maximum over all CFG paths. Anchors are a followed `COND` and a clocked `XFER`. HOLD and unclocked XFER add their duration. If `A` can grow without bound around a loop, the step fails `T1-unanchored`.

| Rule | Check |
|---|---|
| `T1-sample` | For every follower sample of slot `k` (HOLD: `k = 0`, `P = nom`; unclocked XFER: last bit): `ceil(d·(A + (k+1)·P)) + QUANT + 1 < P // 2`. |
| `T2-window` | For a bounded COND, and for the accumulated edge windows of `CHK` steps: `min ≤ nom − ceil(d·(A + nom)) − QUANT` and (`max = INF` or) `max ≥ nom + ceil(d·(A + nom)) + QUANT`. |
| `T3-latency` | Clocked XFER, clock followers: `H − ceil(d·H) ≥ 2`. If the data owner is not the clock owner: `H ≥ EDGE_LAT + 1 + ceil(d·2H)`. For bit 0 with `cpha = 0`: `H ≥ L + 1 + ceil(d·(A + H))`, where `L` is the cycle (relative to the step's `tb`) at which the data owner starts the step: its previous-step completion offset + 1 + the untimed cycles it runs in between. |
| `T4-profile` | `0 ≤ min ≤ nom ≤ max ≤ INF`. `nom ≥ 1` for HOLD and XFER. `H ≥ 2`. |
| `T5-watch` | A follower must already be executing a step when its first observation happens: the owner's event for COND (`start_f + ceil(d·(A+nom)) ≤ event + EDGE_LAT`), the first edge of a clocked XFER (`≤ H + EDGE_LAT`), or the first sample of HOLD / unclocked XFER (`≤ nom//2 + EDGE_LAT`). `start_r` is computed per role as the previous timed step's completion offset for that role + 1 + that role's untimed cycles in between (max over paths; an untimed loop makes it unbounded). |
| `T5-overrun` | The owner of a bounded step must start in time: `start_o ≤ max(planned_offset, 1)`, where the planned offset is `nom` for COND and 0 for HOLD and XFER. |

Completion offsets, relative to the step's end `tb` (used by T3/T5): COND do 0, follow `E`; HOLD do `−nom`, follow `−nom + nom//2 + E`; unclocked XFER do `−P`, follow `−P + P//2 + E`; clocked XFER: clock follower `E`, clock owner that owns the data 0, clock owner that follows the data `max(0, E − H)` for cpha 0 and `E` for cpha 1. Reset acts as a predecessor completing at −1 with `A = 0`.

The rules are conservative static bounds. **The fuzz loop is the arbiter:** any accepted program that the model runs, in tolerance, into a false alarm, a data mismatch or a hang is a compiler-rule bug or a model bug. Either way it becomes a regression case.

## 7. Model-only observability

The reference model also emits, per engine: a `StepRecord(pc, op, mode, value, tick)` for every completed timed step, every violation/MARK/ABORT event, every follower sample point (used for margin coverage), and every conditional branch outcome. Phase 3 decides which of these the RTL event log exposes. The co-simulation compares at step boundaries.
