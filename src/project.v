`default_nettype none

// Top level, bring-up layers 1-5 (architecture §5):
//   1. TESTMODE: clock, reset and every pad, no SPI or SRAM needed (§2).
//   2. Host SPI with the global ID, VERSION, SCRATCH and STATUS registers (§4).
//   3. GPIO override and LOOPBACK: the host drives the pins through PIN_CTRL,
//      PIN_OUT and PIN_OE_SET, and reads them back through PINS and PIN_OE.
//   4. Host access to both SRAM columns (0x1000, 0x3000).
//   5. Engine 0 running a program from column 1 (engine.v), with the shared
//      PROF, LINE, LANE, ABORT_PC and BUS tables.
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
  // 1A sizes: program / 64, profiles, lanes, engines
  localparam [31:0] SIZES  = {8'd2, 8'd4, 8'd8, 8'd8};

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
  // Each source has a "pull low" and a "drive high" flop per pin: the host
  // (GPIO override) and the engines. The override replaces the engines' drives.
  //   uio_oe  = OR of the sources' flops (forced to 0 by LOOPBACK)
  //   uio_out = NOR of the pull-low flops (wired-AND)
  //   inside  = low ? 0 : high ? 1 : LOOPBACK ? 1 : uio_in, then 2 sync flops
  reg  [7:0] h_low, h_high;      // the host's drive flops
  wire [7:0] e0_low, e0_high;    // engine 0's drive flops
  reg        loopback, gpio_override;
  wire [7:0] drv_low  = h_low  | (gpio_override ? 8'h00 : e0_low);
  wire [7:0] drv_high = h_high | (gpio_override ? 8'h00 : e0_high);
  wire [7:0] drv_oe   = drv_low | drv_high;
  wire [7:0] pin_in   = ~drv_low & (drv_high | {8{loopback}} | uio_in);

  // ---- Input synchronizers: 2 flops on every external input ----
  // (Each engine has its own synchronizer for the uio pins, preloaded at RUN.)
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

  // ---- Phase: engine e may read column 1 only on cycles with ph == e (isa §4.0) ----
  reg ph;
  always @(posedge clk or negedge rst_n_sync)
    if (!rst_n_sync) ph <= 1'b0;
    else             ph <= ~ph;

  // ---- Host SPI ----
  wire [15:0] haddr;
  wire [7:0]  hwdata;
  reg  [7:0]  hrdata;
  wire        hwr, hrd, hframe_end, miso, htx_valid, hsent;
  wire [15:0] htx_addr, hsent_addr;

  host_spi u_spi (
      .clk(clk), .rst_n(rst_n_sync),
      .sck(ui_s[0]), .mosi(ui_s[1]), .cs(~ui_s[2]),
      .rdata(hrdata), .miso(miso),
      .addr(haddr), .wr(hwr), .wdata(hwdata), .rd(hrd), .frame_end(hframe_end),
      .tx_addr(htx_addr), .tx_valid(htx_valid), .sent(hsent), .sent_addr(hsent_addr)
  );

  wire       in_glob  = haddr[15:8] == 8'h00;
  wire       in_table = haddr[15:8] == 8'h04;
  wire       in_col1  = haddr[15:11] == 5'b00010;  // 0x1000-0x17FF
  wire       in_col2  = haddr[15:11] == 5'b00110;  // 0x3000-0x37FF
  wire       in_eng0  = haddr[15:8] == 8'h20;
  wire [7:0] off      = haddr[7:0];
  wire       gwr      = hwr & in_glob;
  wire       ewr      = hwr & in_eng0;

  // ---- Engine 0 ----
  wire        e0_running, e0_halted, e0_irq, e0_fetch;
  wire [8:0]  e0_fetch_addr, e0_pc, e0_fv_pc;
  wire [3:0]  e0_fv_kind;
  wire [15:0] e0_viol, e0_reg;
  wire [5:0]  e0_evt;
  wire [11:0] e0_irq_id;
  wire [23:0] e0_now, e0_tb;
  wire [31:0] col1_dout;
  wire        any_running = e0_running;

  // ---- Global registers and the shared tables ----
  reg  [7:0]  scratch, pin_out, pin_oe_set;
  reg  [1:0]  run, run_eff;   // run_eff: RUN, taking effect on the next ph = 0 cycle
  reg         host_err;
  reg  [63:0] line_cfg;       // LINE[0..7]
  reg  [383:0] prof_all;      // PROF[0..7], 6 bytes each (min, nom, max LE); PROF0 is ignored
  reg  [127:0] lane_all;      // LANE[0..3], 4 bytes each
  reg  [8:0]   abort_pc;
  reg  [15:0]  bus_cfg;       // {BUS_CONFIRM, BUS_AB (b[5:3], a[2:0]), BUS_KIND[1:0]} (0x54-0x56)
  reg  [1:0]  e0_role;
  reg  [2:0]  e0_reg_sel;
  reg  [7:0]  e0_reg_lo;      // REG_DATA low byte, written with the high byte
  reg  [7:0]  e0_tx_lo;       // TXDATA low byte; the high byte pushes the word
  wire [2:0]  e0_tx_count;
  wire [3:0]  e0_rx_count;
  wire [15:0] e0_rx_head, e0_rx_next;

  wire soft_reset0 = gwr & (off == 8'h05) & hwdata[0];
  wire irq_clr0    = gwr & (off == 8'h06) & hwdata[0];
  wire e0_reg_we   = ewr & (off == 8'h15) & ~e0_running;
  // TXDATA (0x30, pair) pushes on its high byte; RXDATA (0x32, pair) pops once its high byte has
  // fully shifted out to the host (architecture §4), so read-ahead never pops
  wire e0_tx_push  = ewr & (off == 8'h31);
  wire e0_rx_pop   = hsent & (hsent_addr == 16'h2033);
  wire [15:0] e0_viol_clr = {(ewr && off == 8'h07) ? hwdata : 8'h00,
                             (ewr && off == 8'h06) ? hwdata : 8'h00};

  // Illegal accesses (architecture §4); the rest need later blocks
  wire mem_wr = hwr & (in_col1 | in_col2);
  wire refuse = (any_running & ((hwr & in_table) | (hwr & in_col1) | (ewr & off == 8'h15))) |
                (e0_tx_push & (e0_tx_count == 3'd4));   // TX full: the word is dropped

  integer k;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      scratch       <= 8'h00;
      gpio_override <= 1'b0;
      loopback      <= 1'b0;
      pin_out       <= 8'h00;
      pin_oe_set    <= 8'h00;
      run           <= 2'b00;
      run_eff       <= 2'b00;
      host_err      <= 1'b0;
      e0_role       <= 2'd0;
      e0_reg_sel    <= 3'd0;
      e0_reg_lo     <= 8'h00;
      e0_tx_lo      <= 8'h00;
      for (k = 0; k < 8; k = k + 1) line_cfg[8*k +: 8] <= 8'h68 + k[7:0];  // released OD
      prof_all      <= 384'h0;
      lane_all      <= 128'h0;
      abort_pc      <= 9'd0;
      bus_cfg       <= 16'h0;
    end else begin
      if (ph) run_eff <= run;   // the next cycle has ph = 0
      if (soft_reset0) run[0] <= 1'b0;
      if (refuse) host_err <= 1'b1;
      if (gwr) begin
        case (off)
          8'h02: scratch <= hwdata;
          8'h04: run <= hwdata[1:0] & 2'b01;  // engine 1 comes with layer 6
          8'h07: if (hwdata[0]) host_err <= 1'b0;
          8'h0B: {loopback, gpio_override} <= hwdata[1:0];
          8'h0E: pin_out    <= hwdata;
          8'h0F: pin_oe_set <= hwdata;
          default: ;
        endcase
      end
      // the shared tables (0x0400), written only while no engine runs
      if (hwr && in_table && !any_running) begin
        if (off < 8'h30)                    prof_all[{off[5:0], 3'd0} +: 8] <= hwdata;
        else if (off < 8'h38)               line_cfg[{off[2:0], 3'd0} +: 8] <= hwdata;
        else if (off < 8'h48)               lane_all[{off[3:0] - 4'h8, 3'd0} +: 8] <= hwdata;
        else if (off == 8'h4A)              abort_pc[7:0] <= hwdata;
        else if (off == 8'h4B)              abort_pc[8]   <= hwdata[0];
        else if (off == 8'h54)              bus_cfg[1:0]  <= hwdata[1:0];
        else if (off == 8'h55)              bus_cfg[7:2]  <= hwdata[5:0];
        else if (off == 8'h56)              bus_cfg[15:8] <= hwdata;
      end
      if (ewr) begin
        case (off)
          8'h00: e0_role    <= hwdata[1:0];
          8'h12: e0_reg_sel <= hwdata[2:0];
          8'h14: e0_reg_lo  <= hwdata;
          8'h30: e0_tx_lo   <= hwdata;
          default: ;
        endcase
      end
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

  engine #(.E(0)) u_eng0 (
      .clk(clk), .rst_n(rst_n_sync), .ph(ph), .run(run_eff[0]), .soft_reset(soft_reset0),
      .role(e0_role), .line_cfg(line_cfg), .pin_in(pin_in),
      .lane_cfg({lane_all[119:96], lane_all[87:64], lane_all[55:32], lane_all[23:0]}),
      .prof_tab(prof_all[383:48]), .abort_pc(abort_pc), .bus_cfg(bus_cfg),
      .fetch(e0_fetch), .fetch_addr(e0_fetch_addr), .sram_dout(col1_dout),
      .drv_low(e0_low), .drv_high(e0_high),
      .reg_sel(e0_reg_sel), .reg_we(e0_reg_we), .reg_wdata({hwdata, e0_reg_lo}),
      .reg_rdata(e0_reg), .viol_clr(e0_viol_clr), .irq_clr(irq_clr0),
      .running(e0_running), .halted(e0_halted), .viol(e0_viol), .evt(e0_evt),
      .irq(e0_irq), .irq_id(e0_irq_id), .xpc(e0_pc), .fv_pc(e0_fv_pc), .fv_kind(e0_fv_kind),
      .now_o(e0_now), .tb_o(e0_tb),
      .tx_push(e0_tx_push), .tx_wdata({hwdata, e0_tx_lo}), .tx_count(e0_tx_count),
      .rx_pop(e0_rx_pop), .rx_count(e0_rx_count), .rx_head(e0_rx_head), .rx_next(e0_rx_next)
  );

  // No boot controller yet (Plus), so boot is always done and never fails.
  wire [7:0] status = {2'b00, loopback, testmode, 1'b0 /* mem2_busy */,
                       host_err, 1'b0 /* boot_err */, 1'b1 /* boot_done */};

  // ---- SRAM host port: bytes collect into a word, which commits on its 4th byte ----
  reg  [23:0] wbuf;
  reg  [2:0]  wmask;   // lanes 0-2 held in wbuf
  wire [1:0]  lane = haddr[1:0];
  wire        commit = mem_wr & (lane == 2'd3) & (&wmask) & ~(in_col1 & any_running);
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

  mem_port #(.PROG(1)) u_col1 (
      .clk(clk), .rst_n(rst_n_sync),
      .en((commit | hrd) & in_col1), .we(commit), .addr(haddr[10:2]),
      .wdata(mem_wdata), .rdata_q(col1_q),
      .f_en(e0_fetch), .f_addr(e0_fetch_addr), .dout(col1_dout)
  );

  mem_port u_col2 (
      .clk(clk), .rst_n(rst_n_sync),
      .en((commit | hrd) & in_col2), .we(commit), .addr(haddr[10:2]),
      .wdata(mem_wdata), .rdata_q(col2_q),
      .f_en(1'b0), .f_addr(9'd0), .dout()
  );

  // ---- Read path (architecture §4: a multicycle path) ----
  // A read is captured 4 cycles after `hrd` (the SRAM needs 3), and the frame
  // gives it >= 60. Reading the low byte of a multi-byte register snapshots it:
  // its other bytes come from `snap`, taken in the same cycle.
  reg [3:0]  rd_q;
  reg [23:0] snap;
  reg [23:0] e_val;    // the engine register that `off` falls in
  reg [1:0]  e_byte;   // which byte of it
  reg        e_multi;  // multi-byte: the low byte snapshots, the rest read `snap`
  always @* begin
    e_val = 24'h0; e_byte = 2'd0; e_multi = 1'b0;
    case (off)
      // STATUS: running, halted, TX full, TX empty, RX full, RX empty; FIFO counts: TX [3:0], RX [7:4]
      8'h02:        e_val = {18'h0, e0_rx_count == 4'd0, e0_rx_count == 4'd8,
                             e0_tx_count == 3'd0, e0_tx_count == 3'd4, e0_halted, e0_running};
      8'h03:        e_val = {16'h0, e0_rx_count, 1'b0, e0_tx_count};
      // RXDATA. Its low byte is fetched one byte ahead: if the byte now shifting out is RXDATA's
      // high byte, it pops when it finishes, so the low byte comes from the next entry
      8'h32:        e_val = (htx_valid && htx_addr == 16'h2033) ?
                              ((e0_rx_count >= 4'd2) ? {8'h0, e0_rx_next} : 24'h0) :
                              ((e0_rx_count != 4'd0) ? {8'h0, e0_rx_head} : 24'h0);
      8'h33:        e_val = (e0_rx_count != 4'd0) ? {16'h0, e0_rx_head[15:8]} : 24'h0;
      8'h04, 8'h05: begin e_val = {15'h0, e0_pc};      e_multi = 1'b1; end
      8'h06, 8'h07: begin e_val = {8'h0, e0_viol};     e_multi = 1'b1; end
      8'h08:        e_val = {18'h0, e0_evt};
      8'h0A, 8'h0B: begin e_val = {12'h0, e0_irq_id};  e_multi = 1'b1; end
      8'h0C, 8'h0D: begin e_val = {15'h0, e0_fv_pc};   e_multi = 1'b1; end
      8'h0E:        e_val = {20'h0, e0_fv_kind};
      8'h14, 8'h15: begin e_val = {8'h0, e0_reg};      e_multi = 1'b1; end
      8'h16, 8'h17, 8'h18: begin e_val = e0_tb;        e_multi = 1'b1; end
      8'h1A, 8'h1B, 8'h1C: begin e_val = e0_now;       e_multi = 1'b1; end
      default: ;
    endcase
    case (off)
      8'h05, 8'h07, 8'h0B, 8'h0D, 8'h15, 8'h17, 8'h1B: e_byte = 2'd1;
      8'h18, 8'h1C:                                    e_byte = 2'd2;
      default:                                         e_byte = 2'd0;
    endcase
  end

  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) begin
      rd_q   <= 4'h0;
      hrdata <= 8'h00;
      snap   <= 24'h0;
    end else begin
      rd_q <= {rd_q[2:0], hrd};
      if (rd_q[3]) begin
        if (in_glob)
          case (off)
            8'h00:   hrdata <= ID;
            8'h01:   hrdata <= VERSION;
            8'h02:   hrdata <= scratch;
            8'h03:   hrdata <= status;
            8'h04:   hrdata <= {6'b0, run};
            8'h06:   hrdata <= {7'b0, e0_irq};
            8'h0B:   hrdata <= {6'b0, loopback, gpio_override};
            8'h0C:   hrdata <= uio_s;    // PINS: the synchronized pins
            8'h0D:   hrdata <= drv_oe;   // PIN_OE: what the chip drives, before LOOPBACK
            8'h1A:   hrdata <= SIZES[7:0];
            8'h1B:   hrdata <= SIZES[15:8];
            8'h1C:   hrdata <= SIZES[23:16];
            8'h1D:   hrdata <= SIZES[31:24];
            default: hrdata <= 8'h00;
          endcase
        else if (in_eng0) begin
          if (e_multi && e_byte != 2'd0) hrdata <= snap[{e_byte, 3'b000} +: 8];
          else                           hrdata <= e_val[7:0];
          if (e_multi && e_byte == 2'd0) snap <= e_val;
        end
        else if (in_col1) hrdata <= col1_q[{lane, 3'b000} +: 8];
        else if (in_col2) hrdata <= col2_q[{lane, 3'b000} +: 8];
        else hrdata <= 8'h00;
      end
    end
  end

  // ---- Outputs: every pin from a flop; the TESTMODE mux is the only gate ----
  // uo_out: 0 MISO, 1 IRQ, 2 ERR, 3 flash SCK, 4 flash CS_N, 5 flash MOSI, 6 REPORT, 7 ACT
  reg [7:0] uo_q;
  always @(posedge clk or negedge rst_n_sync) begin
    if (!rst_n_sync) uo_q <= 8'b0101_0000;
    else             uo_q <= {e0_running, 1'b1, 1'b0, 1'b1, 1'b0, |e0_viol, e0_irq, miso};
  end

  wire tm_drive = testmode & ui_s[6];   // uio outputs the registered ui_in
  wire tm_show  = testmode & ~ui_s[6];  // uo_out shows the registered uio_in

  assign uo_out  = tm_show  ? uio_s : uo_q;
  assign uio_out = tm_drive ? ui_s  : ~drv_low;
  assign uio_oe  = tm_drive ? 8'hFF : drv_oe & {8{~loopback}};

  wire _unused = &{ena, ui_s[5:3], 1'b0};

endmodule

// One SRAM macro plus the flops that drive it. The host's address, data and
// control are registered here, so the placer puts them next to the macro, and
// A_DOUT drives only flops: the local register rdata_q, and in column 1 the
// engines' prefetch registers (architecture §3). With PROG = 1 an engine fetch
// (f_en, f_addr, straight from the engine) takes the macro for that cycle,
// ahead of the host. Run 41975807753 failed max slew/cap when one set of flops
// fed both macros ~450 um apart.
module mem_port #(
    parameter AW   = 9,
    parameter PROG = 0
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          en,     // host access this cycle
    input  wire          we,     // with en: write; otherwise read
    input  wire [AW-1:0] addr,
    input  wire [31:0]   wdata,
    output reg  [31:0]   rdata_q,// the last word the host read, 3 cycles after en
    input  wire          f_en,   // engine fetch this cycle (PROG only)
    input  wire [AW-1:0] f_addr,
    output wire [31:0]   dout    // A_DOUT, to the engines' prefetch registers
);

  reg [AW-1:0] addr_q;
  reg [31:0]   wdata_q;
  reg          en_q, we_q, rd_d;

  wire fe = (PROG != 0) & f_en;

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
      rd_d <= en_q & ~we_q & ~fe;
      if (rd_d) rdata_q <= dout;
    end
  end

  prog_mem #(.AW(AW)) u_pm (
      .clk(clk), .en(fe | en_q), .we(~fe & we_q), .addr(fe ? f_addr : addr_q),
      .wdata(wdata_q), .rdata(dout)
  );

endmodule
