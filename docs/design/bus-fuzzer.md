# Learning bus fuzzer ("AFL for wires")

Status: design r4 (2026-11-05), owner Manthan (algorithm) / Parthiv (chip integration).
- r4 puts storage on SRAM column 2 and gives the fingerprint its own folder.
- Hang inputs are saved and pinned at the abort; costs are totals; determinism rules are added.
- Nothing is built yet. The reference model's `FuzzUnit` (`emu/model/fuzzunit.py`, Task 19)
  becomes the golden model for the RTL.

## 1. Idea

Send slightly changed messages to a device and fingerprint how it responds. Keep only the
messages that produced a fingerprint never seen before, and mutate those next. This is
**corpus distillation**: the corpus only ever holds inputs that reached new behaviour. Over
many iterations the saved messages climb into behaviour random guessing almost never reaches,
such as multi-step unlock sequences, hidden registers, or commands that hang the device.

- **Driver.** Engine 0, in the controller role, sends the mutated payload. The protocol
  description supplies legal framing, so mutations reach the device's logic instead of being
  rejected as noise.
- **Observer.** Engine 0's own followed steps (the device's replies) feed the fingerprint:
  reply data, ACK/NACK bits, response latency, timeouts and timing-window violations. Timing
  goes in, not just bytes. Engine 1 may monitor and log at the same time, but its events don't
  enter the fingerprint in v1.
- **Either side.** In the target role, engine 0 impersonates a device and mutates its own
  replies, to fuzz a host's driver code.

## 2. Blocks (one fuzz unit, attached to engine 0)

Requirements: `MEM2_MODE = 0` (column 2 belongs to the fuzz unit), and a completed bitmap clear
(FZ_STATUS.ready). Every `FZ` sits in a role block for one party (`R3-fz`).

| Block | Size | Behaviour |
|---|---|---|
| Corpus | 32 slots × 32 B in SRAM, plus per-slot metadata in flops (6-bit length, 4-bit energy, pinned bit) | the saved interesting messages. A pinned slot (a hang input) is never evicted. |
| Seed picker | – | Energy-weighted rejection sampling: draw a slot, keep it with probability `energy/16`, at most 4 rounds (the 4th round's slot is kept). Bounded cost. |
| LFSR | 16-bit Galois, poly `0xB400` | the only randomness, seeded by the host, so a run is reproducible |
| Mutator | – | Applied while the seed is copied into staging: at most one structural op (DELETE, DUP, or SPLICE = a tail from another slot), then 1–4 point ops (FLIP one bit, INTERESTING 0x00 0x01 0x7F 0x80 0xFF, ADD ±1..4, random byte). Positions only where the host's per-byte mask (32 bits) allows. |
| Staging | 32 B, flops | an exact copy of what was sent; `XFER src=FZ` reads it byte by byte |
| Fingerprint | its **own** CRC-16 (CCITT poly, init 0xFFFF), 8 bits per cycle. It's fed by a 4-entry data-token queue and an 11-bit per-iteration **event mask** | it doesn't use the engine's CRC unit, so program `CRC FEED`s never collide with tokens |
| Coverage bitmap | 8192 bits in SRAM (host-set 1K–8K) | indexed by `fingerprint mod size`, test-and-set |
| Findings log | 2 entries × 32 bits, plus FINDINGS_COUNT | kind (new / hang / hang not saved), slot, iteration. Each finding raises the IRQ and is printed on REPORT (`architecture.md` §7), so 2 entries are enough. |

### 2.1 Storage

**Column 2** is one `RM_IHPSG13_1P_512x32_c2_bm_bist` (2 KB, one access per cycle, read data
one cycle later). Its layout passed precheck and the GL test (run 41994812789).

| Region | Size | Notes |
|---|---|---|
| Corpus | 32 × 32 B = 1 KB | 8 words per slot |
| Bitmap | 8192 bits = 1 KB | lost fingerprints ≈ n/2m: ~0.6% at n = 100 behaviours |

Why 32 slots: the metadata and the lowest-energy comparator tree are flops and grow with the
slot count. 32 matches the model, where Task 22's experiments run.

Flops outside the macro: staging 256, metadata 352, mask 32, findings 64, the token queue 32,
the event mask 11, plus the LFSR, fingerprint and pointers. That is **≈ 800 flops**, ~95K µm²
with logic (architecture §9).

**Setup by the host or the boot image:**
- FZ_CTRL.clear wipes the bitmap. It takes 256 cycles and is required after power-up, since
  SRAM wakes up random.
- Write seed slots into column 2 and set their SLOT_META.
- Set FZ_LEN, FZ_MODE, MASK and LFSR_SEED.
- A slot with length 0 is empty. Copying a seed (or a SPLICE tail) reads 0x00 past the source
  slot's length, so garbage SRAM never reaches the wire.

## 3. Instructions (opcode `FZ = 14`, sub-op in `sub`)

Costs are **totals** in engine cycles (they include the instruction's own `U`). They are fixed,
whatever the outcome, so the compiler counts them as untimed cycles; the hardware pads every op
to its cost.

| Sub-op | Behaviour | Cycles |
|---|---|---|
| `fz.next rd` | Pick a slot, mutate it into staging, rewind the staging read pointer, reset the fingerprint, arm token capture. `rd := length`. | `FZ_NEXT_CYCLES = 48` |
| `fz.tok ra` | Queue `ra[7:0]` as a token | 2 |
| `fz.end rd` | Drain the token queue, finish the fingerprint, test-and-set its bitmap bit. **New:** copy staging into an empty slot (lowest index) or the lowest-energy unpinned slot (lowest index on ties) at energy 15, log a finding, raise the IRQ. **Not new:** decrement the parent's energy (floor 1). `rd := new`. Disarms capture. | `FZ_END_CYCLES = 40` |
| (fatal abort while armed) | Save staging into a slot (same pick rule) and pin it, log a hang finding ("not saved" if every slot is pinned), disarm. A **bus exit is not a hang**: capture stays armed, and the program runs `fz.end` (a one-party FZ block may come before the COND at `abort_pc`). | adds `FZ_HANG_CYCLES = 12` to a fatal abort |

### 3.1 Cycle budgets (worst case, stages in sequence)

| `fz.next` stage | Cycles |
|---|---|
| Seed pick, up to 4 rounds (energy in flops) | 4 |
| Copy the seed into staging (8 words + latency) | 9 |
| SPLICE: second slot tail | 10 |
| DELETE/DUP shift | 1 |
| 1–4 point ops | 4 |
| Rewind, reset fingerprint, arm, `rd` | 1 |
| **Total (budget 48)** | **29** |

| `fz.end` stage | Cycles |
|---|---|
| Drain ≤ 4 tokens, fold the 2 event-mask bytes, finish | 7 |
| Bitmap test-and-set | 3 |
| Pick a slot | 2 |
| Write staging (8 words) | 8 |
| Metadata, findings, IRQ | 1 |
| Disarm, `rd` | 1 |
| **Total (budget 40)** | **22** |

RTL requirements:
- the lowest-energy pick uses a comparator tree (a sequential scan of 32 slots blows the budget);
- a masked point-op position makes that op a no-op (bounded time).

**Length modes** (set by the host):
- `fixed` (default): every message is FZ_LEN bytes. The structural ops keep the length:
  DELETE shifts left and appends 0x00, DUP repeats a byte and drops the last, SPLICE keeps the
  position.
- `wire`: `src=FZ` returns the length first, then the bytes. The description sends it as a
  count field with `XFER dst=rN`, so every role learns the count from the wire and may loop on
  it. Lengths stay within 1–32.
- Messages that end with a STOP or CS rise use the bus-event exits (`isa.md` §4.7). `CHOICE`
  stays reserved.

**Auto-tokens.** While armed, engine 0's completed steps queue data tokens, at most 2 per step:
XFER bytes first, then COND latency.
- If the queue overflows, the token is dropped and `FZ_STATUS.token_lost` is set. The model
  does exactly the same.
- Every event kind raised during the iteration (`isa.md` §7, bus exits included) sets its bit
  in the event mask. `fz.end` folds the mask in as 2 bytes, so *which* things went wrong counts,
  and *when* doesn't.
- `bucket(m) = min(15, floor(log2(m + 1)))`, so cycle jitter doesn't look like new behaviour.

| Step | Tokens |
|---|---|
| Followed XFER | `value[7:0]`, then `value[15:8]` if `n > 8` or the lane is duplex (packed value) |
| Followed COND (response latency) | `0x40 + bucket(m)` |

**Draw sequence (normative; the model and RTL match it draw for draw).** A *draw* steps the LFSR
once and uses the new 16-bit state `d`. `pos(d) = ((d & 0xFF) · len) >> 8`, where `len` is the
message length.

| Step | Draws |
|---|---|
| 1. Seed pick, up to 4 rounds | draw → `slot = d & 31`; draw → keep if `(d & 15) < energy[slot]` (an empty slot has energy 0). Round 4 takes only its slot draw (no keep draw) and keeps that slot; an empty slot gives an all-zero message of FZ_LEN. |
| 2. Structural op | draw → `d & 3`: 0 none, 1 DELETE, 2 DUP, 3 SPLICE. DELETE/DUP: draw → `pos(d)`. SPLICE: draw → donor `slot = d & 31`, draw → cut `pos(d)` |
| 3. Point-op count | draw → `(d & 3) + 1` |
| 4. Each point op | draw → type `d & 3` (0 FLIP, 1 INTERESTING, 2 ADD, 3 random byte); draw → `pos(d)`; draw → operand (FLIP: bit `d & 7`; INTERESTING: `{00,01,7F,80,FF,00,FF,01}[d & 7]`; ADD: `±((d & 3) + 1)`, minus if bit 2; random: `d & 0xFF`). A masked position makes the op a no-op, with its draws still consumed. |

## 4. Iteration

```
loop:  reset prefix                      ; device to a known state (steps, or RAW on a reset pin in only(ctrl))
       only(ctrl): fz.next r0            ; mutate a seed into staging
       ... transaction, xfer src=fz ...  ; owned steps send the staged bytes; followed steps tokenize the reply
       only(ctrl): fz.end r1
       br loop
```

## 5. Known limits

- **Stateful devices.** A device that remembers earlier messages makes fingerprints
  nondeterministic unless every iteration starts with a reset prefix.
- **Saturation.** The bitmap fills and slots get evicted (pinned ones never). The host or a
  standalone run reads out, clears and reseeds.
- **Single messages.** Each input is one message. Sequences need larger slots (stretch).
- **Where on-chip pays off.** On slow buses (UART, I2C) a host-driven loop is almost as fast.
  On-chip wins on fast buses and **standalone** runs, which we decided to support.

## 6. Build stages

1. **Model:** `emu/model/fuzzunit.py` plus the `FZ` ops, against emulated devices with planted
   hidden behaviour. Measure learning against a no-feedback baseline.
2. **Host-driven:** the same algorithm with the host doing the scheduling, on the Basys 3. This
   is the fallback if stage 3 is cut (`chip-spec.md` §4).
3. **RTL:** the fuzz unit in Verilog, diffed draw-for-draw against the model.
