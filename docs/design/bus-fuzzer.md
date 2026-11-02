# Learning bus fuzzer ("AFL for wires")

Status: design, r2 of the plan. Built model-first (Phase 3), then driven from a host on the
FPGA (Phase 5), then in RTL (Phase 6). The reference model's `FuzzUnit` is the golden model
for the RTL. Team requirement: `protocol_emulator_ISA(1).xlsx`, features sheet,
"learning bus fuzzer".

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

## 2. Blocks (one fuzz unit, shared by the engines)

| Block | Default size | Behaviour |
|---|---|---|
| Corpus | 32 slots × 32 bytes, plus length and a 4-bit energy per slot | The saved interesting messages. 1 KiB of SRAM; the macro choice is open (§6). |
| Seed picker | – | Energy-weighted rejection sampling: draw a slot from the LFSR, keep it with probability `energy/16`, at most 4 draws (the 4th is kept). Bounded cost, no scan of all slots. |
| LFSR | 16-bit Galois, polynomial `0xB400` | The only randomness, seeded by the host, so a run is reproducible. |
| Mutator | – | Applied while the seed is copied into staging: at most one structural op (DELETE, DUP, or SPLICE = tail from another slot), then 1–4 point ops (FLIP one bit, INTERESTING 0x00 0x01 0x7F 0x80 0xFF, ADD ±1..4). Positions only where the host's per-byte mutable mask allows. |
| Staging buffer | 32 bytes | An exact copy of what was sent, saved if it turns out interesting. `XFER src=FZ` reads it byte by byte. |
| Fingerprint | CRC-16 state | Folds tokens. Shares the engine CRC unit's datapath with its own state register. |
| Coverage bitmap | 1024 bits | Indexed by `fingerprint mod 1024`; test-and-set. In SRAM. |
| Findings log | 8 entries | Hang and new-behaviour records, readable by the host. Each entry has a kind, the slot and the iteration. |

## 3. Instructions (opcode `FZ = 14`, sub-op in `prof`)

All three must sit inside a role block (rule R3-fz), because only the fuzzing role runs the
unit. Cycle costs are fixed, whatever the length or outcome, so the compiler's timing rules
can count them as untimed cycles.

| Sub-op | Behaviour | Cycles |
|---|---|---|
| `fz.next rd` | Pick a slot, mutate it into staging, rewind the staging read pointer, reset the fingerprint, arm token capture. `rd := length`. | `FZ_NEXT_CYCLES = 48` |
| `fz.tok ra` | Fold `ra[7:0]` into the fingerprint. | 1 |
| `fz.end rd` | Finish the fingerprint and test-and-set its bitmap bit. New: copy staging into an empty or the lowest-energy slot at full energy, log a finding, raise the IRQ. Not new: decrement the parent slot's energy (floor 1). `rd := new`. Disarms capture. | `FZ_END_CYCLES = 40` |

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

## 6. Build stages

1. **Model (Phase 3).** `emu/model/fuzzunit.py` plus the `FZ` ops in the engine, against
   emulated devices with planted hidden behaviour. It also measures the learning loop
   against a no-feedback baseline.
2. **Host-driven (Phase 5).** The same loop, with the host doing seed scheduling over the
   host link, on the Basys 3.
3. **RTL (Phase 6).** The fuzz unit in Verilog with SRAM macros, diffed against the model.
   The logic is estimated at 1–2K cells; the memories are SRAM. Confirm the macros the
   CMOS5L template allows.
