`default_nettype none

// 256 x 32 program memory: two IHP RM_IHPSG13_1P_256x16 SRAM macros side by
// side, so one 32-bit ISA word is read or written per cycle.
//
// Read latency is one cycle: `rdata` shows mem[addr] after the clock edge that
// samples `en` (with `we` low), and holds that value until the next read. The
// engine hides the latency by presenting the NEXT pc in the cycle it executes
// the current instruction (fetch-ahead): branch targets and role-block skip
// counts are immediates, so the next pc is available combinationally.
//
// Single port: a cycle either reads or writes. A write does not update `rdata`.
module prog_mem (
    input  wire        clk,
    input  wire        en,     // access this cycle
    input  wire        we,     // with en: write wdata to addr; otherwise read addr
    input  wire [7:0]  addr,
    input  wire [31:0] wdata,
    output wire [31:0] rdata
);

  wire ren = en & ~we;

  RM_IHPSG13_1P_256x16_c2_bm_bist u_lo (
      .A_CLK      (clk),
      .A_MEN      (en),
      .A_WEN      (we),
      .A_REN      (ren),
      .A_ADDR     (addr),
      .A_DIN      (wdata[15:0]),
      .A_DLY      (1'b1),       // datasheet: must be tied high
      .A_DOUT     (rdata[15:0]),
      .A_BM       (16'hFFFF),   // whole-word writes
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(8'h00),
      .A_BIST_DIN (16'h0000),
      .A_BIST_BM  (16'h0000)
  );

  RM_IHPSG13_1P_256x16_c2_bm_bist u_hi (
      .A_CLK      (clk),
      .A_MEN      (en),
      .A_WEN      (we),
      .A_REN      (ren),
      .A_ADDR     (addr),
      .A_DIN      (wdata[31:16]),
      .A_DLY      (1'b1),
      .A_DOUT     (rdata[31:16]),
      .A_BM       (16'hFFFF),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(8'h00),
      .A_BIST_DIN (16'h0000),
      .A_BIST_BM  (16'h0000)
  );

endmodule
