# Co-simulation: model vs RTL

Status: built (2026-11-10), owner: verification role. One image runs on the Python model
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
| `replay(specs, e, glitches)` | `toolchain/emu/cosim.py` | runs the model and checks the rules below; returns the expected pulses and the stimulus |
| harness | `test/cosim/harness.py` | `cosim(dut, rep, role)`: reset, load, run, compare the pulses, then read back r0–r7 |
| tests | `test/cosim/test_cosim.py` | §4 |

The model side is plain Python, so pytest checks it without a simulator. The cocotb side imports
devOps's SPI host (`test/chip.py`) without editing it.

**Rules the harness enforces** (it raises before simulating if one is broken):
- The RTL's engine is model engine `e`, at clock `(1, 1)` and phase 0, so model tick = `now`.
  The other engines are external devices: any clock ratio or phase.
- **Time.** `uio_in` during the cycle with `now = w` is the stimulus at tick `w`. At the rising
  edge that ends cycle `w − 1` (where `tr_now` reads `w − 1`), the harness writes the value for
  `w`. Before RUN, `uio_in` holds the tick-0 value. The RTL preloads its synchronizer with each
  line's idle level (isa §4.1) and reads the pin from s0 on.
- **Start-up:** both sides make the idle drives in `s0`, on the wire from `now = 1` (isa §2), so an
  owned idle-0 line reads the pull-up's 1 at `now = 2`. Test `idle 0 start` checks it.
- **Undriven lines read 1** on both sides (the model's `PULL`). Pins with no line read 1.
- **Glitches** only pull an OD line to 0, or touch a line the RTL isn't driving at that tick
  (checked tick by tick). The chip reads its own drive inside, so a glitch over it would reach the
  model and not the RTL.
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

**Registers:** the trace port has no register writes, so after the pulses agree the harness reads
r0–r7 over the host port and compares them with the model's final registers.

## 3. Failures

A failing test reports:
- the first differing pulse, expected against observed;
- the last 5 pulses that matched;
- the source line of that `pc` (`image.debug`);
- the waveform, `test/cosim/tb.fst`.

A program the model can't run yet fails loudly (`NotImplementedError`), never silently. A known
model or RTL gap is a cocotb `expect_fail` test that must fail on exactly that gap's mismatch;
any other failure is an error `expect_fail` doesn't absorb. When the gap is fixed, the test
passes, cocotb reports that as a failure, and the mark comes off.

## 4. Tests

**The harness itself** (pytest, `toolchain/tests/test_cosim.py`). A comparator that can't fail is
worse than none:
- a planted one-cycle shift, a changed field, a dropped pulse and an extra pulse are each
  reported, at the right index;
- reordering pulses within a cycle is not reported;
- `expected()` and `stimulus()` on a small two-party model run, against values worked out by hand.

**No vacuous cases.** Every co-sim test first checks on the model that its scenario does what it
names: the event happens, the value changes, and each flag of a parametrized case changes what
is compared. Planted bugs, each caught (11-10): an RTL follower that ignores MSB, a flipped
register, a gap test failing on the wrong mismatch, the stimulus driver keyed on `running`, and an RTL
that preloads both sync flops at `s0` (hiding the start-up 1), and an RTL OD owner that stores
the bits it sent instead of the ones it read back.

**Co-sim** (cocotb, `test_cosim.py`), only features both sides have today:

| Test | RTL role | Covers |
|---|---|---|
| untimed | P0 | LI, every ALU op (results folded into one register), a BR NZ loop, MARK, RAW DELAY and GET, HALT |
| input at now 1 | P1 | `uio_in` during `now = 1` is the stimulus at tick 1 |
| idle 0 start | P0, P1 | the idle drive lands at `now = 1`: owner and follower read 1 at `now = 2`, then 0 |
| hold, cond | P0, P1 | HOLD and COND do and follow; EARLY, TIMEOUT, ABORT into `abort_pc` |
| xfer follower | P1 | unclocked XFER, every MSB/CHK/RESYNC combination, against a 2% slow sender with a glitch |
| uart frame | tx, rx, mon | a UART frame with register src/dst, peers up to 2% slow and out of phase (model clocks only run slower) |
| glitch | rx | an out-of-window edge (EDGE, data kept), a flipped sample (data 0xA0), a glitch in the start bit (LEVEL) |
| raw evt | rx | TIMEOUT and LEVEL set their `evt` bits; RAW EVT reads `evt`, then clears the masked bits |
| od readback | tx, rx | on an OD line a glitch over a released 1 reaches `dst` in every role; the owner completes on its last readback |
| time clamp | tx | after a gap of about 4.2 M cycles, `tb` trails `now` by 2^22 (slow: `COSIM_SLOW=1`) |
| illegal | P0 | a reserved code halts as ILLEGAL |
| role branches | P0 | `tr_br` pulses for ROLE_IF and RAW JPIN (both gaps in #13 were fixed in #14) |

**Not checked yet: the RTL's own pin drives** (Later 1). In an owner role (tx, HOLD and COND
do) co-sim checks the RTL's trace and registers, not its wire, so a wrong drive level, bit order
or pin on the RTL owner passes until pin comparison lands. Follower roles are checked against the
model owner's real wire.

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
COSIM_SLOW=1 COCOTB_TEST_FILTER=test_time_clamp make WAVES=0   # the clamp test, without a waveform
```

The Makefile builds `../../src/*.v`, the SRAM models in `../sram/` and `../tb.v`, with
`TOPLEVEL = tb`. Check the result with `python -m cocotb_tools.check_results results.xml`
(`make` exits 0 even when tests fail). devOps wires it into CI next to `test/`.
