# Testbench

cocotb tests for `tt_um_prot_emu_SAP` (`test.py`, wrapped by `tb.v`). See the
[Tiny Tapeout testing guide](https://tinytapeout.com/hdl/testing/) for background.

| Test | Checks |
|---|---|
| `test_idle_high` | TX idles high, not busy |
| `test_send_one_byte`, `test_send_several_bytes` | UART frames decode to the bytes sent |
| `test_mem_word_roundtrip` | 32-bit words survive in both SRAM macros at every address bit |
| `test_mem_chase_follows_pointers` | the macro's output drives its own address every cycle: the memory part of the engine's fetch path (isa §4.0) |
| `test_uart_still_works_with_mem_view_off` | the memory harness leaves the UART pins alone |

`reset()` holds `rst_n` low for 10 cycles. The design releases its reset two
clocks after `rst_n` rises (`src/project.v`).

## SRAM models

`sram/` holds the IHP PDK's behavioural models of the SRAM macro; see
[sram/README.md](sram/README.md). The Makefile adds them and `-DFUNCTIONAL` to
both RTL and gate-level runs.

## How to run

RTL simulation (CI runs this with Icarus):

```sh
make -B
make -B SIM=verilator EXTRA_ARGS=--timing   # Verilator needs --timing for the models' delays
```

Gate-level simulation needs a netlist and the PDK's cell models:

1. Download the `tt_submission` artifact of a `gds` run (`gh run download <run-id> -n tt_submission`)
   and copy its `tt_um_prot_emu_SAP.v` to `gate_level_netlist.v` (gitignored).
2. Point `PDK_ROOT` at a directory holding
   `ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/{sg13cmos5l_stdcell.v,sg13cmos5l_udp.v}`
   and `ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v`,
   from the IHP-Open-PDK revision in [sram/README.md](sram/README.md).

```sh
make -B GATES=yes PDK_ROOT=/path/to/pdk
```

Use Icarus for gate-level runs. It is 4-state, so it shows X propagation that
Verilator's 2-state simulation hides. CI's `gl_test` job does the same.

If you wish to save the waveform in VCD format instead of FST format, edit tb.v
to use `$dumpfile("tb.vcd");` and then run:

```sh
make -B FST=
```

This will generate `tb.vcd` instead of `tb.fst`.

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```
