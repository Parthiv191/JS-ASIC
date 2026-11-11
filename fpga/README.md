# FPGA bring-up on the Basys 3

The chip's RTL (`src/`, the `PROJECT_SOURCES` list in `test/Makefile`) runs on a Digilent Basys 3
(Artix-7 `xc7a35tcpg236-1`) at the 40 MHz sign-off clock (chip-spec §7). The PC drives the host
SPI port through a UART-to-SPI bridge in the FPGA, so the only cable is the board's USB.

| File | What |
|---|---|
| `basys3_top.v` | the harness: MMCM 100 → 40 MHz, the bridge, the chip, Pmod JB = `uio`, LEDs, switches |
| `uart_spi_bridge.v` | PC frames (0xA5, 16-bit length, bytes) → host SPI, mode 0, SCK = clk/10 |
| `sram_fpga.v` | block-RAM stand-in for the IHP SRAM macro, with the macro model's read behaviour |
| `basys3.xdc` | pins |
| `build.tcl` | Vivado batch build → `build/basys3_top.bit`, `timing.rpt`, `utilization.rpt` |
| `sim/` | cocotb: every bring-up layer through the bridge and the chip's RTL (`cd fpga/sim && make`) |
| `../host/` | the PC side: `emu_host.py` (driver), `bringup.py` (one check per layer) |

## Board map

| Board | Chip |
|---|---|
| USB-UART, 1 Mbaud | host SPI, through the bridge |
| Pmod JB pins 1–4, 7–10 | `uio[0..7]` (IO0–IO7), with the FPGA's pull-ups. I2C at 400 kHz needs external 2.2–4.7 kΩ ones (architecture §8). |
| Pmod JC pins 1–4 | flash SCK, CS_N, MOSI, REPORT (debug) |
| btnC | reset |
| sw[15], sw[14] | TESTMODE strap and direction, read at reset |
| sw[5:3] | `ui_in[5:3]`: keep 0 |
| sw[2:0] | `ui_in[2:0]` in TESTMODE only |
| led[7:0] | `uo_out`: 0 HOST_MISO, 1 IRQ, 2 ERR, 3–5 flash, 6 REPORT, 7 ACT |
| led[15:8] | `uio_in`, the protocol pins as the chip sees them |

## Build

```sh
# from fpga/, so Vivado's scratch files (.Xil/) stay where .gitignore covers them;
# the 2026.1 install (the 2026.1.1 one rejects the license tier)
cd fpga
D:\2026.1\Vivado\bin\vivado.bat -mode batch -nojournal -nolog -source build.tcl
```

## Bring-up session (architecture §5's order; stop at the first failure)

Setup: `pip install pyserial`, plug in the board, program `fpga/build/basys3_top.bit`
(Vivado Hardware Manager), and find the USB-UART's COM port (Device Manager, "USB Serial Port").
All switches down.

| Layer | Do | Expect |
|---|---|---|
| 1 TESTMODE | sw[15] and sw[14] up, press btnC. Toggle sw[5:0]. | led[13:8] follow sw[5:0], and led[15:14] are on: `uio` outputs the registered `ui_in`. With sw[14] down and btnC again, led[7:0] show `uio_in` (all 1 from the pull-ups). sw[15] down and btnC to leave. |
| 2 host port | `python host/bringup.py --port COM5 id` | ID 0x5E; SCRATCH holds 0x00, 0xFF, 0xA5, 0x5A |
| 3 GPIO override | `... gpio` | PINS follows PIN_OUT (led[15:8] too), and reads 0xFF once released |
| 4 program SRAM | `... sram` | 2048 random bytes read back |
| 5 one engine | `... engine` | engine 0 halts with r0–r2 = 0x1234, 0x0FF0, 0x1DC4 |
| 6 LOOPBACK | waits for engine 1 in the RTL | |
| 7 a device | the microcontroller sends 4 bytes at 115200 8N1 (3.3 V) to JB pin 1, ground shared; `... uart-rx` | the bytes arrive in RX, no violations |

Every layer's register sequence also runs in `fpga/sim` against the RTL, so a failure on the
board points at the board, the pins or the build, not the sequence.
