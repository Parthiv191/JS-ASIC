`default_nettype none

// Basys 3 bring-up harness for tt_um_prot_emu_SAP (verification.md, FPGA layer; architecture §5
// bring-up order). The chip runs at the 40 MHz sign-off clock (chip-spec §7), made from the
// board's 100 MHz by an MMCM.
//
//   USB-UART (RsRx/RsTx)  the PC drives the host SPI port through uart_spi_bridge (1 Mbaud)
//   Pmod JB               uio[7:0], the protocol pins (IO0-IO7), with pull-ups
//   Pmod JC               debug: flash SCK, CS_N, MOSI, REPORT (uo_out[3:6])
//   btnC                  reset (rst_n low while pressed, and until the MMCM locks)
//   sw[15]                TESTMODE strap (ui_in[7]), read at reset
//   sw[14]                TESTMODE direction (ui_in[6])
//   sw[5:3]               ui_in[5:3]: unused, BOOT strap (keep 0), FLASH_MISO (no flash here)
//   sw[2:0]               ui_in[2:0] in TESTMODE only; otherwise the bridge's SCK, MOSI, CS_N
//   led[7:0]              uo_out (HOST_MISO, IRQ, ERR, flash x3, REPORT, ACT)
//   led[15:8]             uio_in, the protocol pins as the chip sees them
module basys3_top (
    input  wire        clk,     // 100 MHz
    input  wire        btnC,
    input  wire [15:0] sw,
    output wire [15:0] led,
    input  wire        RsRx,
    output wire        RsTx,
    inout  wire [7:0]  JB,
    output wire [3:0]  JC
);

  // ---- clock: 100 MHz -> 40 MHz (VCO 1000 MHz) ----
  wire clk40, locked;
`ifdef FPGA_SIM
  assign clk40 = clk;           // simulation drives the 40 MHz clock directly
  assign locked = 1'b1;
`else
  wire clk_fb, clk40_u;
  MMCME2_BASE #(
      .CLKIN1_PERIOD   (10.0),
      .CLKFBOUT_MULT_F (10.0),
      .DIVCLK_DIVIDE   (1),
      .CLKOUT0_DIVIDE_F(25.0)
  ) u_mmcm (
      .CLKIN1  (clk),
      .CLKFBIN (clk_fb),
      .CLKFBOUT(clk_fb),
      .CLKOUT0 (clk40_u),
      .LOCKED  (locked),
      .PWRDWN  (1'b0),
      .RST     (1'b0),
      .CLKOUT0B(), .CLKOUT1(), .CLKOUT1B(), .CLKOUT2(), .CLKOUT2B(), .CLKOUT3(), .CLKOUT3B(),
      .CLKOUT4(), .CLKOUT5(), .CLKOUT6(), .CLKFBOUTB()
  );
  BUFG u_bufg (.I(clk40_u), .O(clk40));
`endif

  // ---- reset: the chip synchronizes rst_n itself (architecture §2) ----
  wire rst_n = locked & ~btnC;

  // ---- host SPI from the PC ----
  wire host_sck, host_mosi, host_cs_n;
  wire [7:0] uo_out;
  uart_spi_bridge u_bridge (
      .clk (clk40),
      .rst_n(rst_n),
      .rx  (RsRx),
      .tx  (RsTx),
      .sck (host_sck),
      .mosi(host_mosi),
      .cs_n(host_cs_n),
      .miso(uo_out[0])
  );

  wire testmode = sw[15];
  wire [7:0] ui_in = {sw[15], sw[14], sw[5:3],
                      testmode ? sw[2:0] : {host_cs_n, host_mosi, host_sck}};

  // ---- the chip ----
  wire [7:0] uio_in, uio_out, uio_oe;
  tt_um_prot_emu_SAP u_chip (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk40),
      .rst_n  (rst_n)
  );

  // ---- protocol pins: tristate per pin ----
  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_uio
      assign JB[i] = uio_oe[i] ? uio_out[i] : 1'bz;
      assign uio_in[i] = JB[i];
`ifdef FPGA_SIM
      pullup (JB[i]);                // the XDC's PULLUP on the board
`endif
    end
  endgenerate

  assign JC = uo_out[6:3];
  assign led = {uio_in, uo_out};

endmodule
