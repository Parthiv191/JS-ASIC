`default_nettype none

// FPGA stand-in for the IHP SRAM macro RM_IHPSG13_1P_512x32_c2_bm_bist, inferred as block RAM.
// Same ports and the same behaviour as the macro's model (test/sram): one-cycle read latency,
// A_DOUT holds the last read, and a write updates A_DOUT only when A_REN is also high. Block RAM
// has byte write enables, not bit masks, so A_BM must be byte-granular; the design writes whole
// words (src/prog_mem.v ties A_BM high). The BIST port is unused and ignored.
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
  wire [31:0] merged = {be[3] ? A_DIN[31:24] : mem[A_ADDR][31:24],
                        be[2] ? A_DIN[23:16] : mem[A_ADDR][23:16],
                        be[1] ? A_DIN[15:8]  : mem[A_ADDR][15:8],
                        be[0] ? A_DIN[7:0]   : mem[A_ADDR][7:0]};
  integer i;

  always @(posedge A_CLK) begin
    if (A_MEN && A_WEN) begin
      for (i = 0; i < 4; i = i + 1)
        if (be[i]) mem[A_ADDR][8*i +: 8] <= A_DIN[8*i +: 8];
      if (A_REN) q <= merged;
    end else if (A_MEN && A_REN) begin
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
`endif

endmodule
