![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg) ![](../../workflows/toolchain/badge.svg)

# Role-switching protocol emulator

A Tiny Tapeout chip for the IHP SG13CMOS5L shuttle (8x4 tiles), built for the
Jane Street ASIC design competition. Each bus protocol is written once, as an
owner-labelled description. The same program then runs in any role: controller,
target or monitor. A learning bus fuzzer mutates the messages it sends and keeps
the ones that make a device respond in new ways.

Top module: `tt_um_prot_emu_SAP`. Datasheet: [docs/info.md](docs/info.md).

## Status (2026-11-21)

- **Design:** ISA v1, the architecture and the scope are decided, apart from the items marked Open ([docs/design/](docs/design/README.md)).
  Milestones: a first end-to-end chip by Nov 29, Core complete by Dec 13, then improvement rounds
  until the Jan 17 submission ([chip-spec §4](docs/design/chip-spec.md), [plan](docs/plan.md)).
- **On the chip today:** TESTMODE, the host SPI port, GPIO override, LOOPBACK and both 512x32 SRAM
  columns. Engine 0 with timed steps, TX/RX FIFOs, clocked lanes (CPOL/CPHA, duplex, clock
  stretching), the I2C bus detector and gated lines. CI hardens it at 8x4 for a 40 MHz clock and
  passes precheck and the gate-level tests. Engine 1 is next.
- **Toolchain:** the compiler and reference model at ISA v1, with library UART, SPI and I2C (and an
  I2C register read) that run in every role from one description. The compiler's timing rules
  are cross-checked against the model by random programs, and the model by mutation testing.
- **Co-sim:** the RTL matches the model on every pin, every cycle, for the library UART, SPI and
  I2C and for random programs; the UART suite also passes at gate level.
- **Fuzzer:** in the model, the learning fuzzer finds every planted behaviour with partial feedback
  in 5 of 5 seeds; the no-feedback baseline finds none (#34).
- **Challenge compliance:** [docs/challenge.md](docs/challenge.md).

## Layout

| Path | Contents |
|---|---|
| `src/` | Verilog (`project.v` top level, `host_spi.v`, `prog_mem.v` SRAM wrapper), LibreLane config, custom PDN script |
| `test/` | cocotb tests, RTL and gate-level ([test/README.md](test/README.md)) |
| `flow/` | blackbox of the SRAM macro for the lint step |
| `toolchain/` | Python ISA, compiler and reference model ([toolchain/README.md](toolchain/README.md)) |
| `docs/design/` | the design docs: chip spec, architecture, the ISA v1 contract, the bus fuzzer, the decision log ([index](docs/design/README.md)) |
| `docs/plan.md`, `docs/challenge.md` | the schedule; what the challenge requires and where we meet it |
| `docs/superpowers/plans/` | the first task plan, superseded by `docs/plan.md`; its task designs are still referenced |

## Tiny Tapeout

The GitHub actions build the ASIC files with
[LibreLane](https://www.zerotoasiccourse.com/terminology/librelane/), run the
precheck and the gate-level tests, and publish the
[results page](https://tinytapeout.com/faq/#my-github-action-is-failing-on-the-pages-part).

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Build your design locally](https://www.tinytapeout.com/guides/local-hardening/)
- [Join the community](https://tinytapeout.com/discord)
