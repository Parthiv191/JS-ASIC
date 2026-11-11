"""FPGA bring-up checks, one per layer of architecture §5, over the Basys 3 harness's bridge.

    python bringup.py --port COM5 id       # layer 2: host port (ID, VERSION, SCRATCH)
    python bringup.py --port COM5 gpio     # layer 3: GPIO override drives Pmod JB
    python bringup.py --port COM5 sram     # layer 4: program SRAM write and read back
    python bringup.py --port COM5 engine   # layer 5: one engine runs a tiny program
    python bringup.py --port COM5 uart-rx  # layer 7: the library UART receives from a device

Layer 1 (TESTMODE) needs no PC: see README.md. Each check prints what it saw and exits 1 on the
first mismatch. Layer 6 (LOOPBACK, two engines) waits for engine 1 in the RTL.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from emu_host import ID, RUN, SCRATCH, VERSION, Chip

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolchain"))

PIN_CTRL, PINS, PIN_OE, PIN_OUT, PIN_OE_SET = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
PROGRAM = 0x1000


def check(what: str, got, want) -> None:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        sys.exit(1)


def layer_id(chip: Chip) -> None:
    check("ID", chip.read(ID, 1), [0x5E])
    print(f"     VERSION {chip.read(VERSION, 1)[0]:#04x}")
    for v in (0x00, 0xFF, 0xA5, 0x5A):
        chip.write(SCRATCH, [v])
        check(f"SCRATCH {v:#04x}", chip.read(SCRATCH, 1), [v])


def layer_gpio(chip: Chip) -> None:
    """GPIO override drives every JB pin; PINS reads them back (led[15:8] show them too)."""
    chip.write(PIN_CTRL, [0b01])
    chip.write(PIN_OE_SET, [0xFF])
    for v in (0x55, 0xAA, 0x0F, 0xF0, 0x00, 0xFF):
        chip.write(PIN_OUT, [v])
        time.sleep(0.01)
        check(f"PINS after PIN_OUT {v:#04x}", chip.read(PINS, 1), [v])
    check("PIN_OE", chip.read(PIN_OE, 1), [0xFF])
    chip.write(PIN_OE_SET, [0x00])
    chip.write(PIN_CTRL, [0b00])
    check("PINS released (pull-ups)", chip.read(PINS, 1), [0xFF])


def layer_sram(chip: Chip) -> None:
    chip.write(RUN, [0])
    rng = random.Random(1)
    data = bytes(rng.randrange(256) for _ in range(2048))
    chip.write(PROGRAM, data)
    got = bytes(chip.read(PROGRAM, len(data)))
    bad = [i for i in range(len(data)) if got[i] != data[i]]
    check(f"program SRAM, {len(data)} bytes: mismatched bytes", bad[:8], [])


def layer_engine(chip: Chip) -> None:
    """LI and ALU, then HALT: engine 0's registers and status after the run."""
    from emu.compiler import compile_desc
    from emu.desc import Description
    from emu.isa import Role
    d = Description("hello", parties=("a", "b"))
    d.line("l", owner="a", idle=1)
    d.li("r0", 0x1234)
    d.li("r1", 0x0FF0)
    d.alu("xor", "r2", "r0", "r1")
    d.halt()
    chip.load(compile_desc(d, check_timing=False))
    chip.set_role(0, Role.P0)
    chip.run(0b01)
    time.sleep(0.05)
    st = chip.status(0)
    check("halted", st["halted"], 1)
    check("r0-r2", chip.regs(0)[:3], [0x1234, 0x0FF0, 0x1234 ^ 0x0FF0])


def layer_uart_rx(chip: Chip, n: int = 4, wait: float = 5.0) -> None:
    """The library UART (115200 baud at 40 MHz) as rx on engine 0: line 0 is JB pin 0 (IO0).
    Have the microcontroller send `n` bytes to IO0 (3.3 V, 8N1); they arrive in RX."""
    from emu.compiler import compile_desc
    from emu.isa import Role
    from emu.protocols import get
    chip.load(compile_desc(get("uart").build(bit_cycles=347)))
    chip.set_role(0, Role.P1)
    chip.run(0b01)
    print(f"     waiting {wait:.0f} s for {n} bytes on IO0 (JB pin 1)...")
    t0 = time.time()
    while time.time() - t0 < wait:
        if chip.fifo_counts(0)[1] >= n:
            break
        time.sleep(0.1)
    words = chip.rx_pop(0, chip.fifo_counts(0)[1])
    print(f"     received {[hex(w) for w in words]}")
    st = chip.status(0)
    check("violations", st["viol"], 0)
    check("bytes received", len(words) >= n, True)


LAYERS = {"id": layer_id, "gpio": layer_gpio, "sram": layer_sram, "engine": layer_engine,
          "uart-rx": layer_uart_rx}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="the Basys 3's USB-UART, e.g. COM5")
    ap.add_argument("layer", choices=list(LAYERS) + ["all"])
    args = ap.parse_args()
    chip = Chip.open(args.port)
    for name in (["id", "gpio", "sram", "engine"] if args.layer == "all" else [args.layer]):
        print(f"--- {name}")
        LAYERS[name](chip)
    print("all checks passed")


if __name__ == "__main__":
    main()
