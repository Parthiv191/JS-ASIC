`default_nettype none

// Top level, bring-up layers 1-4 (architecture §5):
//   1. TESTMODE: clock, reset and every pad, no SPI or SRAM needed (§2).
//   2. Host SPI with the global ID, VERSION, SCRATCH and STATUS registers (§4).
//   3. GPIO override and LOOPBACK: the host drives the pins through PIN_CTRL,
//      PIN_OUT and PIN_OE_SET, and reads them back through PINS and PIN_OE.
//   4. Host access to both SRAM columns (0x1000, 0x3000), which keeps the
//      2 x 512x32 layout (§3, §9) in the harden.
// Pins as in architecture §8.
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

  localparam [7:0] ID      = 8'h5E;
  localparam [7:0] VERSION = 8'h01;

  // ---- Reset: async assert, sync release (2 flops) ----
  // Every other flop resets through its own RESET_B pin.
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

  // ---- Pin drivers (architecture §2) ----
  // Each source has a "pull low" and a "drive high" flop per pin. The engines
  // will be two more sources; the host (GPIO override) is the only one so far.
  //   uio_oe  = OR of every source's flops (forced to 0 by LOOPBACK)
  //   uio_out = NOR of the pull-low flops (wired-AND)
  //   inside  = low ? 0 : high ? 1 : LOOPBACK ? 1 : uio_in, then 2 sync flops
  reg  [7:0] h_low, h_high;      // the host's drive flops
  reg        loopback;
  wire [7:0] drv_low  = h_low;
  wire [7:0] drv_high = h_high;
  wire [7:0] drv_oe   = drv_low | drv_high;
  wire [7:0] pin_in   = ~drv_low & (drv_high | {8{loopback}} | uio_in);

  // ---- Input synchronizers: 2 flops on every external input ----
  reg [7:0] ui_m, ui_s, uio_m, uio_s;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      ui_m  <= 8'h00;
      ui_s  <= 8'h00;
      uio_m <= 8'h00;
      uio_s <= 8'h00;
    end else begin
      ui_m  <= ui_in;
      ui_s  <= ui_m;
      uio_m <= pin_in;
      uio_s <= uio_m;
    end
  end

  // ---- Straps: sampled once, when the synchronizers hold post-reset values ----
  reg [1:0] strap_wait;
  reg       straps_done, testmode;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      strap_wait  <= 2'd0;
      straps_done <= 1'b0;
      testmode    <= 1'b0;
    end else if (!straps_done) begin
      strap_wait <= strap_wait + 2'd1;
      if (strap_wait == 2'd2) begin
        straps_done <= 1'b1;
        testmode    <= ui_s[7];
      end
    end
  end

  // ---- Host SPI ----
  wire [15:0] haddr;
  wire [7:0]  hwdata;
  reg  [7:0]  hrdata;
  wire        hwr, hrd, hframe_end, miso;

  host_spi u_spi (
      .clk(clk), .rst_n(rst_n_sync),
      .sck(ui_s[0]), .mosi(ui_s[1]), .cs(~ui_s[2]),
      .rdata(hrdata), .miso(miso),
      .addr(haddr), .wr(hwr), .wdata(hwdata), .rd(hrd), .frame_end(hframe_end)
  );

  wire in_glob = haddr[15:8] == 8'h00;
  wire in_col1 = haddr[15:11] == 5'b00010;  // 0x1000-0x17FF
  wire in_col2 = haddr[15:11] == 5'b00110;  // 0x3000-0x37FF

  // ---- Global registers ----
  reg [7:0] scratch, pin_out, pin_oe_set;
  reg       gpio_override;
  wire      gwr = hwr & in_glob;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      scratch       <= 8'h00;
      gpio_override <= 1'b0;
      loopback      <= 1'b0;
      pin_out       <= 8'h00;
      pin_oe_set    <= 8'h00;
    end else if (gwr) begin
      case (haddr[7:0])
        8'h02: scratch <= hwdata;
        8'h0B: {loopback, gpio_override} <= hwdata[1:0];
        8'h0E: pin_out    <= hwdata;
        8'h0F: pin_oe_set <= hwdata;
        default: ;
      endcase
    end
  end

  // The host's drive flops follow PIN_OUT / PIN_OE_SET while gpio_override is set.
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      h_low  <= 8'h00;
      h_high <= 8'h00;
    end else begin
      h_low  <= {8{gpio_override}} & pin_oe_set & ~pin_out;
      h_high <= {8{gpio_override}} & pin_oe_set &  pin_out;
    end
  end

  // No boot controller yet (Plus), so boot is always done and never fails.
  // host_err has no sources yet: the illegal accesses need RUN and MEM2_MODE.
  wire [7:0] status = {2'b00, loopback, testmode, 1'b0 /* mem2_busy */,
                       1'b0 /* host_err */, 1'b0 /* boot_err */, 1'b1 /* boot_done */};

  // ---- SRAM host port: bytes collect into a word, which commits on its 4th byte ----
  reg  [23:0] wbuf;
  reg  [2:0]  wmask;   // lanes 0-2 held in wbuf
  wire [1:0]  lane = haddr[1:0];
  wire        mem_wr = hwr & (in_col1 | in_col2);
  wire        commit = mem_wr & (lane == 2'd3) & (&wmask);
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      wbuf  <= 24'h0;
      wmask <= 3'b000;
    end else if (hframe_end || commit) begin
      wmask <= 3'b000;
    end else if (mem_wr) begin
      case (lane)
        2'd0: begin wbuf[7:0]   <= hwdata; wmask <= 3'b001; end  // a new word
        2'd1: begin wbuf[15:8]  <= hwdata; wmask[1] <= 1'b1; end
        2'd2: begin wbuf[23:16] <= hwdata; wmask[2] <= 1'b1; end
        default: ;
      endcase
    end
  end

  wire [31:0] mem_wdata = {hwdata, wbuf};
  wire [31:0] col1_q, col2_q;

  mem_port u_col1 (
      .clk(clk), .rst_n(rst_n_sync),
      .en((commit | hrd) & in_col1), .we(commit), .addr(haddr[10:2]),
      .wdata(mem_wdata), .rdata_q(col1_q)
  );

  mem_port u_col2 (
      .clk(clk), .rst_n(rst_n_sync),
      .en((commit | hrd) & in_col2), .we(commit), .addr(haddr[10:2]),
      .wdata(mem_wdata), .rdata_q(col2_q)
  );

  // ---- Read mux, registered (architecture §4: a multicycle path) ----
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) hrdata <= 8'h00;
    else if (in_glob)
      case (haddr[7:0])
        8'h00:   hrdata <= ID;
        8'h01:   hrdata <= VERSION;
        8'h02:   hrdata <= scratch;
        8'h03:   hrdata <= status;
        8'h0B:   hrdata <= {6'b0, loopback, gpio_override};
        8'h0C:   hrdata <= uio_s;    // PINS: what the engines see
        8'h0D:   hrdata <= drv_oe;   // PIN_OE: what the chip drives, before LOOPBACK
        default: hrdata <= 8'h00;
      endcase
    else if (in_col1) hrdata <= col1_q[{lane, 3'b000} +: 8];
    else if (in_col2) hrdata <= col2_q[{lane, 3'b000} +: 8];
    else hrdata <= 8'h00;
  end

  // ---- Outputs: every pin from a flop; the TESTMODE mux is the only gate ----
  // uo_out: 0 MISO, 1 IRQ, 2 ERR, 3 flash SCK, 4 flash CS_N, 5 flash MOSI, 6 REPORT, 7 ACT
  reg [7:0] uo_q;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) uo_q <= 8'b0101_0000;
    else             uo_q <= {1'b0, 1'b1, 1'b0, 1'b1, 1'b0, 1'b0, 1'b0, miso};
  end

  wire tm_drive = testmode & ui_s[6];   // uio outputs the registered ui_in
  wire tm_show  = testmode & ~ui_s[6];  // uo_out shows the registered uio_in

  assign uo_out  = tm_show  ? uio_s : uo_q;
  assign uio_out = tm_drive ? ui_s  : ~drv_low;
  assign uio_oe  = tm_drive ? 8'hFF : drv_oe & {8{~loopback}};

  wire _unused = &{ena, ui_s[5:3], 1'b0};

endmodule

// One SRAM macro plus the flops that drive it. Address, data and control are
// registered here, so the placer puts them next to the macro, and A_DOUT drives
// only the local register rdata_q (architecture §3). Run 41975807753 failed max
// slew/cap when one set of flops fed both macros ~450 um apart.
module mem_port #(
    parameter AW = 9
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          en,     // access this cycle
    input  wire          we,     // with en: write; otherwise read
    input  wire [AW-1:0] addr,
    input  wire [31:0]   wdata,
    output reg  [31:0]   rdata_q // the last word read, 2 cycles after en
);

  reg [AW-1:0] addr_q;
  reg [31:0]   wdata_q;
  reg          en_q, we_q, rd_d;
  wire [31:0]  dout;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      addr_q  <= {AW{1'b0}};
      wdata_q <= 32'h0;
      en_q    <= 1'b0;
      we_q    <= 1'b0;
      rd_d    <= 1'b0;
      rdata_q <= 32'h0;
    end else begin
      en_q <= en;
      we_q <= en & we;
      if (en) begin
        addr_q  <= addr;
        wdata_q <= wdata;
      end
      rd_d <= en_q & ~we_q;
      if (rd_d) rdata_q <= dout;
    end
  end

  prog_mem #(.AW(AW)) u_pm (
      .clk(clk), .en(en_q), .we(we_q), .addr(addr_q), .wdata(wdata_q), .rdata(dout)
  );

endmodule
