`default_nettype none

// FPGA stand-in for the IHP SRAM macro RM_IHPSG13_1P_512x32_c2_bm_bist, inferred as block RAM in
// NO_CHANGE mode. It behaves as the macro's model (test/sram) for every access the design makes:
// one-cycle read latency, and A_DOUT holds the last read through writes and idle cycles. Two
// limits, each stopped in simulation: block RAM has byte write enables, not bit masks, so A_BM
// must be byte-granular; and a write with A_REN also high (which the macro writes through to
// A_DOUT) isn't supported. src/prog_mem.v writes whole words with A_REN low. BIST is ignored.
module RM_IHPSG13_1P_512x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [8:0]  A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output wire [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [8:0]  A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);

  (* ram_style = "block" *) reg [31:0] mem [0:511];
  reg [31:0] q = 32'h0;
  wire [3:0] be = {&A_BM[31:24], &A_BM[23:16], &A_BM[15:8], &A_BM[7:0]};
  wire en = A_MEN & (A_WEN | A_REN);
  integer i;

  always @(posedge A_CLK)
    if (en) begin
      if (A_WEN) begin
        for (i = 0; i < 4; i = i + 1)
          if (be[i]) mem[A_ADDR][8*i +: 8] <= A_DIN[8*i +: 8];
      end else begin
        q <= mem[A_ADDR];
      end
    end

  assign A_DOUT = q;

`ifndef SYNTHESIS
  always @(posedge A_CLK)
    if (A_MEN && A_WEN && (A_BM != {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}})) begin
      $display("sram_fpga: A_BM %h is not byte-granular; block RAM has byte enables only", A_BM);
      $finish;
    end
  always @(posedge A_CLK)
    if (A_MEN && A_WEN && A_REN) begin
      $display("sram_fpga: a write with A_REN high (write-through) isn't supported");
      $finish;
    end
`endif

endmodule
