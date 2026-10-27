`default_nettype none

// Week 1 bring-up: a hardcoded UART TX, just to get something through the
// whole flow. This gets replaced by the protocol emulator later.
//
//   ui_in[7:0]  byte to send
//   uio_in[0]   send (rising edge starts a byte)
//   uo_out[0]   UART TX
//   uo_out[1]   busy
module tt_um_prot_emu_SAP (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // The send pin comes from outside and isn't synced to our clock, so pass it
  // through two flops first (synchronizer), then look for a 0 -> 1 edge.
  reg [2:0] send_sync;
  always @(posedge clk) begin
    if (!rst_n) send_sync <= 3'b000;
    else        send_sync <= {send_sync[1:0], uio_in[0]};
  end
  wire send_pulse = send_sync[1] & ~send_sync[2];

  wire tx, busy;

  uart_tx #(
      .CLKS_PER_BIT(434)
  ) u_uart_tx (
      .clk  (clk),
      .rst_n(rst_n),
      .start(send_pulse),
      .data (ui_in),
      .tx   (tx),
      .busy (busy)
  );

  assign uo_out  = {6'b0, busy, tx};
  assign uio_out = 0;
  assign uio_oe  = 0;  // all bidirectional pins are inputs for now

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in[7:1], 1'b0};

endmodule
