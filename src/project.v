`default_nettype none

// SRAM layout experiment (docs/design/architecture.md §3.3): two 32-bit-wide SRAM
// macros, one standing in for engine 0's program memory (u_mem) and one for the
// fuzz unit's corpus (u_fmem), plus the week-1 UART so the harness has a known
// output. With `define FLOP128` a 128 x 32 flop memory stands in for the
// watcher engine's program memory, to measure its area.
//
//   ui_in[7:0]  byte to send / byte for the strobes below
//   uio_in[0]   SEND  rising edge: send ui_in on the UART
//   uio_in[1]   ADDR  rising edge: addr <= {addr, ui_in} (send the high byte first)
//   uio_in[2]   DATA  rising edge: wdata <= {wdata[23:0], ui_in}
//   uio_in[3]   WRITE rising edge: mem[addr] <= wdata
//   uio_in[4]   READ  rising edge: rdata <= mem[addr]
//   uio_in[5]   CHASE level: read mem[rdata[AW-1:0]] every cycle (the engine's
//               fetch-ahead path: memory output drives its own address)
//   uio_in[6]   VIEW  level: uo_out = byte ui_in[1:0] of rdata (ui_in[2] = 1:
//               the flop memory's rdata, if present)
//   uio_in[7]   SEL   level: 0 = u_mem, 1 = u_fmem (and the flop memory)
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

  localparam AW = 10;

  // Reset asserts asynchronously and releases on a clock edge. Every flop
  // then resets through its own RESET_B pin, not through logic in front of D.
  reg rst_n_meta, rst_n_sync;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rst_n_meta <= 1'b0;
      rst_n_sync <= 1'b0;
    end else begin
      rst_n_meta <= 1'b1;
      rst_n_sync <= rst_n_meta;
    end
  end

  // Synchronize the strobes and levels, then look for 0 -> 1 edges.
  reg [7:0] s1, s2, s3;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      s1 <= 8'b0;
      s2 <= 8'b0;
      s3 <= 8'b0;
    end else begin
      s1 <= uio_in;
      s2 <= s1;
      s3 <= s2;
    end
  end
  wire [7:0] rise = s2 & ~s3;
  wire chase = s2[5];
  wire view  = s2[6];
  wire sel   = s2[7];

  wire tx, busy;

  uart_tx #(
      .CLKS_PER_BIT(434)
  ) u_uart_tx (
      .clk  (clk),
      .rst_n(rst_n_sync),
      .start(rise[0]),
      .data (ui_in),
      .tx   (tx),
      .busy (busy)
  );

  // ---- SRAM trial harness ----
  // Each macro gets its own port (mem_port below): its own address, data and
  // control flops, and a local register on its output. The real chip works the
  // same way (one client per macro), and it keeps every macro pin's net short:
  // run 41975807753 failed max slew/cap when one set of flops fed both macros
  // ~450 um apart and A_DOUT ran to the output pins.
  wire mem_we = rise[3];
  wire mem_re = rise[4];
  wire [31:0] rdata_a, rdata_b;
  wire [AW-1:0] addr;   // u_fmem's address and data, shared with the flop memory below
  wire [31:0]   wdata;

  mem_port #(.AW(AW)) u_mem (
      .clk(clk), .rst_n(rst_n_sync), .sel_me(~sel), .ld_addr(rise[1]), .ld_data(rise[2]),
      .wr(mem_we), .rd(mem_re), .chase(chase), .byte_in(ui_in), .rdata_q(rdata_a),
      .addr_o(), .wdata_o()
  );

  mem_port #(.AW(AW)) u_fmem (
      .clk(clk), .rst_n(rst_n_sync), .sel_me(sel), .ld_addr(rise[1]), .ld_data(rise[2]),
      .wr(mem_we), .rd(mem_re), .chase(chase), .byte_in(ui_in), .rdata_q(rdata_b),
      .addr_o(addr), .wdata_o(wdata)
  );

`ifdef FLOP128
  // Stand-in for a 128-word flop program memory: same one-cycle read latency,
  // rdata holds between reads. Written and read alongside u_fmem.
  reg [31:0] fmem [0:127];
  reg [31:0] rdata_f;
  always @(posedge clk) begin
    if (sel & mem_we) fmem[addr[6:0]] <= wdata;
  end
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) rdata_f <= 32'h0;
    else if (sel & mem_re) rdata_f <= fmem[addr[6:0]];
  end
`else
  wire [31:0] rdata_f = 32'h0;
`endif

  wire [31:0] rdata = (view & ui_in[2]) ? rdata_f : (sel ? rdata_b : rdata_a);
  wire [7:0] view_byte = rdata[{ui_in[1:0], 3'b000} +: 8];

  assign uo_out  = view ? view_byte : {6'b0, busy, tx};
  assign uio_out = 0;
  assign uio_oe  = 0;  // all bidirectional pins are inputs for now

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in[7:3], rise[7:5], 1'b0};  // CHASE, VIEW and SEL are levels

endmodule

// One SRAM macro plus the flops that drive it. Address, data and control are
// registered here, so the placer puts them next to the macro; the output is
// captured by a local register (rdata_q). CHASE drives the macro's address from
// its own output, combinationally: the engine's fetch-ahead path.
module mem_port #(
    parameter AW = 9
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          sel_me,   // this macro is selected
    input  wire          ld_addr,  // shift byte_in into the address
    input  wire          ld_data,  // shift byte_in into the write data
    input  wire          wr,       // write strobe
    input  wire          rd,       // read strobe
    input  wire          chase,    // level: read mem[dout] every cycle
    input  wire [7:0]    byte_in,
    output reg  [31:0]   rdata_q,
    output wire [AW-1:0] addr_o,
    output wire [31:0]   wdata_o
);

  reg [AW-1:0] addr;
  reg [31:0]   wdata;
  reg          en_q, we_q, chase_q, rd_d;
  wire [31:0]  dout;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      addr    <= {AW{1'b0}};
      wdata   <= 32'h0;
      en_q    <= 1'b0;
      we_q    <= 1'b0;
      chase_q <= 1'b0;
      rd_d    <= 1'b0;
      rdata_q <= 32'h0;
    end else begin
      if (sel_me & ld_addr) addr  <= {addr[AW-9:0], byte_in};
      if (sel_me & ld_data) wdata <= {wdata[23:0], byte_in};
      en_q    <= sel_me & (wr | rd | chase);
      we_q    <= sel_me & wr;
      chase_q <= sel_me & chase;
      rd_d    <= en_q & ~we_q;
      if (rd_d) rdata_q <= dout;
    end
  end

  assign addr_o  = addr;
  assign wdata_o = wdata;

  prog_mem #(.AW(AW)) u_pm (
      .clk  (clk),
      .en   (en_q),
      .we   (we_q),
      .addr (chase_q ? dout[AW-1:0] : addr),
      .wdata(wdata),
      .rdata(dout)
  );

endmodule
