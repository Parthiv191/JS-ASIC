# Design docs: index

Read only what your task needs. Status tags used in every doc: **Decided** (owner approved),
**Proposed** (drafted, needs owner sign-off), **Open** (not decided).

| Doc | Read it when you… | Owner |
|---|---|---|
| `chip-spec.md` | need to know what the chip is for, its scope, and its limits | verification |
| `architecture.md` | work on the top level, memory, host interface, pins, standalone mode | verification |
| `isa.md` | implement or check any instruction (ISA v1, the cycle-level contract) | verification |
| `bus-fuzzer.md` | work on the learning fuzzer (FZ ops, corpus, bitmap) | devOps |
| `decisions.md` | need the why behind a choice, or want to log a new one | everyone |
| `../superpowers/plans/2026-11-02-role-switching-emulator.md` | need the old task plan (predates v1; D1 is now decided in `architecture.md` §3; to be rewritten by the supervisor role) | supervisor |

Rules:
- `isa.md` is the contract between the compiler, the Python model
  (`toolchain/emu`) and the RTL. When code and docs disagree, fix one of them in the same change.
- New decisions go in `decisions.md`, one line each, with the date and who made them.
- Keep docs short: tables over prose, and link to a section instead of repeating it.
