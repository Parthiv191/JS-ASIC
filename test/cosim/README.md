# Co-simulation: model vs RTL

Status: built (2026-11-10), owner: verification role. One image runs on the Python model
(`toolchain/emu`) and on the chip. On RTL, every trace-port pulse (architecture §5) is compared,
with its `(pc, now)` and fields, then the pins every cycle, then r0–r7. The gate-level netlist has
no trace port, so there the pins, ACT and r0–r7 are compared. Layer 3 of `docs/design/verification.md`.

## 1. How it works

The RTL has one engine today, so it plays one role. The model runs the whole protocol (every
party, and MON), and the other parties' wire is **replayed** into the RTL's `uio_in` cycle by
cycle. Up to the first mismatch the two runs see the same wire, so the replay is exact there, and
the harness stops at the first mismatch anyway.

```
image ─► model System.run (all parties) ─┬─► expected pulses (the RTL's engine)
                                         └─► stimulus: the other engines' wire, per cycle
reset ─► load over host SPI ─► ROLE, RUN ─► drive uio_in by clk count ─► pulses (RTL), pins
                                                    compare the pulses, the pins, then r0-r7
```

| Part | Where | Job |
|---|---|---|
| `expected(trace, e)` | `toolchain/emu/cosim.py` | model engine `e`'s records → pulses (§2) |
| `stimulus(trace, e)` | same | per tick, the wire of every engine except `e`, with the model's glitches, mapped onto `uio_in` through `LINE.pin` |
| `compare(expected, observed)` | same | the first mismatch, with context (§3), or None |
| `pins(trace, e, ticks)`, `compare_pins` | same | per tick, the `(uio_oe, uio_out)` engine `e` must drive; the first tick and pins that differ (§2) |
| `act(trace, e, ticks)` | same | per tick, the ACT pin (architecture §8) |
| per-tick drives | `toolchain/emu/model/system.py` | `System.run` records each engine's drives per tick, and the drive after the run, so the wire can be rebuilt without engine `e` |
| monitor | `test/cosim/monitor.py` | every clk: records the pins and ACT, drives `uio_in`, and on RTL samples `tb.user_project.u_eng0.tr_*` into the same pulses |
| `replay(specs, e, glitches)` | `toolchain/emu/cosim.py` | runs the model and checks the rules below; returns the expected pulses and the stimulus |
| harness | `test/cosim/harness.py` | `cosim(dut, rep, role)`: reset, load, run, check the alignment, compare the pulses, the pins and ACT, then read back r0–r7 |
| tests | `test/cosim/test_cosim.py` | §4 |

The model side is plain Python, so pytest checks it without a simulator. The cocotb side imports
devOps's SPI host (`test/chip.py`) without editing it.

**Rules the harness enforces** (it raises before simulating if one is broken):
- The RTL's engine is model engine `e`, at clock `(1, 1)` and phase 0, so model tick = `now`.
  The other engines are external devices: any clock ratio or phase.
- **Time is counted in clk**, because the gate-level netlist has no `tr_now`. Edge `k`, counted
  from just before the RUN write, ends the cycle with tick `k − RUN_S0`. `RUN_S0 = 263` comes from
  `chip.py`'s SPI timing and the RTL's RUN path. Every RTL run checks it: the engine's `run` input
  first reads 1 at edge `RUN_S0` (the edge that ends s0), and `tr_now` equals the tick in every
  running cycle until HALT. Every run, gate level too, checks that ACT (`uo_out[7]`) first reads 1
  at edge `RUN_S0 + 2` (the cycle with `now = 2`). A failed check stops the test before anything
  is compared.
- **Stimulus.** `uio_in` during tick `w` is the stimulus at `w`: the harness writes it at the
  edge that ends tick `w − 1`. Before RUN, `uio_in` holds the tick-0 value. The RTL preloads its
  synchronizer with each line's idle level (isa §4.1) and reads the pin from s0 on.
- **Start-up:** both sides make the idle drives in `s0`, on the wire from `now = 1` (isa §2), so an
  owned idle-0 line reads the pull-up's 1 at `now = 2`. Test `idle 0 start` checks it.
- **Undriven lines read 1** on both sides (the model's `PULL`). Pins with no line read 1.
- **Glitches** only pull an OD line to 0, or touch a line the RTL isn't driving at that tick
  (checked tick by tick). The chip reads its own drive inside, so a glitch over it would reach the
  model and not the RTL.
- No PP contention in the model run.
- **Host words:** the model pushes each engine's host words at tick 0. For the RTL's engine the
  harness writes them to TXDATA before RUN (RUN keeps the TX FIFO), so at most 4, the TX depth.
  After the run it compares the TX and RX counts at 0x03 and reads RX out over RXDATA.

**Loading:** reset, then `Image.host_writes()` (the tables at 0x0400, the program at 0x1000),
ROLE at `0x2000`, the RTL engine's host words to TXDATA, RUN = 1. Registers are 0 after reset, as in the model.

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

**End:** the model runs until it is quiescent. The chip runs until the model's last tick + 64.
A pulse on one side only is a mismatch, so extra RTL activity after the model goes quiet is
caught.

**Pins**, every clk from just before the RUN write to the end:
- `uio_oe` of every pin: a pin is driven iff the model engine drives the line on it (PP 0 or 1, OD
  0). Before RUN every pin is an input (architecture §2); past the model's end, its last drive
  holds.
- `uio_out` of every driven pin: the level. `uio_out` of an undriven pin never reaches the wire,
  so it isn't compared.
- This is the only check of the chip's own drives. The trace port shows when a step drives, not
  which pin or what level, and nothing on it shows HALT or an abort releasing the OD lines.
- ACT (`uo_out[7]`) every cycle: 1 from `now = 2` through the model HALT's start cycle + 1
  (architecture §8). At gate level it is the only view of when the engine halts, so a follower
  test that drives no pin and writes no register still checks something there.
- `COSIM_GATES` must match the build: the harness stops if it is 1 and the design has a trace
  port, or 0 and it has none. The Makefile sets it in both modes, so a stale shell value can't
  turn off the trace checks.

**Registers:** the trace port has no register writes, so after the pulses agree the harness reads
r0–r7 over the host port and compares them with the model's final registers.

## 3. Failures

A failing test reports:
- the first differing pulse, expected against observed;
- the last 5 pulses that matched;
- the source line of that `pc` (`image.debug`);
- the waveform, `test/cosim/tb.fst`.

A pin mismatch reports the tick and each pin that differs (`driven 0`, `driven 1` or
`released`). It also names the instruction running in the cycle before, which made the drive.

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
the bits it sent instead of the ones it read back. Pin bugs that pass the trace and register
checks (11-10):

| Planted bug | Tests that fail |
|---|---|
| HALT releases the PP lines too | 9 |
| an abort keeps the OD lines | 1 (od release) |
| timed-step drives land on the neighbouring pin | 8 |
| ACT stays 1 after HALT | 36 (every test that halts) |

An OD line driven high for its 1s also fails, in the OD readback test, through the trace.

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
| od release | rx | HALT and an abort release the OD lines and keep the PP drives, with the lines on pins 5, 2 and 6. It found the unused-slot collision that #24 fixes. |
| time clamp | tx | after a gap of about 4.2 M cycles, `tb` trails `now` by 2^22 (slow: `COSIM_SLOW=1`) |
| illegal | P0 | a reserved code halts as ILLEGAL |
| role branches | P0 | `tr_br` pulses for ROLE_IF and RAW JPIN (both gaps in #13 were fixed in #14) |
| uart protocol | tx, rx, mon | the library UART (`emu/protocols/uart.py`) from the FIFOs: tx POPs its host's 4 words (the RTL's from TXDATA), rx and mon push them into RX (the RTL's read over RXDATA); peers up to 2% slow |

Every test compares the pins and ACT, at gate level too. There, illegal and role branches check
only ACT: an ILLEGAL halts 2 cycles before the HALT behind it would.

**Later**, each its own PR:
1. SPI and I2C, the library protocols (`emu/protocols`), once the RTL has clocked XFER and the bus.
2. Engine 1: both engines on-chip with LOOPBACK, against a two-engine model.
3. Random programs from the compiler, with the shrinker (verification.md layer 4).
4. The other `uo_out` pins against the model: IRQ, and ERR (any VIOL bit).
5. Host words during the run (more than 4 for the RTL's engine), timed as the model pushes them.

## 5. Running

```sh
cd test/cosim && make
COSIM_SLOW=1 COCOTB_TEST_FILTER=test_time_clamp make WAVES=0   # the clamp test, without a waveform
make GATES=yes PDK_ROOT=<pdk> GL_NETLIST=<netlist.v>            # gate level (default: ../gate_level_netlist.v)
```

- The Makefile builds devOps's `PROJECT_SOURCES`, read from `../Makefile` so co-sim builds what
  the RTL tests build, plus the SRAM models in `../sram/` and `../tb.v`, with `TOPLEVEL = tb`.
- `GATES=yes` builds the netlist with the IHP cell models, as `test/Makefile` does, and sets
  `COSIM_GATES=1` for the harness. The netlist is `tt_submission/<top>.v` from a gds run.
- **Icarus 12 leaves every IHP flop at X.** The cell models clock on the delayed nets of
  `$setuphold`, which Icarus 12 doesn't drive (even with `-gspecify`). CI uses TT's Icarus 13.
  To run gate level on Icarus 12, point `PDK_ROOT` at a copy whose `sg13cmos5l_stdcell.v` has its
  `wire delayed_…;` lines removed and `delayed_` deleted everywhere else.
- Check the result with `python -m cocotb_tools.check_results results.xml` (`make` exits 0 even
  when tests fail). devOps wires both runs into CI: RTL next to `test/`, and gate level after
  `gl_test` copies the netlist.
