`default_nettype none

// Week 1 bring-up: a hardcoded UART TX, plus a trial harness for the SRAM
// program memory (prog_mem.v). Both get replaced by the protocol emulator and
// its image loader later.
//
//   ui_in[7:0]  byte to send / byte for the strobes below
//   uio_in[0]   SEND  rising edge: send ui_in on the UART
//   uio_in[1]   ADDR  rising edge: addr  <= ui_in
//   uio_in[2]   DATA  rising edge: wdata <= {wdata[23:0], ui_in}
//   uio_in[3]   WRITE rising edge: mem[addr] <= wdata
//   uio_in[4]   READ  rising edge: rdata <= mem[addr]
//   uio_in[5]   CHASE level: read mem[rdata[7:0]] every cycle. The memory's
//               output drives its own address: the engine's fetch-ahead
//               timing path, so the harden's STA covers it.
//   uio_in[6]   VIEW  level: uo_out = byte ui_in[1:0] of rdata
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

  // The uio pins come from outside and aren't synced to our clock, so pass
  // them through two flops first (synchronizer), then look for 0 -> 1 edges.
  // ui_in is not synchronized: the host holds it steady around each strobe.
  reg [6:0] s1, s2, s3;
  always @(posedge clk) begin
    if (!rst_n) begin
      s1 <= 7'b0;
      s2 <= 7'b0;
      s3 <= 7'b0;
    end else begin
      s1 <= uio_in[6:0];
      s2 <= s1;
      s3 <= s2;
    end
  end
  wire [6:0] rise = s2 & ~s3;
  wire chase = s2[5];
  wire view  = s2[6];

  wire tx, busy;

  uart_tx #(
      .CLKS_PER_BIT(434)
  ) u_uart_tx (
      .clk  (clk),
      .rst_n(rst_n),
      .start(rise[0]),
      .data (ui_in),
      .tx   (tx),
      .busy (busy)
  );

  // ---- SRAM trial harness ----
  reg  [7:0]  addr;
  reg  [31:0] wdata;
  wire [31:0] rdata;

  always @(posedge clk) begin
    if (!rst_n) begin
      addr  <= 8'h00;
      wdata <= 32'h0;
    end else begin
      if (rise[1]) addr  <= ui_in;
      if (rise[2]) wdata <= {wdata[23:0], ui_in};
    end
  end

  wire mem_we = rise[3];
  wire mem_re = rise[4] | chase;

  prog_mem u_mem (
      .clk  (clk),
      .en   (mem_we | mem_re),
      .we   (mem_we),
      .addr (chase ? rdata[7:0] : addr),
      .wdata(wdata),
      .rdata(rdata)
  );

  wire [7:0] view_byte = rdata[{ui_in[1:0], 3'b000} +: 8];

  assign uo_out  = view ? view_byte : {6'b0, busy, tx};
  assign uio_out = 0;
  assign uio_oe  = 0;  // all bidirectional pins are inputs for now

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in[7], rise[6:5], 1'b0};  // CHASE and VIEW are levels

endmodule
