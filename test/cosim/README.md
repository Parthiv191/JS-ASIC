# Co-simulation: model vs RTL

Status: design (2026-11-10), owner: verification role. One image runs on the Python model
(`toolchain/emu`) and on the RTL. Every trace-port pulse (architecture §5) is compared, with its
`(pc, now)` and fields, until the first mismatch. Layer 3 of `docs/design/verification.md`.

## 1. How it works

The RTL has one engine today, so it plays one role. The model runs the whole protocol (every
party, and MON), and the other parties' wire is **replayed** into the RTL's `uio_in` cycle by
cycle. Up to the first mismatch the two runs see the same wire, so the replay is exact there, and
the harness stops at the first mismatch anyway.

```
image ─► model System.run (all parties) ─┬─► expected pulses (the RTL's engine)
                                         └─► stimulus: the other engines' wire, per cycle
reset ─► load over host SPI ─► ROLE, RUN ─► drive uio_in by tr_now ─► observed pulses
                                                        compare(expected, observed)
```

| Part | Where | Job |
|---|---|---|
| `expected(trace, e)` | `toolchain/emu/cosim.py` | model engine `e`'s records → pulses (§2) |
| `stimulus(trace, e)` | same | per tick, the wire of every engine except `e`, with the model's glitches, mapped onto `uio_in` through `LINE.pin` |
| `compare(expected, observed)` | same | the first mismatch, with context (§3), or None |
| per-tick drives | `toolchain/emu/model/system.py` | `System.run` records each engine's drives per tick, so the wire can be rebuilt without engine `e` |
| monitor | `test/cosim/monitor.py` | samples `tb.user_project.u_eng0.tr_*` every clk into the same pulses |
| harness | `test/cosim/harness.py` | `cosim(dut, image, roles, ...)`: model run, reset, load, run, compare |
| tests | `test/cosim/test_cosim.py` | §4 |

The model side is plain Python, so pytest checks it without a simulator. The cocotb side imports
devOps's SPI host (`test/chip.py`) without editing it.

**Rules the harness enforces** (it raises before simulating if one is broken):
- The RTL's engine is model engine `e`, at clock `(1, 1)` and phase 0, so model tick = `now`.
  The other engines are external devices: any clock ratio or phase.
- **Time.** `uio_in` during the cycle with `now = w` is the stimulus at tick `w`. At the rising
  edge that ends cycle `w − 1` (where `tr_now` reads `w − 1`), the harness writes the value for
  `w`. Before RUN, `uio_in` holds the tick-0 value, which is what the synchronizer preloads.
- **Undriven lines read 1** on both sides (the model's `PULL`). Pins with no line read 1.
- **Glitches** only pull an OD line to 0, or touch a line the RTL's role never drives. The chip
  reads its own drive inside, so a glitch over it would reach the model and not the RTL.
- No PP contention in the model run, and no host words for the RTL's engine until the RTL has
  FIFOs (other engines may have them).

**Loading:** reset, then `Image.host_writes()` (the tables at 0x0400, the program at 0x1000),
ROLE at `0x2000`, RUN = 1. Registers are 0 after reset, as in the model.

## 2. Pulses

Each pulse is `(now, kind, pc, fields)`. Within one cycle, pulses sort by kind in the order
below, then by fields, so the order of signals inside a cycle doesn't matter.

| Kind | Fields | RTL | Model |
|---|---|---|---|
| issue | – | `tr_issue` | `starts` |
| step | op, do, value, tb | `tr_step` with `tr_op`, `tr_do`, `tr_value`, `tr_tb` | `records` (op COND 1, HOLD 2, XFER 3; `value` None = 0) |
| evt | kind code (isa §7) | each set bit of `tr_evt` | `events`, except MARK |
| mark | id | `tr_mark` with `tr_value` | `events` of kind MARK |
| br | taken | `tr_br` with `tr_taken` | `branches` |
| sample | line | `tr_sample` with `tr_line` | `samples` |
| halt | – | the first cycle `tr_halted` reads 1. Time freezes at the halt, so its `tr_now` is the HALT's start cycle. | `halt_cycle` |

`pc` is `tr_pc` in the pulse's cycle. Times compare modulo 2^24.

**End:** the model runs until it is quiescent. The RTL runs until `tr_now` passes the model's
last tick + 64, or until it halts + 64 clk. A pulse on one side only is a mismatch, so extra
RTL activity after the model goes quiet is caught.

## 3. Failures

A failing test reports:
- the first differing pulse, expected against observed;
- the last 5 pulses that matched;
- the source line of that `pc` (`image.debug`);
- the waveform, `test/cosim/tb.fst`.

A program the model can't run yet fails loudly (`NotImplementedError`), never silently. A known
model or RTL gap is a cocotb `expect_fail` test naming the gap. When the gap is fixed, the test
passes, cocotb reports that as a failure, and the mark comes off.

## 4. Tests

**The harness itself** (pytest, `toolchain/tests/test_cosim.py`). A comparator that can't fail is
worse than none:
- a planted one-cycle shift, a changed field, a dropped pulse and an extra pulse are each
  reported, at the right index;
- reordering pulses within a cycle is not reported;
- `expected()` and `stimulus()` on a small two-party model run, against values worked out by hand.

**Co-sim** (cocotb, `test_cosim.py`), only features both sides have today:

| Test | RTL role | Covers |
|---|---|---|
| untimed | P0 | LI, ALU, BR loops, ROLE_IF, MARK, RAW, HALT |
| hold, cond | P0, P1 | HOLD and COND do and follow; EARLY, TIMEOUT, ABORT into `abort_pc` |
| xfer | P0, P1 | unclocked XFER owner and follower; CHK, RESYNC, MSB |
| uart frame | tx, rx, mon | a UART frame with register src/dst, peers at ±2% drift |
| glitch | rx | a glitch in a data bit (LEVEL or EDGE) |
| illegal | P0 | a reserved code halts as ILLEGAL: `expect_fail` until devOps's #13 fix |

**Later**, each its own PR:
1. Per-cycle pin comparison (`uio_out`, `uio_oe` against the model's drives), which also runs on
   the gate-level netlist in CI's `gl_test`.
2. The UART protocol program in every role, once the RTL and the model (step 3b) have FIFOs.
3. Engine 1: both engines on-chip with LOOPBACK, against a two-engine model.
4. Random programs from the compiler, with the shrinker (verification.md layer 4).
5. SPI and I2C as the model gains clocked XFER and START/STOP.

## 5. Running

```sh
cd test/cosim && make
```

The Makefile builds `../../src/*.v`, the SRAM models in `../sram/` and `../tb.v`, with
`TOPLEVEL = tb`. devOps wires it into CI next to `test/`.
