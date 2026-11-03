# Vendored SRAM simulation models

Byte-for-byte copies of the IHP Open PDK's behavioural models for the
`RM_IHPSG13_1P_256x16_c2_bm_bist` single-port SRAM macro, for simulation only.
The RTL and gate-level cocotb runs need them; CI's RTL test job has no PDK
installed. LibreLane never reads these files: it gets the macro's LEF, GDS and
liberty from the PDK through `MACROS` in `src/config.json`.

| | |
|---|---|
| Upstream | [IHP-GmbH/IHP-Open-PDK](https://github.com/IHP-GmbH/IHP-Open-PDK), `ihp-sg13g2/libs.ref/sg13g2_sram/verilog/` (`ihp-sg13cmos5l/libs.ref/sg13cmos5l_sram` is a symlink to it) |
| Revision | `2bbec755dc67ca3db0261c3d6163e15735d66710`, the one `tt-gds-action@ihp-cmos5l` installs (`install_sg13cmos5l.sh`) |
| Licence | Apache-2.0, "Copyright 2025 IHP PDK Authors", header kept in each file |
| Modifications | None |

Compile with `-DFUNCTIONAL` (the timing branch needs SDF-style delayed nets).
The array and `A_DOUT` power up X, as in silicon.
