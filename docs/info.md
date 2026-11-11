<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

A programmable emulator for serial buses (UART, SPI, I2C), built for debugging and reverse
engineering. Each protocol is a short program for a timing engine, written once as an
owner-labelled description. The same program runs in any role: controller, target or monitor.

- **Two identical engines** share a 512-instruction program SRAM (engine 0 fetches on even
  cycles, engine 1 on odd). Each engine has 8 registers and TX/RX FIFOs, and can play any party
  on the bus or monitor it.
- **Protocol pins** are `uio[7:0]` (IO0–IO7). Lines can be push-pull or open-drain; every output
  comes straight from a flop.
- **A host** (the demo board's microcontroller, or a PC through an FPGA bridge) loads the program
  and tables over SPI, starts the engines and moves data through the FIFOs.
- **Debug:** TESTMODE pad test, LOOPBACK (the two engines talk on-chip), GPIO override, halt and
  register readback.
- **Planned, behind build parameters:** an on-chip learning bus fuzzer, a capture log with
  timing, standalone boot from SPI flash with a text report on `uo[6]`.

Full design: `docs/design/` in the repository (chip spec, architecture, ISA).

## How to test

**Pad test (no host needed).** Hold `ui[7]` high through reset to enter TESTMODE. With `ui[6]`
high, `uio` outputs follow `ui_in` two clocks later; with `ui[6]` low, `uo_out` follows `uio_in`.

**Host SPI.** Mode 0, MSB first, SCK at most clk/8, on `ui[0]` SCK, `ui[1]` MOSI, `ui[2]` CS_N and
`uo[0]` MISO. A frame is a command byte (bit 7 = read), a 16-bit address, then data; a read
returns one dummy byte first.

1. Read address 0x0000: the ID is 0x5E.
2. Write any byte to SCRATCH (0x0002) and read it back.
3. Load a program into 0x1000 and the tables into 0x0400 (the toolchain's `Image.host_writes()`
   produces the bytes), set ROLE at 0x2000, then write 1 to RUN (0x0004).
4. Watch `uo[7]` ACT go high while the engine runs, and `uo[2]` ERR for any violation.

## External hardware

- A host that speaks SPI mode 0: the TT demo board's microcontroller, or any microcontroller or
  FPGA.
- Pull-up resistors on open-drain protocol lines: 2.2–4.7 kΩ for I2C. The demo board's I/O is 3.3 V.
- The device under test on `uio`: a sensor, EEPROM, SPI flash or microcontroller.
- Optional, for standalone mode (planned): an SPI flash on `ui[3]` and `uo[3..5]`, and a
  USB-UART adapter on `uo[6]` REPORT (115200 baud).
