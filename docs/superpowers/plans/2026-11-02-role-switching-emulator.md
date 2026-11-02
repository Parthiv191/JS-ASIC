# Role-Switching Protocol Emulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the role-switching protocol emulator from the proposal: one owner-labeled description per protocol that runs as controller, target or monitor. Verification is a coverage-guided fuzz loop whose findings feed back into the generator and the regression suite.

**Architecture:** The Python toolchain (`toolchain/emu`) is the single source of truth. It holds the ISA encoding, the description DSL, the compiler with its well-formedness and timing checks, and a cycle-level reference model of N engines on a shared bus. A fuzz campaign generates constrained-random test cases, runs every role pairing plus a monitor, checks cross-role oracles, extracts functional coverage, and feeds new coverage back into seed selection, mutation scheduling and generator choice weights. Failures are shrunk into a replayed regression corpus. Phase 3 adds the Verilog engine as a second backend for the same cases, so RTL is diffed against the model.

**Tech Stack:** Python ≥ 3.11 (no runtime deps; dev: pytest, hypothesis), Verilog RTL on the Tiny Tapeout IHP CMOS5L template, cocotb + Icarus/Verilator, SymbiYosys (Phase 4).

**Spec:** `role_switching_protocol_emulator_proposal.md` (vision) and `docs/design/isa-v0.md` (cycle-level contract derived from it; executors read both).

## Global Constraints

- Target: IHP sg13g2 CMOS5L via Tiny Tapeout, **6x4 tiles** (design for 6x4 from day one; 8x4 only if announced).
- Pins: clk, rst_n, 8 inputs, 8 outputs, 8 bidirectional. Protocol lines use the 8 bidirectional pins.
- Clock: operate at 48 MHz; harden at `CLOCK_PERIOD = 20` ns (50 MHz); 40 MHz fallback.
- Submission due **January 18, 2027**; RTL freeze around January 4, 2027.
- Terminology: "controller"/"target", never master/slave.
- Toolchain language: Python (pairs with the existing cocotb tests). RTL language: Verilog (existing repo).
- `SYNC_LAT = 2`, `EDGE_LAT = 3`, `QUANT = 1`, 8 lines, 4×16-bit registers, 16 profiles, 16 lanes (isa-v0 §1).
- The toolchain has zero runtime dependencies; dev deps are pinned in `toolchain/pyproject.toml`.
- Never cut (proposal §14.5): three-role UART, SPI and I2C; the monitor log; the formal harness for those; a clean harden.
- Fuzzing must be deterministic for a given seed so every failure is replayable.

## Review Focus

1. **Follower arrives late to a waiting step** (role-only instructions between steps). Expected: the compiler rejects it with `T5-watch`. The model must never hang silently; a hang is reported as a `completion` failure. Test: Task 4 `test_t5_rejects_long_follower_gap`.
2. **Engines reset at different phases.** Expected: no spurious edges at start (synchronizers preload the idle wire), and owners don't act before followers are ready (host data arrives after `start_tick`). Test: Task 6 `test_phase_offsets_no_spurious_edges`.
3. **Owner's FIFO empty at an owned step.** Expected: the owner stalls; a bounded step logs `OVERRUN` and never sends silent garbage. Test: Task 7 `test_xfer_fifo_stall_logs_overrun`.
4. **Clock drift exactly at the declared tolerance on the longest frame** (UART stop bit, 9.5 P from the anchor). Expected: no false alarms at the minimum P the compiler accepts. Test: Task 9 `test_uart_at_tolerance_edge_min_period`.
5. **Back-to-back frames with zero idle**, and payload extremes (0x00, 0xFF, n = 1, n = 16, MSB-first). Expected: the follower catches the next start edge, and data is exact. Test: Task 9 `test_uart_back_to_back_frames` and Task 7 `test_xfer_widths_and_order`.

---

## Phases

| Phase | Weeks (proposal §14.2) | Delivers | Exit criterion |
|---|---|---|---|
| **1. Reference model** | Nov 2–15 | ISA v0 encoding, description DSL, compiler (R1–R3, T1–T5), cycle-level multi-engine model, UART + SPI descriptions | UART and SPI pass cross-role tests in every role permutation with drift inside tolerance (pytest) |
| **2. Fuzz verification loop** | Nov 2–15, alongside Phase 1 | Test-case format, oracles, functional coverage, coverage-guided campaign (corpus energy, operator bandit, generator-weight feedback), shrinker, regression corpus + CI, random description generator (well- and ill-formed) | `python -m emu.fuzz run` reaches a stable coverage plateau on UART/SPI/random with zero open failures; regressions replay in CI |
| **3. Engine RTL v0** | Nov 9–22 | Verilog engine for the Phase 1 ISA subset, `isa_defs.vh` generated from `emu.isa`, image loader, top level replacing the hardcoded UART, Verilator backend for the fuzz runner (RTL vs model diff at step boundaries), first real harden | RTL passes the regression corpus plus N differential fuzz iterations; area/slack numbers recorded |
| **4. I2C, multi-engine, formal** | Nov 16 – Dec 6 | OD lines, internal wired-AND bus, START/STOP, clock stretching, CHOICE, 3 engines + crossbar, event log, host SPI link, I2C in all roles, SymbiYosys harness (no contention, no false alarms, lockstep, bounded completion), FPGA bring-up | I2C working on FPGA with a real sensor; first properties proven; paper-test numbers recorded (`docs/design/paper-test.md`) |
| **5. Codecs, showpiece, margin** | Dec 7–27 | CODEC (NRZI, stuffing, CRC5/16, Manchester) + codec proofs, ARB or USB-LS showpiece, bias register + margin sweep, gate-level fuzz runs | Showpiece working in sim and on FPGA |
| **6. Harden of record, submission** | Dec 28 – Jan 17 | Timing/DRC/LVS clean harden, full GL regression, host library, self-test script, writeup, demos | Submitted before Jan 18 |

Phases 3–6 get their own detailed plans when they start, because their task boundaries depend on Phase 1–2 findings and on the first harden numbers. Scope-cut order if behind (proposal §14.5): margin testing, then the showpiece, then codecs.

**Gate G0 (proposal §14.1, paper test).** The repo history shows no record of it. The UART half falls out of Task 9 (description size vs. the per-role programs it replaces). The I2C half is a Phase 4 entry criterion.

---

## File structure

```
toolchain/
  pyproject.toml                 package "emu"; dev extras pytest, hypothesis
  emu/isa.py                     enums, constants, Instr, encode/decode           (T1)
  emu/image.py                   Profile, LineCfg, LaneCfg, Image (+JSON)         (T2)
  emu/desc.py                    Description builder DSL                          (T2)
  emu/compiler.py                lowering, R/S rules, debug map                   (T3)
  emu/analysis.py                CFG, taint (R2), timing rules T1–T5              (T4)
  emu/model/bus.py               wire resolution, contention                      (T5)
  emu/model/engine.py            cycle-level interpreter (generator per engine)   (T5,T7,T8)
  emu/model/system.py            engines + bus + clocks + hosts + trace           (T6)
  emu/protocols/{__init__,uart,spi}.py   registry + descriptions                  (T9)
  emu/fuzz/case.py               TestCase (+JSON, key)                            (T10)
  emu/fuzz/run.py                run_case -> Outcome                              (T10)
  emu/fuzz/oracles.py            cross-role oracles                               (T10)
  emu/fuzz/coverage.py           bins, universe, CoverageMap                      (T11)
  emu/fuzz/gen.py                Generator + ChoiceWeights (feedback)             (T12)
  emu/fuzz/mutate.py             mutation operators                               (T13)
  emu/fuzz/shrink.py             reducer                                          (T13)
  emu/fuzz/campaign.py           the feedback loop + report                       (T14)
  emu/fuzz/__main__.py           CLI: run / replay                                (T14)
  emu/fuzz/randdesc.py           random well-/ill-formed descriptions             (T16)
  tests/test_*.py                one file per module
  tests/corpus/*.json            regression cases replayed by test_regressions.py (T15)
.github/workflows/toolchain.yaml pytest + fuzz smoke                              (T15)
```

Run all toolchain tests with `cd toolchain && python -m pytest -q`.

---

## Phase 1: Reference model

### Task 1: Toolchain skeleton and ISA encoding

**Files:** Create `toolchain/pyproject.toml`, `toolchain/emu/__init__.py`, `toolchain/emu/isa.py`, `toolchain/tests/test_isa.py`, `toolchain/README.md`.

**Interfaces — Produces:**
- Constants `SYNC_LAT=2, EDGE_LAT=3, QUANT=1, NUM_LINES=8, NUM_REGS=4, NUM_PROFILES=16, NUM_LANES=16, PROG_DEPTH=4096, INF=0xFFFF, REG_MASK=0xFFFF`.
- `IntEnum`s `Op, Owner, Role, CondKind, AluOp, BrCond, RawOp, Src, Dst` with isa-v0 §3 codes; flag ints `F_CHK, F_RESYNC, F_ABORT, F_REG`.
- `@dataclass(frozen=True) Instr(op, owner=Owner.NONE, pin=0, prof=0, operand=0, flags=0)`.
- `encode(i: Instr) -> int`, `decode(w: int) -> Instr` (ValueError on out-of-range fields or unknown opcode).
- Field helpers: `xfer_operand(n, src, dst, msb_first) -> int`, `unpack_xfer(operand) -> (n, Src, Dst, bool)`, `role_if_operand(mask, count)`, `unpack_role_if(operand)`, `role_mask(roles: Iterable[Role]) -> int`.

- [ ] **Step 1: Write failing tests**: `test_encode_pins_field_layout` asserts `encode(Instr(Op.XFER, Owner.P1, pin=3, prof=5, operand=0x7AB, flags=0b10101)) == (3<<27)|(1<<25)|(3<<21)|(5<<17)|(0x7AB<<5)|0b10101`. `test_roundtrip_property` (hypothesis over all valid fields) asserts `decode(encode(i)) == i`. `test_field_overflow_raises` covers pin=16, operand=4096, flags=32, and op code 31 → ValueError. `test_xfer_operand_roundtrip` covers n=1 and n=16, all src/dst, msb.
- [ ] **Step 2: Run** `python -m pytest tests/test_isa.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `isa.py`, plus `pyproject.toml` (setuptools, `requires-python >= 3.11`, `[project.optional-dependencies] dev = ["pytest>=8", "hypothesis>=6.100"]`, pytest `testpaths = ["tests"]`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(toolchain): ISA v0 encoding`.

### Task 2: Image and description DSL

**Files:** Create `emu/image.py`, `emu/desc.py`, `tests/test_desc.py`.

**Interfaces — Produces:**
- `Profile(name: str, min: int, nom: int, max: int)` with `bounded -> bool` (`max != INF`). `IDLE = Profile("IDLE", 0, 0, INF)` is always table entry 0.
- `LineCfg(name, kind: Literal["pp","od"], owner: Owner, idle: int)`. `LaneCfg(data: int, clk: int | None, cpol: int, cpha: int)`.
- `Image(name, parties: tuple[str,str], words: list[int], profiles: list[Profile], lines: list[LineCfg], lanes: list[LaneCfg], abort_pc: int, tolerance_ppm: int, debug: dict[int,str])` with `instrs() -> list[Instr]`, `to_json() -> str`, `Image.from_json(s)`, and `with_profiles(scale: dict[str,float]) -> Image` (used later for "sloppy device" faults).
- `Description(name, parties=("p0","p1"), tolerance_ppm=20000)`:
  - `.line(name, kind="pp", owner, idle=1) -> LineRef`, `.profile(name, nom, min=None, max=None) -> ProfRef`, `.lane(name, data, clk=None, cpol=0, cpha=0) -> LaneRef`, `.label(name)`, `.abort_to(label)`.
  - Steps: `.cond(kind, line, owner, prof=None, abort=False)`, `.hold(line, level, owner, prof, abort=False)`, `.xfer(n, lane_or_line, owner, prof, src="zero", dst="discard", msb_first=False, check=False, resync=False, abort=False)`, `.li(rd, imm)`, `.alu(op, rd, ra, b)`, `.br(target, cond="always", reg=None)`, `.push(ra)`, `.pop(rd)`, `.mark(id)`, `.halt()`, `.nop()`, `.raw_set(line, level)`, `.raw_rel(line)`, `.raw_get(rd, line)`, `.raw_delay(n)`, `.raw_jpin(line, level, target)`.
  - `with d.only(*roles):` opens a role block, where roles are party names or `"mon"`.
  - Every step records `loc` (`file:line` of the caller) and its index. Steps live in `d.steps: list[Step]`, with `Step(kind, args, owner, block: frozenset[str] | None, loc, labels)`.
- The builder raises `ValueError` immediately only for **name and type errors** (unknown party, duplicate line or label name, bad register name, nested `only`). Rule violations are left for the compiler so that ill-formed descriptions can still be built.

- [ ] **Step 1: Tests**: `test_builder_records_steps_and_locs` checks kinds, owners and that `loc` ends with `test_desc.py:<n>`. `test_only_marks_block_membership`. `test_nested_only_raises`. `test_unknown_party_raises`. `test_duplicate_label_raises`. `test_image_json_roundtrip`. `test_with_profiles_scales_nom_only` checks that a scale of 1.1 on `BIT` with nom 20 gives nom 22 and leaves min/max unchanged.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(toolchain): description DSL and image`.

### Task 3: Compiler (lowering, R1, R3, S rules, debug map)

**Files:** Create `emu/compiler.py`, `tests/test_compiler.py`.

**Interfaces:**
- Consumes: `Description`, `Image`, `isa`.
- Produces: `compile_desc(desc, *, check_timing=True) -> Image`. `CompileError(Exception)` with `.diags: list[Diagnostic]`. `Diagnostic(rule: str, index: int, loc: str, msg: str)`. `lower(desc) -> (list[Instr], Image-without-words, block_spans)` is internal but importable for `analysis.py`.
- Lowering: role blocks → `ROLE_IF(mask, count)` + body. Labels → pcs. Profiles → table indices (`prof=None` → 0). A line passed to `xfer` becomes an implicit unclocked lane (deduplicated). Party names → `Owner.P0/P1`; `"shared"` → `Owner.SHARED`. `debug[pc] = f"{loc} {kind} {args}"`.

- [ ] **Step 1: Tests**: `test_lowering_role_if_count_and_labels` (ROLE_IF count equals the body length; a BR target resolves to the label's pc). `test_r1_rejects_wrong_owner_on_pp_line`. `test_r1_rejects_shared_on_pp`. `test_r1_rejects_clock_line_without_party_owner`. `test_r3_rejects_timed_step_in_role_block`. `test_r3_rejects_raw_drive_pp_outside_owner_block`. `test_r3_rejects_branch_out_of_block`. `test_s_rejects_unresolved_label`. `test_s_rejects_17_profiles`. `test_debug_map_has_every_pc`. Each negative test asserts `{d.rule for d in e.diags} == {"<rule>"}`.
- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `feat(toolchain): compiler lowering and ownership rules`.

### Task 4: Analysis (R2 taint, T1–T5)

**Files:** Create `emu/analysis.py`, `tests/test_analysis.py`. Modify `emu/compiler.py` to call `check_branches(instrs, spans)` and, when `check_timing`, `check_timing(image, spans)`. Both return `list[Diagnostic]`.

**Interfaces — Produces:** `cfg(instrs) -> list[list[int]]` (successors; ROLE_IF has two: body and skip), `check_branches(...)`, `check_timing(...)`, and `role_gap(instrs, spans, a_pc, b_pc, role) -> int` (untimed cycles a role executes between two timed steps). Formulas are isa-v0 §6, verbatim.

- [ ] **Step 1: Tests**: `test_r2_rejects_branch_on_popped_reg`. `test_r2_rejects_branch_on_reg_written_in_role_block`. `test_r2_accepts_branch_on_xfer_dst`. `test_r2_taint_joins_over_loop` (a reg tainted on the back edge is rejected at the loop head). `test_r2_rejects_jpin_outside_block`. `test_t1_uart_period_boundary` (2% tolerance: P=10 accepted, P=8 rejected with `T1-sample`). `test_t1_rejects_unanchored_loop`. `test_t2_rejects_tight_cond_window`. `test_t3_rejects_spi_target_half_period_4` and `test_t3_accepts_6`. `test_t4_rejects_min_gt_nom`. `test_t5_rejects_long_follower_gap` (six `only("rx")` NOPs before a follower COND with nom=2 → `T5-watch`).
- [ ] **Step 2–4:** FAIL → implement → PASS. The P and H boundary values come from the formulas; if the formulas disagree with a test's number, fix the test only after re-deriving by hand. Record the derivation in the test docstring.
- [ ] **Step 5: Commit** `feat(toolchain): branch taint and timing compatibility checks`.

### Task 5: Bus and engine core (untimed + raw)

**Files:** Create `emu/model/__init__.py`, `emu/model/bus.py`, `emu/model/engine.py`, `tests/test_engine_core.py`.

**Interfaces — Produces:**
- `resolve(lines: list[LineCfg], drives: list[list[int|None]]) -> tuple[list[int], list[int]]` returns (wire values, contended wire indices). PP: 0 drivers → 1; 1 driver → its value; >1 → contention, value = AND. OD: AND with Z=1.
- `Engine(image: Image, role: Role, name="")` with attributes `pc, regs, tb, now, drv: list[int|None], inp, prev_inp, tx: deque, rx: list, records, events, samples, branches, halted, idle`, and `cycle(wire: list[int]) -> None` (one engine cycle: shift the synchronizer, update `inp/prev_inp`, resume the interpreter generator). Synchronizers preload the reset wire value.
- Records: `StepRecord(pc, op, mode: "do"|"follow", value, cycle)`, `Event(kind, pc, cycle, measured=None, lo=None, hi=None)`, `Sample(pc, line, cycle)`, `Branch(pc, taken)`.
- `idle` is True while blocked on POP/FIFO with an empty FIFO, in an IDLE-profile COND follow, or halted.

- [ ] **Step 1: Tests** (single engine, wire supplied by the test): `test_untimed_ops_take_one_cycle` (LI, ALU, BR loop: assert `now` at HALT). `test_alu_ops_wrap_16_bits`. `test_role_if_skips_per_role` (each of P0/P1/MON). `test_pop_blocks_until_push`. `test_raw_get_sees_wire_after_sync_lat`. `test_raw_delay_exact_cycles`. `test_reset_drives_owned_pp_idle_only`. `test_resolve_contention_and_od`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): bus and engine core`.

### Task 6: System simulator

**Files:** Create `emu/model/system.py`, `tests/test_system.py`.

**Interfaces — Produces:**
- `EngineSpec(image, role: Role, clock: tuple[int,int]=(1,1), phase: int=0)`. The engine is enabled on tick `t ≥ phase` iff `((t-phase+1)*num)//den > ((t-phase)*num)//den`, with `num ≤ den`.
- `System(engines: list[EngineSpec], glitches: list[Glitch] = ())`; `host_push(idx, words, at_tick=0)`; `run(max_ticks, stop_when_quiescent=True) -> Trace`. Wires come from `engines[0].image.lines`. Each tick: resolve wires from the current drives (glitches override), then step every enabled engine.
- `Trace(ticks, quiescent: bool, engines: list[EngineTrace], wire_edges: list[list[int]], contention: list[tuple[int,int]])`; `EngineTrace(role, records, events, rx, samples, branches, halted, cycle_tick: list[int])`. Quiescent means every engine is `idle`, no host push is pending, and no glitch remains.
- `Glitch(wire: int, start: int, length: int, value: int)`, used by Phase 2.

- [ ] **Step 1: Tests**: `test_edge_latency_between_engines` (A `RAW SET` at cycle t; B's `RAW GET` sees it at cycle `t + EDGE_LAT`, not before). `test_clock_enable_ratio` (49/50 → 49 cycles in 50 ticks). `test_phase_offsets_no_spurious_edges` (followers with phases 0..7 log no edges at start). `test_quiescence_stops_run`. `test_contention_reported` (two engines RAW-driving one PP line, compiled with checks off).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): multi-engine system simulator`.

### Task 7: Timed ops: COND, HOLD, unclocked XFER

**Files:** Modify `emu/model/engine.py`. Create `tests/test_engine_timed.py`. Small hand-built descriptions, two or three engines via `System`.

- [ ] **Step 1: Tests**: `test_follower_tb_matches_owner_tb_equal_clocks` (COND then HOLD: the follower's record cycles map to the same planned `tb` as the owner's). `test_cond_window_early_and_timeout` (an owner running a scaled image fires early → `EARLY`; owner silent → `TIMEOUT` + `ABORT` → pc == abort_pc). `test_hold_level_mismatch_logs_level_and_aborts`. `test_xfer_widths_and_order` (n ∈ {1, 8, 16} × LSB/MSB × 0x0000/0xFFFF/random: follower value == owner src). `test_xfer_check_flags_stretched_bit` (owner image with bit nom scaled past max → `EDGE`). `test_xfer_resync_recovers_drift` (drift 4% with RESYNC passes, fails without). `test_xfer_fifo_stall_logs_overrun`. `test_xfer_dst_fifo_only_in_follow_mode`. `test_xfer_dst_reg_written_in_all_roles`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): COND, HOLD and unclocked XFER`.

### Task 8: Clocked XFER

**Files:** Modify `emu/model/engine.py`. Create `tests/test_engine_clocked.py`.

- [ ] **Step 1: Tests**: `test_clock_owner_edge_times` (edges at `tb+(2k+1)H` and `tb+(2k+2)H`, cpol 0 and 1). `test_data_owner_is_clock_owner_all_cpha` (follower value == src for cpha 0/1). `test_data_owner_follows_clock` (target-owned data with clock-owner sampler, H = 6: exact data). `test_clock_follower_completion_sets_tb`. `test_clk_phase_violation_on_short_phase` (owner image H scaled to 0.5 → `CLK_PHASE`). `test_clocked_timeout_aborts` (clock stops mid-byte, `max` bounded).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): clocked XFER`.

### Task 9: UART and SPI descriptions, cross-role tests

**Files:** Create `emu/protocols/__init__.py`, `emu/protocols/uart.py`, `emu/protocols/spi.py`, `tests/test_protocols.py`.

**Interfaces — Produces:** `ProtocolSpec(name, build: Callable[..., Description], defaults: dict, space: dict[str, list], payload(rng, n) -> dict[str, list[int]])`. Registry `PROTOCOLS: dict[str, ProtocolSpec]` and `get(name) -> ProtocolSpec`. `uart.build(bit_cycles=16, data_bits=8, tolerance_ppm=20000)` with parties `("tx","rx")`. `spi.build(half_cycles=8, cpol=0, cpha=0, tolerance_ppm=20000)` with parties `("ctrl","tgt")`: a command byte from ctrl, then a reply byte from tgt, MSB first.

Window convention: `slack = ceil(2·d·nom) + 1`; `BIT = (nom − slack, nom, nom + slack)`; SPI `HALF = (max(1, H − slack), H, INF)`.

Helper used by tests (and by Task 10): `run_roles(desc, perm, payload, clocks=None, phases=None, max_ticks=...) -> Trace`, in `emu/protocols/__init__.py`.

- [ ] **Step 1: Tests**: `test_uart_all_role_permutations` (all 6 permutations of P0/P1/MON across 3 engines: rx and mon FIFOs == tx payload, zero events). `test_spi_all_role_permutations` (tgt rx == commands; ctrl rx == replies; mon rx == interleaved). `test_uart_drift_within_tolerance` (±2%: zero events). `test_uart_at_tolerance_edge_min_period` (min P accepted by T1 at ±2%: zero events, exact data). `test_uart_back_to_back_frames`. `test_spi_all_modes` (cpol × cpha). `test_description_size` (prints instruction counts for the G0 note; asserts UART ≤ 8 instructions).
- [ ] **Step 2–4:** FAIL → implement → PASS. A failure here usually means an isa-v0 rule is wrong. Fix the spec and the model together, and note the change in the commit.
- [ ] **Step 5: Commit** `feat(protocols): UART and SPI in every role`.

**Phase 1 exit:** `python -m pytest -q` is green.

---

## Phase 2: Fuzz verification with a feedback loop

The loop: **generate → run in every role → oracles → coverage → feedback**. Feedback acts in three places:

1. **Corpus admission and energy.** Cases that hit a new bin are kept. Selection probability is proportional to Σ 1/count(bin), so seeds that reach rare behavior get mutated more.
2. **Mutation-operator bandit.** Operators that recently produced new coverage are chosen more often.
3. **Generator choice weights.** Each generator decision (protocol, period bucket, drift bucket, fault kind…) is recorded. When a case finds new coverage its choices are rewarded, so fresh generation drifts toward unexplored regions.

Failures are shrunk and written as replayable JSON. After a fix they move to `tests/corpus/`, where pytest replays them forever: the outer, human-in-the-loop feedback.

### Task 10: Test cases, runner, oracles

**Files:** Create `emu/fuzz/__init__.py`, `emu/fuzz/case.py`, `emu/fuzz/run.py`, `emu/fuzz/oracles.py`, `tests/test_fuzz_oracles.py`. Modify `emu/model/system.py` to apply `Glitch`.

**Interfaces — Produces:**
- `EngineCfg(role: str, clock: tuple[int,int]=(1,1), phase: int=0, skew: dict[str,float] = {})`. `TestCase(protocol, params: dict, engines: list[EngineCfg], payload: dict[str, list[int]], glitches: list[dict], max_ticks: int, choices: list[tuple[str,str]] = [])` with `to_json/from_json/key()`.
- `conforming(case, image) -> bool`: no glitches, no skew, and pairwise clock ratio within `tolerance_ppm`.
- `Failure(oracle: str, detail: str)` with `signature -> str` (`f"{protocol}:{oracle}:{detail_kind}"`).
- `Outcome(case, image, trace, failures: list[Failure], conforming: bool)`. `run_case(case) -> Outcome` (`CompileError` → a `compile` failure unless the case is a random-description reject; see Task 16).
- Oracles, `check(outcome) -> list[Failure]`: `contention` (always). `agreement` (conforming: identical `(pc, value)` record sequences across engines). `fidelity` (conforming: each follower's rx == owner-sent values of the `dst=FIFO` steps it followed). `no_false_alarm` (conforming: zero violation events). `completion` (conforming: quiescent before `max_ticks`). `no_silent_corruption` (non-conforming: if any follower's data differs from the owner's, the MON engine or that follower logged ≥ 1 violation). `detection` (a skew pushing an owner's profile nom outside `[min − QUANT, max + QUANT]` → MON logs ≥ 1 violation).

- [ ] **Step 1: Tests**: `test_conforming_uart_case_passes_all_oracles`. `test_agreement_detects_injected_mismatch` (monkeypatch a record value). `test_glitch_on_uart_data_is_not_silent`. `test_detection_requires_monitor_violation_for_out_of_window_skew`. `test_case_json_roundtrip_and_key_stable`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(fuzz): test cases, runner and cross-role oracles`.

### Task 11: Functional coverage

**Files:** Create `emu/fuzz/coverage.py`, `tests/test_fuzz_coverage.py`.

**Interfaces — Produces:** `bins(outcome) -> set[str]`, `universe(protocol, image) -> set[str]`, and `CoverageMap` with `add(bins) -> set[str]` (returns new bins), `count(bin)`, `rarity(bins) -> float`, `report(protocols) -> dict` (hit, total, pct, holes sorted).
Bin families: `step:{p}:{op}:{do|follow|mon}`, `viol:{p}:{kind}`, `abort:{p}:{pc}`, `branch:{p}:{pc}:{taken}`, `margin:{p}:{b}` (sample-to-nearest-edge distance / slot length, buckets `<5% <15% <30% ≥30%`), `drift:{b}` (`0, ≤0.5%, ≤1%, ≤2%, ≤5%, >5%`), `param:{p}:{k}:{v}`, `payload:{zeros|ones|alt|mixed}`, `fault:{kind}:{detected|benign}`, `roles:{perm}`.

- [ ] **Step 1: Tests**: `test_bins_for_conforming_uart` contains `step:uart:XFER:follow` and `margin:uart:≥30%`. `test_universe_includes_all_ops_and_modes`. `test_coverage_map_new_bins_and_rarity`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): functional coverage`.

### Task 12: Generator with learnable choice weights

**Files:** Create `emu/fuzz/gen.py`, `tests/test_fuzz_gen.py`.

**Interfaces — Produces:** `ChoiceWeights` with `weight(key, opt)`, `reward(choices, amount)`, `decay(rate)`, and weights clamped to `[0.2, 20]`. `Generator(rng: random.Random, weights: ChoiceWeights, protocols: list[str])` with `choose(key, options)`, which records into `self.choices`, and `case() -> TestCase`.
The constrained-random space: the protocol, the params drawn from `ProtocolSpec.space`, a role permutation, the drift bucket (in-tolerance about 85% of the time), phases 0–7, a payload pattern and length 1–6, and faults (none about 70% of the time; else glitch / skew-in-window / skew-out-of-window / over-tolerance drift).

- [ ] **Step 1: Tests**: `test_generator_deterministic_per_seed`. `test_generated_cases_compile_and_run`. `test_reward_shifts_choice_distribution` (reward `fault=glitch` 50× → frequency rises above its baseline).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): constrained-random generator with feedback weights`.

### Task 13: Mutators and shrinker

**Files:** Create `emu/fuzz/mutate.py`, `emu/fuzz/shrink.py`, `tests/test_fuzz_shrink.py`.

**Interfaces — Produces:** `MUTATORS: dict[str, Callable[[TestCase, Random], TestCase]]` (`payload_bitflip, payload_resize, payload_extreme, swap_roles, clock_nudge, clock_to_tolerance_edge, phase_shift, glitch_add, glitch_drop, skew_add, skew_drop, param_step`). `shrink(case, fails: Callable[[TestCase], bool], budget=300) -> TestCase` runs greedy passes to a fixpoint: drop glitches, drop skews, nominal clocks, zero phases, truncate/delete/zero payload, default params, and tighten `max_ticks`.

- [ ] **Step 1: Tests**: `test_mutators_preserve_validity` (every operator, 50 seeds: the output round-trips JSON and `run_case` doesn't raise). `test_shrink_reaches_minimal_case` (a synthetic predicate that "fails iff a 0x5A byte is in the payload and a glitch exists" shrinks to one byte and one glitch).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): mutation operators and shrinker`.

### Task 14: Campaign loop and CLI

**Files:** Create `emu/fuzz/campaign.py`, `emu/fuzz/__main__.py`, `tests/test_fuzz_campaign.py`.

**Interfaces — Produces:** `Campaign(seed: int, protocols: list[str], out_dir: Path, p_fresh=0.35, seeds_from: list[Path] = [])` with `step() -> IterResult(case, new_bins, failures)`, `run(iters, report_every=200) -> Report`, and `Report.to_json()`. Failures are deduplicated by signature, shrunk, and written to `out_dir/failures/{signature-slug}-{key[:8]}.json` (`{"case":…, "failures":[…], "seed":…, "iteration":…}`). `out_dir/report.json` holds coverage per protocol, holes, corpus size, operator stats and the top generator weights.
CLI: `python -m emu.fuzz run --iters N --seed S --protocols uart,spi[,rand] --out DIR [--corpus tests/corpus]` (exit 1 if any failure) and `python -m emu.fuzz replay FILE...` (prints the oracle results; exit 1 on failure).

- [ ] **Step 1: Tests**: `test_campaign_deterministic` (same seed → same report). `test_campaign_coverage_grows_then_plateaus` (300 iters: coverage at 300 ≥ coverage at 50). `test_campaign_writes_shrunk_failure` (monkeypatch an oracle to fail on payload byte 0x5A → one failure file whose case has a single-byte payload). `test_cli_replay_exit_codes`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Run a real campaign:** `python -m emu.fuzz run --iters 2000 --seed 1 --protocols uart,spi --out /tmp/fuzz1`. Triage each failure: fix the model or the compiler rule, or narrow a too-loose oracle, then copy the case into `tests/corpus/`. Repeat until a clean run.
- [ ] **Step 6: Commit** `feat(fuzz): coverage-guided campaign with feedback loop` (plus one commit per triaged fix).

### Task 15: Regression replay and CI

**Files:** Create `tests/test_regressions.py`, `tests/corpus/README.md`, `.github/workflows/toolchain.yaml`.

- [ ] **Step 1: Test**: `test_regressions.py` parametrizes over `tests/corpus/*.json` and asserts `run_case(case).failures == []`; each id is the file stem.
- [ ] **Step 2: Workflow**: on push and workflow_dispatch, Python 3.11: `pip install -e toolchain[dev]`, `pytest -q toolchain`, `python -m emu.fuzz run --iters 400 --seed ${{ github.run_number }} --protocols uart,spi,rand --out fuzz-out --corpus toolchain/tests/corpus`. Upload `fuzz-out/` as an artifact `if: always()`.
- [ ] **Step 3: Run** `python -m pytest -q` locally → PASS. **Step 4: Commit** `ci: toolchain tests and fuzz smoke run`.

### Task 16: Random description generator (compiler and model fuzzing)

**Files:** Create `emu/fuzz/randdesc.py`, `tests/test_fuzz_randdesc.py`. Register `rand` in `emu/protocols/__init__.py` (params: `{"gen_seed": int}`), so the campaign treats it like any protocol.

**Interfaces — Produces:** `random_description(rng) -> tuple[Description, PayloadNeeds]` and `ill_formed(rng, desc) -> tuple[Description, str]` (applies exactly one violation and returns the expected rule id: `R1-owner, R1-shared, R2-branch, R3-timed-in-role, R3-raw-drive`).
Shape: 1–3 PP lines with random owners and idle levels, and 1–3 profiles (P in 6..40, random windows). Then 1–4 frames. Each frame is an anchoring COND on a line, 1–3 HOLD/XFER steps on lines the step owner owns, an optional `only(owner)` block that computes a src register from POP/LI/ALU, an optional forward `BR` on a wire-derived register, and finally `HALT`.
Oracle extension in `run_case`: a well-formed program that the compiler rejects only on a T-rule records bin `rand:rejected:{rule}`, not a failure. An ill-formed variant must raise `CompileError` with the expected rule, otherwise the failure is `compiler_accepts_ill_formed`.

- [ ] **Step 1: Tests**: `test_random_descriptions_deterministic`. `test_well_formed_pass_structural_rules` (200 seeds: no R/S diagnostics). `test_ill_formed_rejected_with_expected_rule` (200 seeds). `test_accepted_random_programs_pass_oracles` (100 seeds, conforming, all role perms).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Campaign with `rand`**: triage as in Task 14 Step 5.
- [ ] **Step 6: Commit** `feat(fuzz): random description generator for compiler and model fuzzing`.

**Phase 2 exit:** a clean `--iters 5000` run across `uart,spi,rand`; `report.json` committed under `docs/verification/` along with the list of bugs the loop found and the regression case for each.
