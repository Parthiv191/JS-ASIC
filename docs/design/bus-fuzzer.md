# Learning bus fuzzer ("AFL for wires")

Status: design, r3 (2026-11-03). Built model-first (Phase 3), then driven from a host on the
FPGA (Phase 5), then in RTL (Phase 6). The reference model's `FuzzUnit` is the golden model
for the RTL. Team requirement: `protocol_emulator_ISA(1).xlsx`, features sheet,
"learning bus fuzzer".

r3 changes: storage mapped onto the SRAM macro that passed CI on the CMOS5L template (§2.1),
the fixed cycle costs re-checked against that macro (§3.1), one fuzz unit with one owner
(§2, §3), and the open items this left (§5). Nothing is built yet: no `FuzzUnit`, no `FZ`
execution and no RTL. Only the encodings exist (`FZ = 14`, `src = FZ`).

## 1. Idea

Send slightly changed messages to a device. Fingerprint how it responds. Keep only the
messages that produced a fingerprint never seen before, and mutate those next. Over many
iterations the saved messages climb into behaviour random guessing almost never reaches,
such as multi-step unlock sequences, hidden registers, or commands that hang the device.

Roles make it protocol-generic:

- **Driver.** An engine in the controller role sends the mutated payload. The protocol
  description supplies legal framing, so mutations reach the device's logic instead of
  being rejected as noise.
- **Observer.** That engine's followed steps, or a monitor engine, turn the response into
  fingerprint tokens: reply data, ACK/NACK bits, timeouts, and the monitor's timing-window
  violations. Timing goes in, not just bytes.
- **Either side.** In the target role the chip impersonates a device and mutates its own
  replies, to fuzz a host's driver code. The fingerprint then covers what the host does next.

## 2. Blocks (one fuzz unit, attached to one engine)

There is one fuzz unit, wired to one engine. Its SRAM does one access per cycle, so only one
role may drive it: every `FZ` sits in a role block for a single party (R3-fz, §3), and the
host gives that party's role to the engine that has the unit.

| Block | Default size | Behaviour |
|---|---|---|
| Corpus | `slots` × 32 bytes, plus length and a 4-bit energy per slot | The saved interesting messages, in SRAM. The model's default is 32 slots; the chip's count follows the macro split (§2.1): 28 or 12. |
| Seed picker | – | Energy-weighted rejection sampling: draw a slot from the LFSR, keep it with probability `energy/16`, at most 4 draws (the 4th is kept). Bounded cost, no scan of all slots. |
| LFSR | 16-bit Galois, polynomial `0xB400` | The only randomness, seeded by the host, so a run is reproducible. |
| Mutator | – | Applied while the seed is copied into staging: at most one structural op (DELETE, DUP, or SPLICE = tail from another slot), then 1–4 point ops (FLIP one bit, INTERESTING 0x00 0x01 0x7F 0x80 0xFF, ADD ±1..4). Positions only where the host's per-byte mutable mask allows. |
| Staging buffer | 32 bytes | An exact copy of what was sent, saved if it turns out interesting. `XFER src=FZ` reads it byte by byte. |
| Fingerprint | CRC-16 state | Folds tokens. Shares the engine CRC unit's datapath with its own state register. |
| Coverage bitmap | 1024 bits | Indexed by `fingerprint mod 1024`; test-and-set. In SRAM. |
| Findings log | 8 entries | Hang and new-behaviour records, readable by the host. Each entry has a kind, the slot and the iteration. |

### 2.1 Storage on the chip

The macro is `RM_IHPSG13_1P_256x16_c2_bm_bist`: 256 × 16 bits (512 bytes), single port,
one access per cycle, read data one cycle after the address and held until the next read.
CI run 41584775180 hardened two of them on the CMOS5L template and passed precheck and
gate-level tests (`src/prog_mem.v`). The power grid allows at most **4 macros on the 6x4
die**. The fourth is in a slot under the Tiny Tapeout pins that is not yet tested; without
it the limit is 3 (plan, Global Constraints). Each engine on SRAM needs 2 macros, because
it fetches a 32-bit word every cycle. How the macros are split between engines and the
fuzzer is open decision D1 in the plan; A and B need all 4.

| Split | Fuzzer macros | Memory map |
|---|---|---|
| **A** (recommended) | 2, paired as one 256 × 32 memory like `src/prog_mem.v` | 28 slots × 32 B = 896 B, plus the bitmap 128 B = 1024 B |
| **B** | 0 | No on-chip unit: the host-driven loop of Phase 5 only |
| **C** | 1, 256 × 16 | 12 slots × 32 B = 384 B, plus the bitmap 128 B = 512 B |
| **D** | More than 2 | Needs a PDN script that lets macros stack, which raises the cap of 4. Unproven. |

In flops, outside the macros: the staging buffer (256 bits), the slot metadata (6-bit
length and 4-bit energy per slot: 280 bits under A), the findings log, the LFSR, the
fingerprint state and the pointers. That is about 800 flops. The cycle budgets in §3.1
assume the metadata and the staging buffer are in flops.

## 3. Instructions (opcode `FZ = 14`, sub-op in `prof`)

All three must sit inside a role block for exactly one party, the same party for every `FZ`
in the description (rule R3-fz). There is one unit, and only the fuzzing role may run it
(§2). Cycle costs are fixed, whatever the length or outcome, so the compiler's timing rules
can count them as untimed cycles. The hardware pads every op to its fixed cost.

| Sub-op | Behaviour | Cycles |
|---|---|---|
| `fz.next rd` | Pick a slot, mutate it into staging, rewind the staging read pointer, reset the fingerprint, arm token capture. `rd := length`. | `FZ_NEXT_CYCLES = 48` |
| `fz.tok ra` | Fold `ra[7:0]` into the fingerprint. | 1 |
| `fz.end rd` | Finish the fingerprint and test-and-set its bitmap bit. New: copy staging into an empty or the lowest-energy slot at full energy, log a finding, raise the IRQ. Not new: decrement the parent slot's energy (floor 1). `rd := new`. Disarms capture. | `FZ_END_CYCLES = 40` |

### 3.1 Cycle budgets against the macro

Upper bounds for the worst case, counted with every stage running one after another and no
overlap. One macro access per cycle, plus one cycle of read latency per burst.

| `fz.next` stage | A (32-bit pair) | C (one 16-bit macro) |
|---|---|---|
| Seed pick: up to 4 draws, energy read from flops | 4 | 4 |
| Copy the seed into staging (one slot, + 1 latency) | 9 | 17 |
| SPLICE: draw a second slot and read its tail (+ 1 latency) | 10 | 18 |
| DELETE or DUP: one shift of the staging flops | 1 | 1 |
| 1–4 point ops in staging | 4 | 4 |
| Rewind, reset the fingerprint, arm, write `rd` | 1 | 1 |
| **Total (budget `FZ_NEXT_CYCLES = 48`)** | **29** | **45** |

| `fz.end` stage | A | C |
|---|---|---|
| Finish the fingerprint | 1 | 1 |
| Bitmap test-and-set: read, data, write | 3 | 3 |
| New: pick an empty or the lowest-energy slot | 2 | 2 |
| New: write staging into the slot | 8 | 16 |
| Metadata, findings log, IRQ (or, if not new, the parent's energy) | 1 | 1 |
| Disarm, write `rd` | 1 | 1 |
| **Total (budget `FZ_END_CYCLES = 40`)** | **16** | **24** |

Both constants stand under A and C, with three requirements on the RTL:

- **Lowest-energy slot.** Use a comparator tree over the energy flops, or keep the minimum
  as energies change. A sequential scan of 28 slots makes `fz.end` 42 cycles under A, over
  budget.
- **Masked positions.** Each point op picks its position in bounded time, for example one
  draw and a skip if the byte is masked. Rejection sampling until an unmasked byte comes up
  has no bound.
- **C has little margin.** Under C, `fz.next` has 3 cycles to spare.

The payload reaches the wire through `XFER src=FZ`, which takes the next staged byte (0 once
the stage is exhausted). The 4-deep TX FIFO never holds the message.

**Length.** `rd` is written inside a role block, so it is tainted, and a description may not
loop on it outside the block (R2). Two modes, set by the host:

- `fixed` (default): every message has the host-set length. The structural ops keep that
  length: DELETE shifts the tail left and appends 0x00, DUP repeats a byte and drops the last,
  SPLICE keeps the position.
- `wire`: `src=FZ` returns the length first, then the bytes. The description must send it as a
  count field with `XFER dst=rN`, so every role learns the count from the wire and may loop on
  it. Structural ops then change the length within 1..32.

CHOICE (Phase 4) removes this restriction.

**Auto-tokens.** While armed, the engine folds these bytes into the fingerprint as its steps
complete. `bucket(m) = min(15, floor(log2(m + 1)))`, so cycle jitter does not look like new
behaviour.

| Event | Tokens |
|---|---|
| Followed XFER | `value[7:0]`, then `value[15:8]` if `n > 8` |
| Followed COND (response latency) | `0x40 + bucket(m)` |
| Violation (`EARLY TIMEOUT LEVEL EDGE CLK_PHASE OVERRUN`) | `0x80 + kind`, then `bucket(measured)` |

An `ABORT` while armed logs a **hang** finding with the staging contents.

## 4. Iteration

```
loop:  reset prefix                      ; put the device in a known state (description steps, or RAW on a reset pin in only(ctrl))
       only(ctrl): fz.next r0            ; mutate a seed into staging
       ... transaction, xfer src=fz ...  ; owned steps send the staged bytes, followed steps auto-tokenize the reply
       only(ctrl): fz.end r1
       br loop
```

## 5. Known limits

- **Stateful devices.** A device that remembers earlier messages makes fingerprints
  nondeterministic unless every iteration starts with a reset prefix.
- **Saturation.** A 1024-bit bitmap fills up, and slots get evicted. The host reads out,
  clears and reseeds between runs.
- **Single messages only.** Each input is one message. Message sequences need larger slots
  (stretch).
- **Where on-chip pays off.** On slow buses (UART, I2C) a host-driven loop is almost as fast.
  On-chip wins on fast buses and standalone runs.
- **Smaller corpus on the chip.** 28 slots under A, 12 under C, against the model's 32.
  Task 22 has to show that learning still beats the baseline at the chip's slot count.

Open items for Task 19:

- **Hang findings.** §3 says a hang is logged with the staging contents, but a log entry (§2)
  holds only the kind, slot and iteration, with no room for 32 bytes. Either save staging into
  a slot, as for new behaviour, or have the host read staging before the next `fz.next`.

## 6. Build stages

1. **Model (Phase 3).** `emu/model/fuzzunit.py` plus the `FZ` ops in the engine, against
   emulated devices with planted hidden behaviour. It also measures the learning loop
   against a no-feedback baseline.
2. **Host-driven (Phase 5).** The same loop, with the host doing seed scheduling over the
   host link, on the Basys 3.
3. **RTL (Phase 6).** The fuzz unit in Verilog, diffed against the model, with storage as in
   §2.1. The template's support for the macro is confirmed (CI run 41584775180). The earlier
   estimate of 1–2K logic cells predates §2.1 and leaves out the roughly 800 flops of state.
   Phase 6 synthesizes the unit to measure it. This is third on the plan's scope-cut list;
   if it is cut, the fuzzer ships host-driven (stage 2).
