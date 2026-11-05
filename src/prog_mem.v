`default_nettype none

// SRAM experiment E2: one RM_IHPSG13_1P_1024x32 macro = 1024 words x 32 bits,
// so one 32-bit ISA word per access without pairing two 16-bit macros.
//
// Read latency is one cycle: `rdata` shows mem[addr] after the clock edge that
// samples `en` (with `we` low), and holds that value until the next read.
// Single port: a cycle either reads or writes. A write does not update `rdata`.
module prog_mem #(
    parameter AW = 10
) (
    input  wire          clk,
    input  wire          en,     // access this cycle
    input  wire          we,     // with en: write wdata to addr; otherwise read addr
    input  wire [AW-1:0] addr,
    input  wire [31:0]   wdata,
    output wire [31:0]   rdata
);

  RM_IHPSG13_1P_1024x32_c2_bm_bist u_sram (
      .A_CLK      (clk),
      .A_MEN      (en),
      .A_WEN      (we),
      .A_REN      (en & ~we),
      .A_ADDR     (addr),
      .A_DIN      (wdata),
      .A_DLY      (1'b1),       // datasheet: must be tied high
      .A_DOUT     (rdata),
      .A_BM       (32'hFFFFFFFF),  // whole-word writes
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR({AW{1'b0}}),
      .A_BIST_DIN (32'h0),
      .A_BIST_BM  (32'h0)
  );

endmodule
