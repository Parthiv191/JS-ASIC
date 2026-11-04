# Role-Switching Protocol Emulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the role-switching protocol emulator from the proposal: one owner-labeled description per protocol that runs as controller, target or monitor, extended with the team ISA draft's requirements and an on-chip learning bus fuzzer. Verification is a coverage-guided fuzz loop whose findings feed back into the generator and the regression suite.

**Architecture:** The Python toolchain (`toolchain/emu`) is the single source of truth. It holds the ISA encoding, the description DSL, the compiler with its well-formedness and timing checks, and a cycle-level reference model of N engines on a shared bus. A verification campaign generates constrained-random test cases, runs every role pairing plus a monitor, checks cross-role oracles, extracts functional coverage, and feeds new coverage back into seed selection, mutation scheduling and generator choice weights. Failures are shrunk into a replayed regression corpus. The learning bus fuzzer is the chip's user-facing novel feature: modelled first (`FuzzUnit`), then driven from a host on the FPGA, then built in RTL. Phase 5 adds the Verilog engine as a second backend for the same cases, so RTL is diffed against the model.

**Tech Stack:** Python ≥ 3.11 (no runtime deps; dev: pytest, hypothesis), Verilog RTL on the Tiny Tapeout IHP CMOS5L template, cocotb + Icarus/Verilator, SymbiYosys (Phase 5).

**Spec:** `role_switching_protocol_emulator_proposal.md` (vision), `docs/design/isa-v0.md` (cycle-level contract; §8 maps the team draft), `docs/design/bus-fuzzer.md` (fuzzer design), and the team draft `protocol_emulator_ISA(1).xlsx` (requirements). Executors read the two design docs.

## Revision log

- **r1 (2026-11-02):** six phases, Tasks 1–16. Tasks 1–6 executed.
- **r2 (2026-11-02):** folds in the team ISA spreadsheet (64-word program memory, CRC, IRQ, wait-with-timeout, full-duplex SPI) and adds the learning bus fuzzer.
  - Seven phases.
  - Tasks 1–6 keep their numbers, plus the code amendments A1–A2 below.
  - New Task 9 (CRC + IRQ). r1 Tasks 9–16 become 10–17. New Task 18 (mutation testing) and Phase 3 (Tasks 19–23).
- **r3 (2026-11-03):** hardware findings and audit follow-ups. No task is renumbered.
  - The r2 amendments A1–A2 are done (`a56f526`).
  - Audit follow-ups (`8456129`):
    - DSL validation of profile and lane refs.
    - A guard: the model raises on `BR TO`.
    - The R2-branch rule for `BR TO`, which lands early from Task 7.
    - An encode-failure guard.
    - Clearer T1 messages and the missing tests.
  - CI moved to the CMOS5L template (`04562f0`); see Global Constraints.
  - SRAM program-memory trial (`a20ad41`, `a200102`): two `RM_IHPSG13_1P_256x16` macros as a 256 × 32 memory. CI run 41584775180 passed harden, precheck and gate-level tests.
    - The trial turned up the 4-macro limit and the reset convention below.
    - It showed fetch-ahead hides the macro's read latency, so the ISA costs are unchanged (isa-v0 §1).
  - The fuzzer design is mapped onto the macro and its cycle costs re-checked (bus-fuzzer.md §2.1, §3.1).
  - R3-fz now requires a single-party block.
  - New section: Open decisions D1–D3.
- **r3, 2026-11-04:** D2 decided (the rule stays). Task 7 done; see its notes.

## Global Constraints

- Target: IHP SG13CMOS5L (130 nm) via Tiny Tapeout, **6x4 tiles** (die 1289.28 × 710.64 µm; design for 6x4 from day one, 8x4 only if announced). This is the Jane Street ASIC design competition's March 2027 CMOS5L shuttle.
  - Template: `TinyTapeout/ttihp-verilog-template`, branch `cmos5l`.
  - CI: `tt-gds-action@ihp-cmos5l` with `pdk: ihp-sg13cmos5l`.
- Pins: clk, rst_n, 8 inputs, 8 outputs, 8 bidirectional. Protocol lines use the 8 bidirectional pins.
- Clock: operate at 48 MHz; harden at `CLOCK_PERIOD = 20` ns (50 MHz); 40 MHz fallback.
- Submission due **January 18, 2027**; RTL freeze around January 4, 2027. Week numbers follow the team sheet: week 1 starts Oct 26, 2026.
- Terminology: "controller"/"target", never master/slave.
- Toolchain language: Python (pairs with the existing cocotb tests). RTL language: Verilog (existing repo).
- `SYNC_LAT = 2`, `EDGE_LAT = 3`, `QUANT = 1`, 8 lines, 4×16-bit registers, 16 profiles, 16 lanes (isa-v0 §1).
- Program memory: `PROG_DEPTH = 64` words × 32 bits per image (team draft), with 12-bit branch targets. In flops this costs about 2× the draft's ~6K cells. On SRAM, an engine gets 256 words from two macros (`src/prog_mem.v`). The macro's one-cycle read latency is hidden by fetch-ahead, so no ISA cost changes (isa-v0 §1).
- SRAM macro: `RM_IHPSG13_1P_256x16_c2_bm_bist`, proven on this template by CI run 41584775180.
  - Size and behaviour: 236.8 × 118.78 µm, 256 × 16 bits, single port. One access per cycle; read data comes one cycle later and holds until the next read.
  - Pins: `A_DLY` must be tied high, and `VDDARRAY!` must be connected, or the array keeps no data.
  - Power: the pins are Metal4-only, so the PDN is the custom `src/pdn_cfg.tcl`, from 2AMLogic under Apache-2.0. It puts three conditions on placement:
    1. Each macro's x is 91.2 + 44.96k µm.
    2. No macro covers the power-pin strap at x = 366.6–370.7 µm.
    3. Macros never overlap in x, because their power bridges run to the core's top and bottom edges.
  - That allows **at most 4 macros on the 6x4 die**, at x = 91.2, 405.92, 675.68 and 945.44. The trial uses 495.84 and 765.60.
  - **The fourth slot is unproven.** It is the one at x = 91.2, left of the power-pin strap; every other legal position lies to the right. Its bridges run up into the Tiny Tapeout pin cluster on the top edge (x < 192 µm). 2AMLogic's single macro sits at exactly that x on a 2x2 die and passes precheck, but it is untested on this die. If it fails, the limit is **3**.
  - A wider die (8x4) fits more; not checked.
  - Simulation uses the PDK's behavioural models (`test/sram/`) with `-DFUNCTIONAL`.
- Reset: every flop resets through its own `RESET_B` pin. Reset asserts asynchronously from `rst_n` and releases through a two-flop synchronizer (`src/project.v`). With a synchronous reset, synthesis can build the reset logic as `q | !q`. Gate-level simulation reads that as X at power-up, though silicon reads it as 1; this failed CI run 41580430296.
- FIFOs 4 × 16 bit (RTL), CRC state 16 bit.
- Fuzz unit: one unit attached to one engine.
  - Sizes: a 32-byte corpus slot, 1024-bit bitmap, 32-byte staging and 8 findings.
  - Cycle costs: `FZ_NEXT_CYCLES = 48` and `FZ_END_CYCLES = 40`, checked against the macro in bus-fuzzer.md §3.1.
  - Slot count: the model defaults to 32 slots. The chip's count follows D1: 28 under A, 12 under C.
- Area: aim for about half utilization (team finding: routing runs out before space).
- The toolchain has zero runtime dependencies; dev deps are pinned in `toolchain/pyproject.toml`.
- Never cut (proposal §14.5): three-role UART, SPI and I2C; the monitor log; the formal harness for those; a clean harden.
- Fuzzing, both verification and bus fuzzing, must be deterministic for a given seed so every result is replayable.

## Review Focus

1. **Follower arrives late to a waiting step** (role-only instructions between steps, including fixed-cost `FZ` ops). Expected: the compiler rejects it with `T5-watch`. The model must never hang silently; a hang is reported as a `completion` failure. Tests: Task 4 `test_t5_rejects_long_follower_gap`, Task 20 `test_t5_counts_fz_cycles`.
2. **Owner's FIFO empty at an owned step.** Expected: the owner stalls; a bounded step logs `OVERRUN` and never sends silent garbage. Test: Task 7 `test_xfer_fifo_stall_logs_overrun`.
3. **Clock drift exactly at the declared tolerance on the longest frame** (UART stop bit, 9.5 P from the anchor). Expected: no false alarms at the minimum P the compiler accepts. Test: Task 10 `test_uart_at_tolerance_edge_min_period`.
4. **Back-to-back frames with zero idle**, and payload extremes (0x00, 0xFF, n = 1, n = 16, MSB-first). Expected: the follower catches the next start edge, and data is exact. Tests: Task 10 `test_uart_back_to_back_frames`, Task 7 `test_xfer_widths_and_order`.
5. **Fuzzer fingerprint nondeterminism.** The same input to the same device, under in-tolerance jitter (reset phase offsets, clock ratios inside `tolerance_ppm`), must give the same fingerprint. Otherwise the bitmap fills with noise and the corpus floods with duplicates. Test: Task 22 `test_fingerprint_stable_under_in_tolerance_jitter`.

## Open decisions

These need the user. Work that depends on them waits.

**D1. How the SRAM macros are split** (blocks the fuzzer memory map, the chip's slot count, and the engine count). The die takes at most 4, one of them in an unproven slot. Each engine on SRAM takes 2 macros.

| Option | Program memory | Fuzzer storage |
|---|---|---|
| **A** (recommended) | 1 engine on SRAM (256 words). Other engines get small flop memories, e.g. 32 words at roughly 10% of the die each. | 2 macros: 28 slots plus the bitmap; the full learning loop |
| **B** | 2 engines on SRAM | None on chip: the host-driven loop (Task 23, Phase 5) |
| **C** | 1 engine on SRAM | 1 macro: 12 slots plus the bitmap, and 1 macro spare |
| **D** | Rework `src/pdn_cfg.tcl` so macros can stack | More macros. Unproven, and each attempt costs a 35–45-minute CI run |

The reason for A: fuzzing a real device needs one engine on fast memory, the one in the controller role. The other engines mainly serve cross-role self-tests and monitoring, where small programs are enough.

Two checks come before freezing D1:
- **A and B both need all 4 macros, including the unproven slot at x = 91.2** (Global Constraints). A CI harden with a macro there settles it. If only 3 fit, C is the only option.
- **Engine area.** Synthesize the engine (Phase 5) to see how much area flop memories really leave.

**D2. `T2-window` at an anchor: decided 2026-11-04, the rule stays.** A `CHK` unclocked `XFER` that starts right at an anchor is rejected at boundary 0 (isa-v0 §6). The rejection prevents a false alarm: the owner drives bit 0 a cycle late (issue slot), and the follower can measure it 2 cycles out, past the engine's `0 ± QUANT`. Slack in T2 alone, or skipping boundary 0 in the compiler alone, would be unsafe. A description drops `check` on that step or puts a timed step first; the compiler's message says so.

**D3. Phase order.** Keep the plan's order (Phase 2 before Phase 3), or go straight from Phase 1 to Phase 3 if on-chip fuzzing comes first. Phase 3 touches two Phase 2 files, so those would start as stubs: `emu/fuzz/randdesc.py` (Task 17) and the `emu/fuzz/__main__.py` CLI (Task 15). The cost is losing Phase 2's model bug-hunting before the fuzzer relies on the model.

---

## Phases

| Phase | Weeks | Delivers | Exit criterion |
|---|---|---|---|
| **1. Reference model** | 2–3 (Nov 2–15) | ISA v0 r2 encoding, description DSL, compiler (R1–R3, T1–T5), cycle-level multi-engine model with timed ops, clocked and duplex lanes, CRC and IRQ, UART + full-duplex SPI descriptions (Tasks 1–10) | UART and SPI pass cross-role tests in every role permutation with drift inside tolerance (pytest) |
| **2. Verification fuzz loop** | 3–5 (Nov 9–29) | Test-case format, oracles, functional coverage, coverage-guided campaign (corpus energy, operator bandit, generator-weight feedback), shrinker, regression corpus + CI, random descriptions (well- and ill-formed), mutation testing of the oracles (Tasks 11–18) | Clean `--iters 5000` run across `uart,spi,rand`; every planted mutant killed or documented as equivalent |
| **3. Learning bus fuzzer (model)** | 5–6 (Nov 23 – Dec 6) | `FuzzUnit`, `FZ` ops and `src=FZ` in the engine and compiler, devices with planted behaviour, learning-vs-baseline and target-role experiments, host-driven loop (Tasks 19–23) | `docs/verification/bus-fuzzer-experiments.md` shows learning beating the no-feedback baseline on partial-progress devices; host-driven and on-chip loops agree |
| **4. I2C and protocol extensions** | 6–7 (Nov 30 – Dec 13) | COND START/STOP, OD readback in followed steps, clock stretching, CHOICE (removes the fuzzer's fixed-length limit), ARB, I2C in all roles; stretch: MEASURE, CODEC, USB-LS showpiece, margin sweep | I2C passes cross-role tests in the model |
| **5. Engine RTL, co-simulation, FPGA** | 4–10 (Nov 16 – Jan 3), RTL in parallel | Verilog engine with fetch-ahead from `src/prog_mem.v`, `isa_defs.vh` generated from `emu.isa`, image loader, top level replacing the hardcoded UART and the SRAM trial harness, an engine synthesis for the D1 area estimate, Verilator backend for the fuzz runner (RTL vs model at step boundaries), planted RTL bugs, sigrok cross-checks, SymbiYosys harness, host link, Basys 3 bring-up, host-driven bus fuzzer on real parts | RTL passes the regression corpus plus N differential iterations; I2C sensor and fuzzer demo on the Basys 3 |
| **6. Fuzz unit RTL, area and timing** | 8–10 (Dec 14 – Jan 3) | Fuzz unit in Verilog diffed against `FuzzUnit`; the macro split (D1) frozen with measured area (the macro and its PDN are already proven, r3); engine count (2 or 3); harden at 20 ns with recorded area/slack | Fuzz unit RTL matches the model; harden clean at the frozen feature set |
| **7. Harden of record, submission** | 11–12 (Jan 4–17) | Timing/DRC/LVS clean harden, full GL regression, host library, self-test, writeup (including how AI was used and checked), demos | Submitted before Jan 18 |

Phases 4–7 get their own detailed plans when they start, because their task boundaries depend on Phase 1–3 findings and on the first harden numbers.

**Scope-cut order if behind:**
1. Margin sweep and CODEC.
2. The USB-LS/ARB showpiece.
3. Fuzz unit RTL. The fuzzer still ships host-driven on the FPGA (Phase 5).
4. CHOICE. The fuzzer stays fixed-length.

The never-cut list above holds regardless.

**Gate G0 (proposal §14.1, paper test).** The repo history shows no record of it. The UART half falls out of Task 10 (description size vs. the per-role programs it replaces). The I2C half is a Phase 4 entry criterion.

---

## File structure

```
toolchain/
  pyproject.toml                 package "emu"; dev extras pytest, hypothesis
  emu/isa.py                     enums, constants, Instr, encode/decode           (T1, A1)
  emu/image.py                   Profile, LineCfg, LaneCfg, CrcCfg, Image (+JSON)  (T2, T8, T9)
  emu/desc.py                    Description builder DSL                          (T2, T7–T9, T20)
  emu/compiler.py                lowering, R/S rules, debug map                   (T3, A2)
  emu/analysis.py                CFG, taint (R2), timing rules T1–T5              (T4, T7–T9, T20)
  emu/model/bus.py               wire resolution, contention                      (T5)
  emu/model/engine.py            cycle-level interpreter (generator per engine)   (T5, T7–T9, T20)
  emu/model/system.py            engines + bus + clocks + hosts + trace           (T6, T11, T20)
  emu/model/fuzzunit.py          learning bus fuzzer unit (golden model)          (T19)
  emu/protocols/{__init__,uart,spi}.py   registry + descriptions                  (T10)
  emu/fuzz/case.py               TestCase (+JSON, key)                            (T11)
  emu/fuzz/run.py                run_case -> Outcome                              (T11)
  emu/fuzz/oracles.py            cross-role oracles                               (T11)
  emu/fuzz/coverage.py           bins, universe, CoverageMap                      (T12)
  emu/fuzz/gen.py                Generator + ChoiceWeights (feedback)             (T13)
  emu/fuzz/mutate.py             mutation operators                               (T14)
  emu/fuzz/shrink.py             reducer                                          (T14)
  emu/fuzz/campaign.py           the feedback loop + report                       (T15)
  emu/fuzz/__main__.py           CLI: run / replay / mutants                      (T15, T18)
  emu/fuzz/randdesc.py           random well-/ill-formed descriptions             (T17)
  emu/fuzz/mutants.py            planted model bugs for oracle strength           (T18)
  emu/fuzz/devices.py            devices with planted behaviour + fuzz harnesses  (T21)
  emu/fuzz/busfuzz.py            bus-fuzz runner and experiments                  (T22)
  emu/fuzz/hostloop.py           host-driven fuzz loop + HostLink                 (T23)
  tests/test_*.py                one file per module
  tests/corpus/*.json            regression cases replayed by test_regressions.py (T16)
.github/workflows/toolchain.yaml pytest + fuzz smoke                              (T16)
```

Run all toolchain tests with `cd toolchain && python -m pytest -q`. Tests marked `@pytest.mark.slow` (long campaigns and experiments) run with `-m slow`; the default run skips them.

---

## Phase 1: Reference model

### Task 1: Toolchain skeleton and ISA encoding

**Files:** Create `toolchain/pyproject.toml`, `toolchain/emu/__init__.py`, `toolchain/emu/isa.py`, `toolchain/tests/test_isa.py`, `toolchain/README.md`.

**Interfaces — Produces:**
- Constants `SYNC_LAT=2, EDGE_LAT=3, QUANT=1, NUM_LINES=8, NUM_REGS=4, NUM_PROFILES=16, NUM_LANES=16, PROG_DEPTH=4096, INF=0xFFFF, REG_MASK=0xFFFF` (r2: `PROG_DEPTH=64`, amendment A1).
- `IntEnum`s `Op, Owner, Role, CondKind, AluOp, BrCond, RawOp, Src, Dst` with isa-v0 §3 codes; flag ints `F_CHK, F_RESYNC, F_ABORT, F_REG`.
- `@dataclass(frozen=True) Instr(op, owner=Owner.NONE, pin=0, prof=0, operand=0, flags=0)`.
- `encode(i: Instr) -> int`, `decode(w: int) -> Instr` (ValueError on out-of-range fields or unknown opcode).
- Field helpers: `xfer_operand(n, src, dst, msb_first) -> int`, `unpack_xfer(operand) -> (n, Src, Dst, bool)`, `role_if_operand(mask, count)`, `unpack_role_if(operand)`, `role_mask(roles: Iterable[Role]) -> int`.

- [x] **Step 1: Write failing tests**: `test_encode_pins_field_layout` asserts `encode(Instr(Op.XFER, Owner.P1, pin=3, prof=5, operand=0x7AB, flags=0b10101)) == (3<<27)|(1<<25)|(3<<21)|(5<<17)|(0x7AB<<5)|0b10101`. `test_roundtrip_property` (hypothesis over all valid fields) asserts `decode(encode(i)) == i`. `test_field_overflow_raises` covers pin=16, operand=4096, flags=32, and op code 31 → ValueError. `test_xfer_operand_roundtrip` covers n=1 and n=16, all src/dst, msb.
- [x] **Step 2: Run** `python -m pytest tests/test_isa.py -q` → FAIL (module missing).
- [x] **Step 3: Implement** `isa.py`, plus `pyproject.toml` (setuptools, `requires-python >= 3.11`, `[project.optional-dependencies] dev = ["pytest>=8", "hypothesis>=6.100"]`, pytest `testpaths = ["tests"]`).
- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** `feat(toolchain): ISA v0 encoding`.

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
- The builder raises `ValueError` immediately only for **name and type errors** (unknown party, duplicate line or label name, bad register name, unknown src/dst/cond name, nested `only`). Rule violations are left for the compiler so that ill-formed descriptions can still be built.

- [x] **Step 1: Tests**: `test_builder_records_steps_and_locs` checks kinds, owners and that `loc` ends with `test_desc.py:<n>`. `test_only_marks_block_membership`. `test_nested_only_raises`. `test_unknown_party_raises`. `test_duplicate_label_raises`. `test_image_json_roundtrip`. `test_with_profiles_scales_nom_only` checks that a scale of 1.1 on `BIT` with nom 20 gives nom 22 and leaves min/max unchanged.
- [x] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [x] **Step 5: Commit** `feat(toolchain): description DSL and image`.

### Task 3: Compiler (lowering, R1, R3, S rules, debug map)

**Files:** Create `emu/compiler.py`, `tests/test_compiler.py`.

**Interfaces:**
- Consumes: `Description`, `Image`, `isa`.
- Produces: `compile_desc(desc, *, check_timing=True) -> Image` (r2: gains `prog_depth`, amendment A2). `CompileError(Exception)` with `.diags: list[Diagnostic]`. `Diagnostic(rule: str, index: int, loc: str, msg: str)`. `lower(desc) -> Lowered` is internal but importable for `analysis.py`.
- Lowering: role blocks → `ROLE_IF(mask, count)` + body. Labels → pcs. Profiles → table indices (`prof=None` → 0). A line passed to `xfer` becomes an implicit unclocked lane (deduplicated). Party names → `Owner.P0/P1`; `"shared"` → `Owner.SHARED`. `debug[pc] = f"{loc} {kind} {args}"`.

- [x] **Step 1: Tests**: `test_lowering_role_if_count_and_labels` (ROLE_IF count equals the body length; a BR target resolves to the label's pc). `test_r1_rejects_wrong_owner_on_pp_line`. `test_r1_rejects_shared_on_pp`. `test_r1_rejects_clock_line_without_party_owner`. `test_r3_rejects_timed_step_in_role_block`. `test_r3_rejects_raw_drive_pp_outside_owner_block`. `test_r3_rejects_branch_out_of_block`. `test_s_rejects_unresolved_label`. `test_s_rejects_17_profiles`. `test_debug_map_has_every_pc`. Each negative test asserts `{d.rule for d in e.diags} == {"<rule>"}`.
- [x] **Step 2–4:** run (FAIL), implement, run (PASS).
- [x] **Step 5: Commit** `feat(toolchain): compiler lowering and ownership rules`.

### Task 4: Analysis (R2 taint, T1–T5)

**Files:** Create `emu/analysis.py`, `tests/test_analysis.py`. Modify `emu/compiler.py` to call `check_branches(desc, low)` and, when `check_timing`, `check_timing(desc, low)`. Both return `list[Diagnostic]`.

**Interfaces — Produces:** `successors(instrs, pc, role=None)`, `check_branches(...)`, `check_timing(...)`, `completion(step, role)`, and `gaps(...)` (untimed cycles a role executes between two timed steps). Formulas are isa-v0 §6, verbatim.

- [x] **Step 1: Tests**: `test_r2_rejects_branch_on_popped_reg`. `test_r2_rejects_branch_on_reg_written_in_role_block`. `test_r2_accepts_branch_on_xfer_dst`. `test_r2_taint_joins_over_loop` (a reg tainted on the back edge is rejected at the loop head). `test_r2_rejects_jpin_outside_block`. `test_t1_uart_period_boundary` (2% tolerance: P=10 accepted, P=8 rejected with `T1-sample`). `test_t1_rejects_unanchored_loop`. `test_t2_rejects_tight_cond_window`. `test_t3_rejects_spi_target_half_period_4` and `test_t3_accepts_6`. `test_t4_rejects_min_gt_nom`. `test_t5_rejects_long_follower_gap` (six `only("rx")` NOPs before a follower COND with nom=2 → `T5-watch`).
- [x] **Step 2–4:** FAIL → implement → PASS. The P and H boundary values come from the formulas; if the formulas disagree with a test's number, fix the test only after re-deriving by hand. Record the derivation in the test docstring.
- [x] **Step 5: Commit** `feat(toolchain): branch taint and timing compatibility checks`.

### Task 5: Bus and engine core (untimed + raw)

**Files:** Create `emu/model/__init__.py`, `emu/model/bus.py`, `emu/model/engine.py`, `tests/test_engine_core.py`.

**Interfaces — Produces:**
- `resolve(lines: list[LineCfg], drives: list[list[int|None]]) -> tuple[list[int], list[int]]` returns (wire values, contended wire indices). PP: 0 drivers → 1; 1 driver → its value; >1 → contention, value = AND. OD: AND with Z=1.
- `Engine(image: Image, role: Role, name="")` with attributes `pc, regs, tb, now, drv: list[int|None], inp, prev_inp, tx: deque, rx: list, records, events, samples, branches, halted, idle`, and `cycle(wire: list[int]) -> None` (one engine cycle: shift the synchronizer, update `inp/prev_inp`, resume the interpreter generator). Synchronizers preload the reset wire value.
- Records: `StepRecord(pc, op, mode: "do"|"follow", value, cycle)`, `Event(kind, pc, cycle, measured=None, lo=None, hi=None)`, `Sample(pc, line, cycle, slot)`, `Branch(pc, taken)`.
- `idle` is True while blocked on POP/FIFO with an empty FIFO, in an IDLE-profile COND follow, or halted. Ops without a handler raise `NotImplementedError`.

- [x] **Step 1: Tests** (single engine, wire supplied by the test): `test_untimed_ops_take_one_cycle` (LI, ALU, BR loop: assert `now` at HALT). `test_alu_ops_wrap_16_bits`. `test_role_if_skips_per_role` (each of P0/P1/MON). `test_pop_blocks_until_push`. `test_raw_get_sees_wire_after_sync_lat`. `test_raw_delay_exact_cycles`. `test_reset_drives_owned_pp_idle_only`. `test_resolve_contention_and_od`.
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(model): bus and engine core`.

### Task 6: System simulator

**Files:** Create `emu/model/system.py`, `tests/test_system.py`.

**Interfaces — Produces:**
- `EngineSpec(image, role: Role, clock: tuple[int,int]=(1,1), phase: int=0)`. The engine is enabled on tick `t ≥ phase` iff `ceil((t-phase+1)·num/den) > ceil((t-phase)·num/den)`, with `num ≤ den`.
- `System(engines: list[EngineSpec], glitches: list[Glitch] = ())`; `host_push(idx, words, at_tick=0)`; `run(max_ticks, stop_when_quiescent=True) -> Trace`. Wires come from `engines[0].image.lines`. Each tick: resolve wires from the current drives (glitches override), then step every enabled engine.
- `Trace(ticks, quiescent: bool, lines, engines: list[EngineTrace], wire_edges: list[list[int]], contention: list[tuple[int,int]])`; `EngineTrace(name, role, records, events, rx, samples, branches, halted, idle, tx_left, cycle_tick: list[int])`. Quiescent means every engine is `idle`, no host push is pending, and no glitch remains.
- `Glitch(wire: int, start: int, length: int, value: int)`, used by Phase 2.

- [x] **Step 1: Tests**: `test_edge_latency_between_engines` (A `RAW SET` at cycle t; B's `RAW GET` sees it at cycle `t + EDGE_LAT`, not before). `test_clock_enable_ratio` (49/50 → 49 cycles in 50 ticks). `test_phase_offsets_no_spurious_edges` (followers with phases 0..7 log no edges at start). `test_quiescence_stops_run`. `test_contention_reported` (two engines RAW-driving one PP line, compiled with checks off).
- [x] **Step 2–4:** FAIL → implement → PASS.
- [x] **Step 5: Commit** `feat(model): multi-engine system simulator`.

### r2 amendments to completed tasks

These change code that Tasks 1 and 3 already committed. Run them once, before Task 7. The ledger line `r2 amendments complete` marks them done.

**A1 (Task 1, `emu/isa.py`, `tests/test_isa.py`).**
- `PROG_DEPTH = 64`.
- `Op.CRC = 13`, `Op.FZ = 14`.
- `BrCond.TO = 3`, `Src.FZ = 3`.
- `F_IRQ = 1 << 4`.
- New `IntEnum`s `CrcOp(RESET=0, FEED=1, GET=2)` and `FzOp(NEXT=0, TOK=1, END=2)`.
- The DSL name tables (`SRCS`, `BR_CONDS`) do **not** gain `"fz"` or `"to"` yet. The tasks that implement them add them, so no description can reach an unimplemented code.

**A2 (Task 3, `emu/compiler.py`, `tests/test_compiler.py`).** `compile_desc(desc, *, check_timing=True, prog_depth=PROG_DEPTH)` and `lower(desc, prog_depth=PROG_DEPTH)`. A program longer than `prog_depth` words fails `S-range`.

- [x] **Step 1: Tests**:
  - `test_r2_opcode_allocation` asserts the A1 values.
  - `test_s_rejects_program_over_depth`: 65 NOPs → `{"S-range"}`; 64 NOPs compile; 65 NOPs with `prog_depth=128` compile.
- [x] **Step 2: Run** `python -m pytest tests/test_isa.py tests/test_compiler.py -q` → the two new tests FAIL (missing names, and 65 words accepted against the old depth of 4096).
- [x] **Step 3: Implement** A1 and A2.
- [x] **Step 4: Run** the full suite → PASS, except the uncommitted Task 7 RED tests.
- [x] **Step 5: Commit** `feat(toolchain): r2 ISA amendments (64-word programs, CRC/FZ/IRQ/TO codes)`.

### Task 7: Timed ops: COND, HOLD, unclocked XFER, timeout flag

**Files:** Modify `emu/model/engine.py`, `emu/desc.py` (`BR_CONDS` gains `"to"`; `br(..., cond="to")` takes no reg), `emu/analysis.py` (R2 for `BR TO`). Create `tests/test_engine_timed.py`, `tests/simutil.py` (both written, uncommitted, at the r1 pause). Small hand-built descriptions, two or three engines via `System`.

**Interfaces — Produces:** `StepRecord(pc, op, mode, value, cycle, tb)`. `Engine.to: bool` (isa-v0 §4). The follower's window anchor resets only on COND (do or follow) and clocked XFER; HOLD and unclocked XFER accumulate whoever performs them, matching `analysis._advance`. An `XFER` with `src = FZ` raises `NotImplementedError` until Task 20.

- [x] **Step 1: Tests** (the r1 set, already in `test_engine_timed.py`):
  - `test_follower_tb_matches_owner_tb_equal_clocks`. `test_cond_window_early_and_timeout`. `test_hold_level_mismatch_logs_level_and_aborts`.
  - `test_xfer_widths_and_order`, `test_xfer_wire_order_msb_vs_lsb`. `test_xfer_check_flags_stretched_bit`. `test_xfer_resync_recovers_drift`.
  - `test_xfer_fifo_stall_logs_overrun`. `test_xfer_dst_fifo_only_in_follow_mode`. `test_xfer_dst_reg_written_in_all_roles`.

  Plus r2:
  - `test_br_to_taken_after_follow_timeout`: bounded COND without ABORT, owner image scaled ×3, then `only("b"): br("late", cond="to")` → `MARK` at `late` in engines 1 and 2, not in 0.
  - `test_to_flag_cleared_by_next_timed_step`.
  - In `test_analysis.py`, `test_r2_rejects_br_to_outside_block` → `{"R2-branch"}` and `test_r2_accepts_br_to_inside_block` already landed in r3 (`8456129`). They build the `BR TO` word by hand until the DSL gains `"to"`. The model's guard (`test_br_to_is_not_run_as_nz`) gets replaced by the real `BR TO` here.
  - **Before Step 3:** decision D2 (`T2-window` at an anchor). Decided: the rule stays. `test_t2_rejects_chk_xfer_at_anchor` pins it and its message; two more tests pin the accepted workarounds.
- [x] **Step 2: Run** `python -m pytest tests/test_engine_timed.py tests/test_analysis.py -q` → FAIL (NotImplementedError for COND/HOLD/XFER; unknown branch condition `to`).
- [x] **Step 3: Implement.** **Step 4: Run** the full suite → PASS.
- [x] **Step 5: Commit** `feat(model): COND, HOLD, unclocked XFER and branch on timeout`.

**Done (2026-11-04).** Notes:
- Two r1 tests were wrong against the spec and are fixed:
  - `test_follower_tb_matches_owner_tb_equal_clocks` expected 3 records; the frame has 4 timed steps.
  - `test_xfer_wire_order_msb_vs_lsb` was one slot off from the slot numbering in `test_hold_level_mismatch_logs_level_and_aborts`.
- `n = 16` in `test_xfer_widths_and_order` runs at 1%, as the r2 note planned.
- Also landed:
  - The COND `min = 0` fix in T2 (audit item 6.2).
  - The D2 message and tests.
  - A sweep of T1 against the model (isa-v0 §6: sound, conservative by about `QUANT + 1`).
  - Tests that pin the follower's sample points and the CHK window's limits; mutation checks showed both were unguarded.
- Model changes outside the engine:
  - `System` loads each engine's synchronizer with the tick-0 wire, so a sub-cycle phase offset no longer hides the owner's first edge.
  - Quiescence waits `(SYNC_LAT + 1)` cycles after the last wire edge, because a follower waiting on an unbounded COND counts as idle.

### Task 8: Clocked XFER and duplex lanes

**Files:** Modify `emu/model/engine.py`, `emu/image.py`, `emu/desc.py`, `emu/compiler.py`, `emu/analysis.py`. Create `tests/test_engine_clocked.py`.

**Interfaces — Produces:** `LaneCfg(data, clk=None, cpol=0, cpha=0, data2=None)` (JSON round-trips; old JSON without `data2` loads). `Description.lane(name, data, clk=None, cpol=0, cpha=0, data2=None)`. Duplex semantics are isa-v0 §4.3: `dst := (v2 << n) | v1`, and the follow-mode FIFO push sends the followed line's value (packed for MON). New rule checks: R1-owner (data2 party owner ≠ data owner), S-range (`data2` only with `clk`, duplex `n ≤ 8`), and T3 for a `data2` owner that is not the clock owner.

- [ ] **Step 1: Tests**:
  - Clocked lanes (r1): `test_clock_owner_edge_times` (edges at `tb+(2k+1)H` and `tb+(2k+2)H`, cpol 0 and 1). `test_data_owner_is_clock_owner_all_cpha` (follower value == src for cpha 0/1). `test_data_owner_follows_clock` (target-owned data with clock-owner sampler, H = 6: exact data). `test_clock_follower_completion_sets_tb`. `test_clk_phase_violation_on_short_phase` (owner image H scaled to 0.5 → `CLK_PHASE`). `test_clocked_timeout_aborts` (clock stops mid-byte, `max` bounded).
  - Duplex (r2):
    - `test_duplex_exchanges_both_directions`: ctrl owns MOSI and SCK, tgt owns MISO, H = 8, ctrl tx `[0xA5]`, tgt tx `[0x3C]`. Every role's dst register == `0x3CA5`; ctrl rx == `[0x3C]`, tgt rx == `[0xA5]`, mon rx == `[0x3CA5]`.
    - `test_duplex_all_cpol_cpha`.
    - `test_r1_rejects_data2_same_owner`. `test_s_rejects_duplex_unclocked`. `test_s_rejects_duplex_n_9`.
    - `test_t3_rejects_duplex_miso_half_period_4` and accepts 6 (2%: `3 + 1 + ceil(0.02·2H)` = 5).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): clocked and duplex XFER`.

### Task 9: CRC unit and IRQ marks

**Files:** Modify `emu/image.py`, `emu/desc.py`, `emu/compiler.py`, `emu/analysis.py`, `emu/model/engine.py`. Create `tests/test_crc_irq.py`.

**Interfaces — Produces:**
- `CrcCfg(width=16, poly=0x1021, init=0xFFFF, xorout=0, lsb_first=False)` as `Image.crc` (JSON).
- DSL: `Description.crc_config(width, poly, init, xorout, lsb_first)`, `.crc_reset()`, `.crc_feed(ra, n)`, `.crc_get(rd)`, `.mark(id, irq=False)`.
- `Engine.crc: int`. A `MARK` with IRQ logs `Event("IRQ", pc, cycle, measured=id)` instead of `MARK`.
- Semantics per isa-v0 §4.4–4.5.
- R2 tracks the CRC state as a fifth taint slot. S-range: `1 ≤ width ≤ 16`, and `poly`, `init`, `xorout` < `2^width`.

- [ ] **Step 1: Tests**:
  - `test_crc_check_values`, parametrized over the three isa-v0 §4.4 configs with expected `0x29B1`, `0x4B37`, `0x19`. Feed `b"123456789"` via `LI` + `CRC FEED n=8`, then `GET`.
  - `test_crc_feed_one_cycle`. `test_crc_reset_restores_init`.
  - `test_r2_accepts_branch_on_crc_of_xfer_values`. `test_r2_rejects_branch_on_crc_fed_from_popped_reg`.
  - `test_s_rejects_crc_width_17`. `test_mark_irq_logs_irq_event`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): CRC unit and IRQ marks`.

### Task 10: UART and full-duplex SPI descriptions, cross-role tests

**Files:** Create `emu/protocols/__init__.py`, `emu/protocols/uart.py`, `emu/protocols/spi.py`, `tests/test_protocols.py`.

**Interfaces — Produces:**
- `ProtocolSpec(name, build: Callable[..., Description], defaults: dict, space: dict[str, list], payload(rng, n) -> dict[str, list[int]])`. Registry `PROTOCOLS: dict[str, ProtocolSpec]` and `get(name) -> ProtocolSpec`.
- `uart.build(bit_cycles=16, data_bits=8, tolerance_ppm=20000)` with parties `("tx","rx")`.
- `spi.build(half_cycles=8, cpol=0, cpha=0, tolerance_ppm=20000)` with parties `("ctrl","tgt")`. Per word: CS low, one duplex `XFER 8` (MOSI from ctrl's FIFO, MISO from tgt's FIFO, MSB first, `dst=FIFO`), CS high.

Window convention: `slack = ceil(2·d·nom) + 1`; `BIT = (nom − slack, nom, nom + slack)`; SPI `HALF = (max(1, H − slack), H, INF)`.

Helper used by tests (and by Task 11): `run_roles(desc, perm, payload, clocks=None, phases=None, max_ticks=...) -> Trace`, in `emu/protocols/__init__.py`.

- [ ] **Step 1: Tests**:
  - `test_uart_all_role_permutations`: all 6 permutations of P0/P1/MON across 3 engines; rx and mon FIFOs == tx payload; zero events.
  - `test_spi_full_duplex_all_role_permutations`: tgt rx == ctrl's tx; ctrl rx == tgt's tx; mon rx == packed `(miso << 8) | mosi`.
  - `test_uart_drift_within_tolerance` (±2%: zero events). `test_uart_at_tolerance_edge_min_period` (min P accepted by T1 at ±2%: zero events, exact data). `test_uart_back_to_back_frames`.
  - `test_spi_all_modes` (cpol × cpha).
  - `test_description_size`: prints instruction counts for the G0 note; asserts UART ≤ 8 words and every registered protocol ≤ `PROG_DEPTH`.
- [ ] **Step 2–4:** FAIL → implement → PASS. A failure here usually means an isa-v0 rule is wrong. Fix the spec and the model together, and note the change in the commit.
- [ ] **Step 5: Commit** `feat(protocols): UART and full-duplex SPI in every role`.

**Phase 1 exit:** `python -m pytest -q` is green.

---

## Phase 2: Fuzz verification with a feedback loop

The loop: **generate → run in every role → oracles → coverage → feedback**. Feedback acts in three places:

1. **Corpus admission and energy.** Cases that hit a new bin are kept. Selection probability is proportional to Σ 1/count(bin), so seeds that reach rare behavior get mutated more.
2. **Mutation-operator bandit.** Operators that recently produced new coverage are chosen more often.
3. **Generator choice weights.** Each generator decision (protocol, period bucket, drift bucket, fault kind…) is recorded. When a case finds new coverage its choices are rewarded, so fresh generation drifts toward unexplored regions.

Failures are shrunk and written as replayable JSON. After a fix they move to `tests/corpus/`, where pytest replays them forever: the outer, human-in-the-loop feedback. Task 18 then checks the loop itself, by planting bugs in the model and requiring the loop to catch each one.

### Task 11: Test cases, runner, oracles

**Files:** Create `emu/fuzz/__init__.py`, `emu/fuzz/case.py`, `emu/fuzz/run.py`, `emu/fuzz/oracles.py`, `tests/test_fuzz_oracles.py`. Modify `emu/model/system.py` to apply `Glitch`.

**Interfaces — Produces:**
- `EngineCfg(role: str, clock: tuple[int,int]=(1,1), phase: int=0, skew: dict[str,float] = {})`. `TestCase(protocol, params: dict, engines: list[EngineCfg], payload: dict[str, list[int]], glitches: list[dict], max_ticks: int, choices: list[tuple[str,str]] = [])` with `to_json/from_json/key()`.
- `conforming(case, image) -> bool`: no glitches, no skew, and pairwise clock ratio within `tolerance_ppm`.
- `Failure(oracle: str, detail: str)` with `signature -> str` (`f"{protocol}:{oracle}:{detail_kind}"`).
- `Outcome(case, image, trace, failures: list[Failure], conforming: bool)`. `run_case(case) -> Outcome` (`CompileError` → a `compile` failure unless the case is a random-description reject; see Task 17).
- Oracles, `check(outcome) -> list[Failure]`:
  - `contention`: always.
  - `agreement` (conforming): identical `(pc, value)` record sequences across engines.
  - `fidelity` (conforming): each follower's rx == the owner-sent values of the `dst=FIFO` steps it followed, per data line for duplex lanes.
  - `no_false_alarm` (conforming): zero violation events.
  - `completion` (conforming): quiescent before `max_ticks`.
  - `no_silent_corruption` (non-conforming): if any follower's data differs from the owner's, the MON engine or that follower logged ≥ 1 violation.
  - `detection`: a skew pushing an owner's profile nom outside `[min − QUANT, max + QUANT]` → MON logs ≥ 1 violation.

- [ ] **Step 1: Tests**: `test_conforming_uart_case_passes_all_oracles`. `test_agreement_detects_injected_mismatch` (monkeypatch a record value). `test_glitch_on_uart_data_is_not_silent`. `test_detection_requires_monitor_violation_for_out_of_window_skew`. `test_case_json_roundtrip_and_key_stable`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(fuzz): test cases, runner and cross-role oracles`.

### Task 12: Functional coverage

**Files:** Create `emu/fuzz/coverage.py`, `tests/test_fuzz_coverage.py`.

**Interfaces — Produces:** `bins(outcome) -> set[str]`, `universe(protocol, image) -> set[str]`, and `CoverageMap` with `add(bins) -> set[str]` (returns new bins), `count(bin)`, `rarity(bins) -> float`, `report(protocols) -> dict` (hit, total, pct, holes sorted).

Bin families:
- `step:{p}:{op}:{do|follow|mon}`; duplex steps add a `:d2` variant.
- `viol:{p}:{kind}`, `abort:{p}:{pc}`, `branch:{p}:{pc}:{taken}`.
- `margin:{p}:{b}`: sample-to-nearest-edge distance / slot length, buckets `<5% <15% <30% ≥30%`.
- `drift:{b}`: `0, ≤0.5%, ≤1%, ≤2%, ≤5%, >5%`.
- `param:{p}:{k}:{v}`, `payload:{zeros|ones|alt|mixed}`, `fault:{kind}:{detected|benign}`, `roles:{perm}`.

- [ ] **Step 1: Tests**: `test_bins_for_conforming_uart` contains `step:uart:XFER:follow` and `margin:uart:≥30%`. `test_universe_includes_all_ops_and_modes`. `test_coverage_map_new_bins_and_rarity`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): functional coverage`.

### Task 13: Generator with learnable choice weights

**Files:** Create `emu/fuzz/gen.py`, `tests/test_fuzz_gen.py`.

**Interfaces — Produces:** `ChoiceWeights` with `weight(key, opt)`, `reward(choices, amount)`, `decay(rate)`, and weights clamped to `[0.2, 20]`. `Generator(rng: random.Random, weights: ChoiceWeights, protocols: list[str])` with `choose(key, options)`, which records into `self.choices`, and `case() -> TestCase`.

The constrained-random space:
- the protocol, and params drawn from `ProtocolSpec.space`;
- a role permutation;
- the drift bucket (in-tolerance about 85% of the time) and phases 0–7;
- a payload pattern and length 1–6;
- faults: none about 70% of the time, otherwise glitch, skew-in-window, skew-out-of-window or over-tolerance drift.

- [ ] **Step 1: Tests**: `test_generator_deterministic_per_seed`. `test_generated_cases_compile_and_run`. `test_reward_shifts_choice_distribution` (reward `fault=glitch` 50× → frequency rises above its baseline).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): constrained-random generator with feedback weights`.

### Task 14: Mutators and shrinker

**Files:** Create `emu/fuzz/mutate.py`, `emu/fuzz/shrink.py`, `tests/test_fuzz_shrink.py`.

**Interfaces — Produces:**
- `MUTATORS: dict[str, Callable[[TestCase, Random], TestCase]]`: `payload_bitflip, payload_resize, payload_extreme, swap_roles, clock_nudge, clock_to_tolerance_edge, phase_shift, glitch_add, glitch_drop, skew_add, skew_drop, param_step`.
- `shrink(case, fails: Callable[[TestCase], bool], budget=300) -> TestCase` runs greedy passes to a fixpoint: drop glitches, drop skews, nominal clocks, zero phases, truncate/delete/zero payload, default params, and tighten `max_ticks`.

- [ ] **Step 1: Tests**: `test_mutators_preserve_validity` (every operator, 50 seeds: the output round-trips JSON and `run_case` doesn't raise). `test_shrink_reaches_minimal_case` (a synthetic predicate that "fails iff a 0x5A byte is in the payload and a glitch exists" shrinks to one byte and one glitch).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(fuzz): mutation operators and shrinker`.

### Task 15: Campaign loop and CLI

**Files:** Create `emu/fuzz/campaign.py`, `emu/fuzz/__main__.py`, `tests/test_fuzz_campaign.py`.

**Interfaces — Produces:**
- `Campaign(seed: int, protocols: list[str], out_dir: Path, p_fresh=0.35, seeds_from: list[Path] = [])` with `step() -> IterResult(case, new_bins, failures)`, `run(iters, report_every=200) -> Report`, and `Report.to_json()`.
- Failures are deduplicated by signature, shrunk, and written to `out_dir/failures/{signature-slug}-{key[:8]}.json` (`{"case":…, "failures":[…], "seed":…, "iteration":…}`).
- `out_dir/report.json` holds coverage per protocol, holes, corpus size, operator stats and the top generator weights.
- CLI:
  - `python -m emu.fuzz run --iters N --seed S --protocols uart,spi[,rand] --out DIR [--corpus tests/corpus]` (exit 1 if any failure).
  - `python -m emu.fuzz replay FILE...` (prints the oracle results; exit 1 on failure).

- [ ] **Step 1: Tests**: `test_campaign_deterministic` (same seed → same report). `test_campaign_coverage_grows_then_plateaus` (300 iters: coverage at 300 ≥ coverage at 50). `test_campaign_writes_shrunk_failure` (monkeypatch an oracle to fail on payload byte 0x5A → one failure file whose case has a single-byte payload). `test_cli_replay_exit_codes`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Run a real campaign:** `python -m emu.fuzz run --iters 2000 --seed 1 --protocols uart,spi --out /tmp/fuzz1`. Triage each failure: fix the model or the compiler rule, or narrow a too-loose oracle, then copy the case into `tests/corpus/`. Repeat until a clean run.
- [ ] **Step 6: Commit** `feat(fuzz): coverage-guided campaign with feedback loop` (plus one commit per triaged fix).

### Task 16: Regression replay and CI

**Files:** Create `tests/test_regressions.py`, `tests/corpus/README.md`, `.github/workflows/toolchain.yaml`.

- [ ] **Step 1: Test**: `test_regressions.py` parametrizes over `tests/corpus/*.json` and asserts `run_case(case).failures == []`; each id is the file stem.
- [ ] **Step 2: Workflow**: on push and workflow_dispatch, Python 3.11:
  - `pip install -e toolchain[dev]`
  - `pytest -q toolchain`
  - `python -m emu.fuzz run --iters 400 --seed ${{ github.run_number }} --protocols uart,spi,rand --out fuzz-out --corpus toolchain/tests/corpus`
  - upload `fuzz-out/` as an artifact `if: always()`.
- [ ] **Step 3: Run** `python -m pytest -q` locally → PASS. **Step 4: Commit** `ci: toolchain tests and fuzz smoke run`.

### Task 17: Random description generator (compiler and model fuzzing)

**Files:** Create `emu/fuzz/randdesc.py`, `tests/test_fuzz_randdesc.py`. Register `rand` in `emu/protocols/__init__.py` (params: `{"gen_seed": int}`), so the campaign treats it like any protocol.

**Interfaces — Produces:** `random_description(rng) -> tuple[Description, PayloadNeeds]` and `ill_formed(rng, desc) -> tuple[Description, str]`. `ill_formed` applies exactly one violation and returns the expected rule id: `R1-owner, R1-shared, R2-branch, R3-timed-in-role, R3-raw-drive`, and `R2-branch` via `BR TO` outside a block.

Shape:
- 1–3 PP lines with random owners and idle levels, an optional duplex clocked lane, and 1–3 profiles (P in 6..40, random windows).
- Then 1–4 frames, each:
  - an anchoring COND on a line;
  - 1–3 HOLD/XFER steps on lines the step owner owns;
  - an optional `only(owner)` block that computes a src register from POP/LI/ALU, or feeds and reads the CRC;
  - an optional forward `BR` on a wire-derived register.
- Finally `HALT`.

Oracle extension in `run_case`: a well-formed program that the compiler rejects only on a T-rule records bin `rand:rejected:{rule}`, not a failure. An ill-formed variant must raise `CompileError` with the expected rule, otherwise the failure is `compiler_accepts_ill_formed`.

- [ ] **Step 1: Tests**: `test_random_descriptions_deterministic`. `test_well_formed_pass_structural_rules` (200 seeds: no R/S diagnostics). `test_ill_formed_rejected_with_expected_rule` (200 seeds). `test_accepted_random_programs_pass_oracles` (100 seeds, conforming, all role perms).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Campaign with `rand`**: triage as in Task 15 Step 5.
- [ ] **Step 6: Commit** `feat(fuzz): random description generator for compiler and model fuzzing`.

### Task 18: Mutation testing of the oracles (planted bugs)

The team's verification plan says to "plant bugs on purpose to make sure the tests catch them". Here the bugs go into the reference model, and the Phase 2 loop must catch each one. Phase 5 repeats this on the RTL.

**Files:** Create `emu/fuzz/mutants.py`, `tests/test_fuzz_mutants.py`. Modify `emu/fuzz/__main__.py` (subcommand `mutants`).

**Interfaces — Produces:**
- `Mutant(name: str, doc: str, apply: Callable[[], ContextManager])`. `MUTANTS: list[Mutant]`.
- `kill(mutant, iters: int, seed: int, protocols) -> KillResult(name, killed: bool, iteration: int | None, oracle: str | None)`.
- CLI: `python -m emu.fuzz mutants --iters N --seed S --out DIR` writes `DIR/mutants.json`, and exits 1 on any survivor not listed in `EQUIVALENT`.

The mutants, each a patch that the context manager undoes:
- `follower_edge_lat_2`: the follower uses `EDGE_LAT − 1`.
- `follow_ignores_msb_first`.
- `cond_never_times_out`.
- `resync_ignored`.
- `od_resolves_as_or`.
- `hold_samples_at_slot_start`.
- `crc_drops_last_bit`.
- `role_if_treats_mon_as_p0`.
- `fifo_push_in_do_mode`: echoes sent data back to the host.
- `cond_does_not_reanchor_tb`.

- [ ] **Step 1: Tests**:
  - `test_mutants_apply_and_revert`: after each context exits, the default suite's UART smoke case passes again.
  - `test_unmutated_model_survives_budget`: same budget as below, zero failures.
  - `test_campaign_kills_every_mutant`, marked `slow`: each mutant is killed within 1000 iterations over `uart,spi,rand`.
- [ ] **Step 2–4:** FAIL → implement → PASS (`python -m pytest -q -m slow tests/test_fuzz_mutants.py`). A survivor means an oracle or a coverage bin is missing. Add it (with a test), or argue the mutant equivalent in `EQUIVALENT` with a one-line reason.
- [ ] **Step 5: Commit** `feat(fuzz): mutation testing of the verification loop`, and write `docs/verification/mutants.md` (kill table).

**Phase 2 exit:** a clean `--iters 5000` run across `uart,spi,rand`, and every mutant killed or justified. `report.json` and the mutant table are committed under `docs/verification/`, along with the list of bugs the loop found and the regression case for each.

---

## Phase 3: Learning bus fuzzer in the model

Design: `docs/design/bus-fuzzer.md`. The model's `FuzzUnit` is the golden model for the Phase 6 RTL. Everything here is deterministic per LFSR seed.

### Task 19: FuzzUnit core

**Files:** Create `emu/model/fuzzunit.py`, `tests/test_fuzzunit.py`.

**Interfaces — Produces:**
- `FuzzConfig(seed: int, length_mode: Literal["fixed","wire"]="fixed", length=8, mutable_mask: int = (1<<32)-1, slots=32, slot_bytes=32, bitmap_bits=1024, findings=8)`.
- `FuzzUnit(cfg)` with:
  - `load(slot, data: bytes, energy=15)`;
  - `next() -> int` (length);
  - `read_byte() -> int` (next staged byte; the length first in `wire` mode; 0 when exhausted);
  - `tok(b: int)`;
  - `end() -> bool` (new);
  - `abort()` (logs a hang finding while armed).
- Attributes: `armed: bool`, `fingerprint: int`, `findings: list[Finding(kind: "new"|"hang", slot, iteration, data: bytes)]`, `stats: dict` (iterations, saves, evictions, bitmap fill).
- `lfsr_step(s) -> int`: 16-bit Galois, right shift, poly `0xB400`.
- Fingerprint: CRC-16/CCITT-FALSE over the token bytes since `next()`.
- `slots` stays 32 by default. The chip's value follows D1 (28 under A, 12 under C), and Task 22 runs at it too.
- Masked point ops pick their position in bounded time (bus-fuzzer.md §3.1). Eviction takes the lowest energy, which in RTL means a comparator tree or a kept minimum, not a scan.
- **Before Step 1:** decide how a hang finding keeps its input (bus-fuzzer.md §5, open item).

- [ ] **Step 1: Tests**:
  - `test_lfsr_known_states_and_period`: from 1, the next three states are `0xB400, 0x5A00, 0x2D00`, and the period is 65535.
  - `test_same_seed_same_mutation_sequence`.
  - `test_mutable_mask_respected`: 1000 iterations with mask `0b0011`, and bytes 2.. never change.
  - `test_fixed_mode_preserves_length`. `test_wire_mode_length_within_1_32_and_sent_first`.
  - `test_end_saves_only_new_fingerprints`. `test_energy_decays_to_floor_1`. `test_eviction_takes_lowest_energy`.
  - `test_abort_while_armed_logs_hang`. `test_findings_log_caps_at_8`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(fuzz): learning bus fuzzer unit model`.

### Task 20: FZ instructions in the engine and compiler

**Files:** Modify `emu/desc.py` (`fz_next(rd)`, `fz_tok(ra)`, `fz_end(rd)`, `SRCS` gains `"fz"`), `emu/compiler.py` (R3-fz), `emu/analysis.py` (untimed cost of `FZ` ops in `gaps`), `emu/model/engine.py`, `emu/model/system.py`, `emu/fuzz/randdesc.py` (`ill_formed` gains `R3-fz`). Create `tests/test_engine_fz.py`.

**Interfaces — Produces:**
- `System(engines, glitches=(), fuzz: dict[int, FuzzUnit] = {})`, which attaches a unit to an engine.
- `Engine` runs `FZ` with the fixed costs `FZ_NEXT_CYCLES = 48` and `FZ_END_CYCLES = 40` (constants in `emu.isa`).
- `XFER src=FZ` calls `read_byte()`.
- While the unit is armed, the engine emits the auto-tokens of bus-fuzzer.md §3, and an `ABORT` calls `unit.abort()`.
- `FZ` on an engine without a unit raises `RuntimeError`.

- [ ] **Step 1: Tests**:
  - `test_fz_costs_fixed_cycles`. `test_xfer_src_fz_sends_staged_bytes`.
  - `test_auto_tokens_fold_followed_values_and_buckets`: a known reply gives a known fingerprint, computed with `FuzzUnit.tok` by hand in the test.
  - `test_abort_while_armed_records_hang`.
  - `test_r3_rejects_fz_outside_block`.
  - `test_r3_rejects_fz_in_two_party_block` (`only("a", "b")`) and `test_r3_rejects_fz_in_blocks_of_different_parties`: one unit, one owner (isa-v0 §5, r3).
  - `test_t5_counts_fz_cycles`: `fz.next` in the follower's block before a COND with nom = 40 → `T5-watch`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(model): FZ instructions and fuzz-sourced transfers`.

### Task 21: Devices under fuzz

**Files:** Create `emu/fuzz/devices.py`, `tests/test_fuzz_devices.py`.

Devices are ordinary descriptions run in the `P1` role. Their logic branches on received bytes, which are clean `XFER dst` registers, so they compile under R2. Each one has planted behaviour. Harnesses are the matching `P0` descriptions, each with a reset prefix, `fz.next`, `src=fz` transfers, `fz.end` and a loop.

**Interfaces — Produces:**
- `Device(name, desc: Description, harness: Callable[[int], Description], planted: dict[str, Callable[[Trace], bool]])`. `DEVICES: dict[str, Device]`. Each predicate in `planted` detects that the behaviour was reached.
- The devices:
  - `uart_unlock`: replies with its state number; the sequence `0xA5 0x3C 0x7E` unlocks a hidden reply. Partial progress is visible.
  - `spi_hidden_reg`: register reads over full-duplex SPI; reading register `0x1F` after writing `0x5A` to `0x10` returns a hidden value.
  - `spi_hang`: command `0xC3` makes it stop driving MISO edges; the harness's bounded follow times out. This is a hang finding.
  - `uart_magic16`: a 16-bit magic value with no partial feedback, the control case.
  - `host_parser`: a `P0` host-driver description that misparses a reply whose length byte is > 8. Used for target-role fuzzing, where the chip is the device.

- [ ] **Step 1: Tests**: `test_devices_compile_within_prog_depth`. `test_planted_behaviour_reachable_by_hand` (scripted inputs reach each predicate). `test_harness_one_iteration_runs` (one `fz.next`/`fz.end` round, quiescent).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(fuzz): devices with planted behaviour and fuzz harnesses`.

### Task 22: Learning-loop experiments

**Files:** Create `emu/fuzz/busfuzz.py`, `tests/test_busfuzz.py`, `docs/verification/bus-fuzzer-experiments.md`. Modify `emu/fuzz/__main__.py` (subcommand `busfuzz`).

**Interfaces — Produces:**
- `run_bus_fuzz(device: str, iters: int, seed: int, feedback=True, role="ctrl", jitter=None) -> BusFuzzReport(found: dict[str, int | None], fill_curve: list[int], findings, corpus_size)`.
  - `feedback=False` is the baseline: `end()` never saves, so only the initial seeds are ever mutated.
  - `role="device"` fuzzes from the target side.
- CLI: `python -m emu.fuzz busfuzz --device D --iters N --seeds 1..5 [--no-feedback]`.

- [ ] **Step 1: Tests**:
  - `test_bus_fuzz_deterministic_per_seed`.
  - `test_fingerprint_stable_under_in_tolerance_jitter`: the same 50 inputs to `spi_hidden_reg`, with phases 0–7 and clock ratios inside `tolerance_ppm`, give the same fingerprints as with no jitter.
  - `test_learning_beats_baseline_on_unlock` (slow): 5 seeds × 3000 iterations. With feedback the unlock is found in every seed; the baseline finds it in at most 1. Run it at the chip's slot count from D1 as well as at 32 (r3).
  - `test_magic16_control_no_advantage` (slow): neither finds it within budget. This shows that learning needs partial feedback.
  - `test_spi_hang_found_as_hang` (slow).
  - `test_target_role_fuzzing_finds_host_parser_bug` (slow).
- [ ] **Step 2–4:** FAIL → implement → PASS (`-m slow` for the experiments). If learning does not beat the baseline, that is a design finding. Record it in the experiments doc, and change the token set or the picker (bus-fuzzer.md) before going on. Do not loosen the test.
- [ ] **Step 5: Run** the CLI for all devices, 5 seeds each. Write the iterations-to-find table and fill curves into `docs/verification/bus-fuzzer-experiments.md`.
- [ ] **Step 6: Commit** `feat(fuzz): bus fuzzer experiments (learning vs baseline, target role)`.

### Task 23: Host-driven fuzz loop

The Phase 5 FPGA demo runs this loop before the fuzz unit exists in RTL. The `FuzzUnit` moves to the host. The engine runs a harness without `FZ`: the payload arrives through the TX FIFO, and the reply and events come back through the RX FIFO and the event log.

**Files:** Create `emu/fuzz/hostloop.py`, `tests/test_hostloop.py`. Modify `emu/fuzz/devices.py` (FIFO-fed harness variant per device).

**Interfaces — Produces:**
- `HostLink` protocol with `push(words)`, `run_until_idle(max_ticks)`, `pull() -> list[int]`, `events() -> list[Event]`.
- `ModelLink(System)` implements it. Phase 5 adds `SerialLink`.
- `host_fuzz(link, unit: FuzzUnit, iters) -> BusFuzzReport`. The host derives the same token stream from the RX words and the events.

- [ ] **Step 1: Tests**:
  - `test_host_driven_matches_on_chip_fingerprints`: for one seed, both loops produce identical fingerprint sequences over 200 iterations on `spi_hidden_reg`.
  - `test_host_driven_finds_unlock` (slow).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(fuzz): host-driven bus fuzz loop`.

**Phase 3 exit:** the experiments doc is committed. It shows that learning beats the baseline on partial-progress devices, that the control is explained, that fingerprints are stable under jitter, and that the host-driven and on-chip loops agree.
