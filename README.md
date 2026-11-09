![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg) ![](../../workflows/toolchain/badge.svg)

# Role-switching protocol emulator

A Tiny Tapeout chip for the IHP SG13CMOS5L shuttle (8x4 tiles), built for the
Jane Street ASIC design competition. Each bus protocol is written once, as an
owner-labelled description. The same program then runs in any role: controller,
target or monitor. A learning bus fuzzer mutates the messages it sends and keeps
the ones that make a device respond in new ways.

Top module: `tt_um_prot_emu_SAP`. Datasheet: [docs/info.md](docs/info.md).

## Status (2026-11-09)

- **Design:** ISA v1, the architecture and the scope are decided ([docs/design/](docs/design/README.md)).
  Milestones: a first end-to-end chip by Nov 29, Core complete by Dec 13, then improvement rounds
  until the Jan 17 submission ([chip-spec §4](docs/design/chip-spec.md)).
- **On the chip today:** a bring-up design, a fixed UART transmitter and a trial harness for the
  program memory (two IHP `RM_IHPSG13_1P_256x16` macros). CI hardens it and passes precheck and
  the gate-level tests. The protocol engine replaces it.
- **Toolchain:** the encoding, the description compiler with its rules, and the reference model
  are at ISA v0, and a UART runs in every role from one description. Moving them to v1 is next.

## Layout

| Path | Contents |
|---|---|
| `src/` | Verilog (`project.v`, `uart_tx.v`, `prog_mem.v`), LibreLane config, custom PDN script |
| `test/` | cocotb tests, RTL and gate-level ([test/README.md](test/README.md)) |
| `flow/` | blackbox of the SRAM macro for the lint step |
| `toolchain/` | Python ISA, compiler and reference model ([toolchain/README.md](toolchain/README.md)) |
| `docs/design/` | the design docs: chip spec, architecture, the ISA v1 contract, the bus fuzzer, the decision log ([index](docs/design/README.md)) |
| `docs/superpowers/plans/` | the first task plan, superseded by the milestones in chip-spec §4; its task designs are still referenced |

## Tiny Tapeout

The GitHub actions build the ASIC files with
[LibreLane](https://www.zerotoasiccourse.com/terminology/librelane/), run the
precheck and the gate-level tests, and publish the
[results page](https://tinytapeout.com/faq/#my-github-action-is-failing-on-the-pages-part).

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)
- [Join the community](https://tinytapeout.com/discord)
