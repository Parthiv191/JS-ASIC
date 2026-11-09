`default_nettype none

// Basys 3 wrapper: connects the board's switches/buttons/USB-UART to the
// Tiny Tapeout top module, the same way the TT demo board would.
//
//   sw[7:0]  byte to send       btnC  send
//   led[7:0] shows the switches btnU  reset
//   RsTx     UART out to the PC over the USB cable
module basys3_top (
    input  wire        clk,   // 100 MHz
    input  wire [15:0] sw,
    input  wire        btnC,
    input  wire        btnU,
    output wire [15:0] led,
    output wire        RsTx
);

  wire [7:0] uo_out;
  wire [7:0] uio_out, uio_oe;

  tt_um_prot_emu_SAP #(
      .CLKS_PER_BIT(868)  // 100 MHz / 115200 baud
  ) chip (
      .ui_in  (sw[7:0]),
      .uo_out (uo_out),
      .uio_in ({7'b0, btnC}),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk),
      .rst_n  (~btnU)  // buttons are high when pressed, rst_n is low to reset
  );

  assign RsTx = uo_out[0];
  assign led  = {8'b0, sw[7:0]};

endmodule
