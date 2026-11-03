<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

Bring-up design: a fixed UART transmitter (115200 baud, 8N1 at 50 MHz) and a trial harness for the protocol emulator's program memory, a 256 x 32 SRAM built from two IHP `RM_IHPSG13_1P_256x16` macros. Both will be replaced by the protocol emulator.

## How to test

UART: put a byte on ui_in[7:0], then raise uio_in[0] to send it. The byte comes out on uo_out[0] (TX). uo_out[1] is high while sending.

Memory: hold a byte on ui_in[7:0] and raise one strobe at a time (each needs about 3 clock cycles high and low):

- uio_in[1] ADDR sets the address.
- uio_in[2] DATA shifts the byte into the 32-bit write word (send the most significant byte first).
- uio_in[3] WRITE writes the word.
- uio_in[4] READ reads the word at the address.

While uio_in[6] (VIEW) is high, uo_out shows byte ui_in[1:0] of the last word read. While uio_in[5] (CHASE) is high, the memory reads the address held in the low byte of its last output, once per clock, which walks a linked list stored in memory.

## External hardware

A USB-UART adapter on uo_out[0] to read the bytes.
