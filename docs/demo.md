# Demo

Owner: supervisor role; the verification role runs it on the FPGA (Basys 3) and later on the
TT demo board. Status: **Agreed** (verification's review in #52, 2026-11-21). Each step names what it shows and what
passing looks like. Steps follow the bring-up order in `design/architecture.md` §5, so a failed
step points to one layer.

| # | Step | Shows | Pass | Tier |
|---|---|---|---|---|
| 1 | TESTMODE: toggle `ui_in`, watch `uio` (and back) | clock, reset, every pad | each pad follows with 2 cycles of latency | Core |
| 2 | Host SPI: read ID (0x5E), write and read SCRATCH | the host port | ID matches; SCRATCH patterns read back | Core |
| 3 | GPIO override: drive each `uio` pin high, low, input | the pin drivers | PINS reads back the drive | Core |
| 4 | Load an image, read it back | the program SRAM | every word matches | Core |
| 5 | UART transmit: host writes bytes, the chip sends them | one engine, a timed program | the microcontroller receives and prints the bytes at 115200 (on the FPGA: `host/bringup.py uart-tx`, #46) | Core (M1) |
| 6 | LOOPBACK: engine 0 as UART tx, engine 1 as rx, same program | role switching on-chip | rx reads what tx sent; no pins driven (needs engine 1, D7) | Core |
| 7 | Swap roles with no reload: engine 0 rx, engine 1 tx | one description, every role | same result (needs engine 1, D7) | Core |
| 8 | I2C controller reads a real sensor or EEPROM | open-drain, ACK readback, START/STOP | the expected ID or data | Core |
| 9 | I2C target: the chip impersonates a sensor to a microcontroller | the target role | the MCU's driver reads the chip's values | Core |
| 10 | SPI controller reads an SPI flash's JEDEC ID; the chip as SPI target to the MCU | SPI modes, CS framing | the ID; the MCU reads back what the chip serves | Core |
| 11 | Monitor: sniff an MCU ↔ sensor I2C bus and dump the capture log with timing | MON role, capture | decoded transfers with Δt | Core |
| 12 | MEASURE: pulse widths and response latency | MEASURE L1 | against a pulse of known width from the microcontroller's PWM or timer: within one cycle of the programmed width | Core |
| 13 | Fuzz a device and show the corpus grow; replay a finding | the learning fuzzer | new responses found; the finding reproduces | Novel, or host-driven if cut |
| 14 | Standalone: boot from flash, report on REPORT | standalone mode | `BOOT OK` and results on a terminal | Plus |

Hardware:
- a Basys 3 (the chip's RTL) and a microcontroller. The microcontroller stands in for every
  device: the UART peer, the I2C target and the SPI flash, so every step runs with no other parts.
- for steps 8 and 10 against *real* devices (V6), a few dollars of parts: an I2C EEPROM
  (24LC256) and an SPI flash (W25Q series). Buying them is the verification role's human's call.
- pull-ups on every open-drain line, 2.2–4.7 kΩ for I2C, even with the microcontroller as the
  target: the pads have no pull-ups (challenge Q3). The Basys 3's internal ones are too weak for
  400 kHz.

Pins: `design/architecture.md` §8.

Steps 1–5 are the bring-up checklist in `fpga/README.md` (#46): `host/bringup.py id`, `gpio`,
`sram`, `engine` and `uart-tx`, with layer 1 on the switches. *To come:* the programs and host
commands for steps 6–14.
