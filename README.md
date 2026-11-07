![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg) ![](../../workflows/toolchain/badge.svg)

# Role-switching protocol emulator

A Tiny Tapeout chip for the IHP SG13CMOS5L shuttle (6x4 tiles), built for the
Jane Street ASIC design competition. Each bus protocol is written once, as an
owner-labelled description. The same program then runs in any role: controller,
target or monitor. A learning bus fuzzer mutates the messages it sends and keeps
the ones that make a device respond in new ways.

Top module: `tt_um_prot_emu_SAP`. Datasheet: [docs/info.md](docs/info.md).

## Status (2026-11-04)

- **On the chip today:** a bring-up design. It has a fixed UART transmitter and
  a trial harness for the program memory, a 256 × 32 SRAM built from two IHP
  `RM_IHPSG13_1P_256x16` macros. CI hardens it on the CMOS5L template and passes
  precheck and gate-level tests. The protocol engine replaces both later.
- **Toolchain:** the ISA encoding, the description compiler with its
  well-formedness and timing rules, and the reference model with its timed
  operations (`COND`, `HOLD`, unclocked `XFER`) are done. A UART runs in every
  role assignment from one 7-word description (`toolchain/emu/protocols`).
  Clocked and duplex lanes come next (plan Task 8).
- **Open decisions:** how the SRAM macros are split between engines and the
  fuzzer (the die takes at most 4, and the fourth slot is untested), and the
  phase order. They are listed in the plan's "Open decisions" section.

## Layout

| Path | Contents |
|---|---|
| `src/` | Verilog (`project.v`, `uart_tx.v`, `prog_mem.v`), LibreLane config, custom PDN script |
| `test/` | cocotb tests, RTL and gate-level ([test/README.md](test/README.md)) |
| `flow/` | blackbox of the SRAM macro for the lint step |
| `toolchain/` | Python ISA, compiler and reference model ([toolchain/README.md](toolchain/README.md)) |
| `docs/design/isa.md` | the cycle-level ISA contract (v1) |
| `docs/design/bus-fuzzer.md` | the learning bus fuzzer design |
| `docs/superpowers/plans/` | the implementation plan, its phases and open decisions |

## Tiny Tapeout

The GitHub actions build the ASIC files with
[LibreLane](https://www.zerotoasiccourse.com/terminology/librelane/), run the
precheck and the gate-level tests, and publish the
[results page](https://tinytapeout.com/faq/#my-github-action-is-failing-on-the-pages-part).

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)
- [Join the community](https://tinytapeout.com/discord)
