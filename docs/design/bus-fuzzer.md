# Learning bus fuzzer ("AFL for wires")

Status: design (2026-11-05), owner: devOps role. The FZ ops' semantics and cycle costs (§3) are
program-visible, so changes to them need the verification role's OK.
- Storage is on SRAM column 2, and the fingerprint has its own CRC unit.
- Hang inputs are saved and pinned at the abort; costs are totals; determinism rules are added.
- 2026-11-11: the stage 1 model exists (`toolchain/emu/fuzz/`, §6). Its `FuzzUnit` is the golden
  model for the RTL. The model changed the LFSR to 32 bits, with 16 steps per draw (§3.2), and
  fixed the readings in §3.2.

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
| LFSR | 32-bit Galois, poly `0x80200003`, 16 steps per draw | the only randomness, seeded by the host (16-bit LFSR_SEED, loaded as `{seed, ~seed}`), so a run is reproducible |
| Mutator | – | Applied while the seed is copied into staging: at most one structural op (DELETE, DUP, or SPLICE = a tail from another slot), then 1–4 point ops (FLIP one bit, INTERESTING 0x00 0x01 0x7F 0x80 0xFF, ADD ±1..4, random byte). Positions only where the host's per-byte mask (32 bits) allows. |
| Staging | 32 B, flops | an exact copy of what was sent; `XFER src=FZ` reads it byte by byte |
| Fingerprint | its **own** CRC-16 (CCITT poly, init 0xFFFF), 8 bits per cycle. It's fed by a 4-entry data-token queue and an 11-bit per-iteration **event mask** | it doesn't use the engine's CRC unit, so program `CRC FEED`s never collide with tokens |
| Coverage bitmap | 8192 bits in SRAM (host-set 1K–8K) | indexed by `fingerprint mod size`, test-and-set |
| Findings log | 2 entries × 32 bits, plus FINDINGS_COUNT | kind (new / hang / hang not saved), slot, iteration. Each finding raises the IRQ and is printed on REPORT (`architecture.md` §7), so 2 entries are enough. |

### 2.1 Storage

**Column 2** is one `RM_IHPSG13_1P_512x32_c2_bm_bist` (2 KB, one access per cycle, read data
one cycle later). Its layout passed precheck and the GL test on 6x4 (run 41994812789).

| Region | Size | Notes |
|---|---|---|
| Corpus | 32 × 32 B = 1 KB | 8 words per slot |
| Bitmap | 8192 bits = 1 KB | lost fingerprints ≈ n/2m (n = distinct behaviours, m = bitmap bits): ~0.6% at n = 100 |

Why 32 slots: the metadata and the lowest-energy comparator tree are flops and grow with the
slot count. 32 matches the model, where the learning experiments run.

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
16 times (in RTL, one fixed XOR network per cycle) and uses the new state's low 16 bits `d`. `pos(d) = ((d & 0xFF) · len) >> 8`, where `len` is the
message length.

| Step | Draws |
|---|---|
| 1. Seed pick, up to 4 rounds | draw → `slot = d & 31`; draw → keep if `(d & 15) < energy[slot]` (an empty slot has energy 0). Round 4 takes only its slot draw (no keep draw) and keeps that slot; an empty slot gives an all-zero message of FZ_LEN. |
| 2. Structural op | draw → `d & 3`: 0 none, 1 DELETE, 2 DUP, 3 SPLICE. DELETE/DUP: draw → `pos(d)`. SPLICE: draw → donor `slot = d & 31`, draw → cut `pos(d)` |
| 3. Point-op count | draw → `(d & 3) + 1` |
| 4. Each point op | draw → type `d & 3` (0 FLIP, 1 INTERESTING, 2 ADD, 3 random byte); draw → `pos(d)`; draw → operand (FLIP: bit `d & 7`; INTERESTING: `{00,01,7F,80,FF,00,FF,01}[d & 7]`; ADD: `±((d & 3) + 1)`, minus if bit 2; random: `d & 0xFF`). A masked position makes the op a no-op, with its draws still consumed. |

### 3.2 Readings fixed by the model (2026-11-11)

| Topic | Reading |
|---|---|
| LFSR width | **32 bits, not 16.** At 16 bits, each iteration's mutation is fixed by the LFSR state at `fz.next`, and the draw stream repeats every 65 535 draws. After ~5K iterations the unit replays the same ~5 200 mutations (5 236 distinct start states in 100K iterations), so a 3-byte unlock was found in 1 of 5 seeds in 100K. At 32 bits, 5 of 5 in 30K. Cost: +16 flops. |
| Steps per draw | **16, not 1.** One Galois step only shifts the state, so with one step `d & 3`, `pos` and the operand of a point op are almost the same bits: a random byte could reach only 256 of the 1 024 (position, value) pairs at length 4, and the keep draw was a function of the slot draw. |
| Seed | LFSR_SEED stays 16 bits; the unit loads `{seed, ~seed}`. A seed of 0 is invalid. |
| Energy decay | Only a slot with energy > 1 loses energy. An empty slot stays at 0. |
| Mask, structural ops | DELETE, DUP and SPLICE are no-ops unless the mask allows every byte from their position to the end of the message, so a masked byte never changes. Their draws are still consumed. |
| Wire mode | DELETE on a 1-byte message is a no-op. DUP on a 32-byte message drops the last byte. SPLICE sets the length to `max(1, cut, donor length)`. A picked empty slot gives FZ_LEN zero bytes. |
| Fingerprint | It covers the tokens after `fz.next` (not the reset prefix), then the event mask, low byte (kinds 0–7) first. The bitmap index is its low bits. |
| COND latency `m` | `tb` after the step minus `tb` before it; on a timeout without ABORT, `0xFFFF` (bucket 15). |
| Hang | The slot is saved pinned at **energy 1**: a mutated hang input mostly hangs again, and at energy 15 hang inputs would crowd out the rest of the corpus. `fz.end` while disarmed does nothing (`rd := 0`). |
| New, but every slot pinned | The finding is logged as new with no slot. |
| Findings log | It keeps the 2 newest entries; FINDINGS_COUNT counts every finding. |
| Token queue | The stage 1 model feeds tokens in order and doesn't model the queue, so it never sets `token_lost`. That needs the FZ ops in the engine. |

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

1. **Model:** `toolchain/emu/fuzz/` plus the `FZ` ops, against emulated devices with planted
   hidden behaviour. Measure learning against a no-feedback baseline. Done for the unit and the
   devices (results below); the `FZ` ops in the engine are the verification role's next step.
2. **Host-driven:** the same algorithm with the host doing the scheduling, on the Basys 3. This
   is the fallback if stage 3 is cut (`chip-spec.md` §4).
3. **RTL:** the fuzz unit in Verilog, diffed draw-for-draw against the model.

### 6.1 Stage 1 results (2026-11-11)

**Setup** (`toolchain/emu/fuzz/`):
- **Devices** (`devices.py`): each is one description, run on the engine model. Engine 0 is the
  fuzzer (`ctrl`) and engine 1 is the device (`dev`). The bus is SPI with a ready line: per
  message byte, the controller clocks the byte out, the device toggles RDY (a bounded COND with
  ABORT on the controller), and the controller clocks the reply in.
- **Iteration:** every iteration runs on a fresh `System`, which is the reset prefix. The model
  has no FZ ops yet, so the TX FIFO stands in for `src=FZ`. The tokens are derived from engine
  0's trace by the §3 auto-token rules (the host-driven loop of stage 2). The observation is
  cached per device state path, and a test checks the cache against fresh runs. The tokens are
  identical with a 2% clock difference and phase offsets.
- **Run:** messages are 4 bytes, and the corpus starts as one all-zero seed. A run is 30K
  iterations, ~0.8 s of chip time at ~1 300 cycles each.
- **Seeds:** `0xACE1, 0x5EED, 0x1D2C, 0xBEEF, 0x7A3B`.
- **Baseline:** the same unit with feedback off. The bitmap still records, so coverage can be
  measured, but nothing is saved and no energy changes: only the seed is mutated.
- **Rerun:** `cd toolchain && python -m emu.fuzz` (~80 s). The asserting test runs with
  `FUZZ_LONG=1 pytest tests/test_fuzz_campaign.py`.

| Device | Planted behaviour | Partial feedback |
|---|---|---|
| unlock | key `A5 3C 7E` unlocks a hidden reply | in the reply (the state) |
| leak | the same key; the reply is 0 until unlocked | in the RDY latency only (the device is slower per matched byte) |
| hang | `5A` then `C3`: the device stops raising RDY, so the controller times out and aborts | the reply after `5A` |
| magic | `4B 1D` gives a hidden reply | none: the control |

Each cell is the iteration at which the planted behaviour was first reached (– means not
within 30K), then the distinct fingerprints at the end.

| Device | 0x1d2c | 0x5eed | 0x7a3b | 0xace1 | 0xbeef | Found |
|---|---|---|---|---|---|---|
| unlock, learning | 16005 / 29 | 17016 / 28 | 7063 / 28 | 9246 / 30 | 26858 / 31 | 5/5 |
| unlock, baseline | – / 5 | – / 5 | – / 5 | – / 5 | – / 5 | 0/5 |
| leak, learning | 16005 / 29 | 17016 / 28 | 7063 / 28 | 9246 / 30 | 26858 / 31 | 5/5 |
| leak, baseline | – / 5 | – / 5 | – / 5 | – / 5 | – / 5 | 0/5 |
| hang, learning | 631 / 16 | 2014 / 15 | 5947 / 15 | 3174 / 15 | 5187 / 14 | 5/5 |
| hang, baseline | – / 5 | – / 5 | – / 6 | – / 5 | – / 5 | 0/5 |
| magic, learning | – / 1 | – / 1 | – / 1 | – / 1 | – / 1 | 0/5 |
| magic, baseline | – / 1 | – / 1 | – / 1 | – / 1 | – / 1 | 0/5 |

- **Learning beats the baseline:** on every device with partial feedback, in every seed. Coverage
  is ~3–6× the baseline's, and the baseline never reaches a planted behaviour.
- **leak equals unlock, run for run.** The RDY latency splits the inputs exactly as the reply
  does, so timing feedback alone is enough (the "timing goes in" claim of §1).
- **magic:** neither finds it. Learning needs partial feedback; a 16-bit magic value with none
  is ~1 in 10⁷ per iteration either way.
- **The 16-bit LFSR** of the earlier draft, same setup: unlock 1/5 and hang 4/5 in 100K
  iterations, with coverage frozen after ~20K. This is why the LFSR is now 32 bits (§3.2).
