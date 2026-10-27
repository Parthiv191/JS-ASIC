<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

Week 1 bring-up: a fixed UART transmitter (115200 baud, 8N1 at 50 MHz). This will be replaced by the protocol emulator.

## How to test

Put a byte on ui_in[7:0], then raise uio_in[0] to send it. The byte comes out on uo_out[0] (TX). uo_out[1] is high while sending.

## External hardware

A USB-UART adapter on uo_out[0] to read the bytes.
