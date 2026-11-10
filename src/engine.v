`default_nettype none

// One protocol engine (isa.md). This version runs the untimed instructions:
// NOP, LI, ALU, BR, ROLE_IF, MARK, RAW, HALT. The timed steps (COND, HOLD,
// XFER), the FIFOs (PUSH, POP), CRC, FZ and PROF come in later layers; until
// then they halt the engine as ILLEGAL.
//
// Fetch and issue (isa §4.0): own cycles are those with ph == E. An
// instruction starts only on an own cycle, presents its successor's address in
// its start cycle, and the word lands in `pf` two cycles later. Every untimed
// instruction therefore costs U = 2, and RAW DELAY n costs n rounded up to a
// multiple of 2.
//
// The tr_* signals are the RTL trace port (architecture §5): simulation only,
// nothing loads them, so synthesis removes them.
module engine #(
    parameter E = 0                   // engine index: own cycles are ph == E
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        ph,            // free-running phase bit
    input  wire        run,           // RUN, already aligned to a ph = 0 cycle
    input  wire        soft_reset,    // pulse: back to the reset state
    input  wire [1:0]  role,          // P0 0, P1 1, MON 2
    input  wire [63:0] line_cfg,      // LINE[0..7]: pin[2:0] od[3] owner[5:4] idle[6] gated[7]
    input  wire [7:0]  pin_in,        // every pin as it is now (drives merged, before sync)

    // program SRAM (column 1)
    output wire        fetch,         // read fetch_addr this cycle
    output wire [8:0]  fetch_addr,
    input  wire [31:0] sram_dout,     // A_DOUT: drives only pf

    // pin drive flops, one pair per pin (the line -> pin map is applied before them)
    output reg  [7:0]  drv_low,
    output reg  [7:0]  drv_high,

    // host
    input  wire [2:0]  reg_sel,
    input  wire        reg_we,        // write reg_wdata to r[reg_sel] (only while not running)
    input  wire [15:0] reg_wdata,
    output wire [15:0] reg_rdata,
    input  wire [15:0] viol_clr,      // W1C
    input  wire        irq_clr,
    output wire        running,
    output reg         halted,
    output reg  [15:0] viol,
    output reg  [5:0]  evt,
    output reg         irq,
    output reg  [11:0] irq_id,
    output reg  [8:0]  xpc,           // pc of the instruction executing (or last executed)
    output reg  [8:0]  fv_pc,
    output reg  [3:0]  fv_kind,
    output wire [23:0] now_o,
    output wire [23:0] tb_o
);

  // ---- opcodes (isa §3) ----
  localparam [4:0] OP_NOP = 5'd0, OP_LI = 5'd4, OP_ALU = 5'd5, OP_BR = 5'd6, OP_ROLE_IF = 5'd7,
                   OP_MARK = 5'd10, OP_RAW = 5'd11, OP_HALT = 5'd12;
  localparam [3:0] RAW_SET = 4'd0, RAW_REL = 4'd1, RAW_GET = 4'd2, RAW_DELAY = 4'd3,
                   RAW_JPIN = 4'd4, RAW_EVT = 4'd5;
  localparam [3:0] BR_ALWAYS = 4'd0, BR_Z = 4'd1, BR_NZ = 4'd2, BR_TO = 4'd3, BR_ROLE = 4'd4;
  localparam [3:0] K_ILLEGAL = 4'd10;
  localparam [22:0] AGE_MAX = 23'h400000;  // 2^22: the 24-bit time clamp (isa §4.1)

  // ---- run state ----
  reg  started;                     // RUN seen; cleared when RUN falls
  wire own = (ph == E[0]);
  wire s0  = run & ~started & own;  // first own cycle after RUN rises
  assign running = run & started & ~halted;

  // ---- fetch and the prefetch register ----
  reg  [31:0] pf;
  reg         pf_valid, fetch_d;
  reg  [8:0]  fpc;                  // address of the last fetch = pc of the word in pf
  reg  [10:0] skip;                 // own cycles still to wait (RAW DELAY)

  wire start = running & own & pf_valid & (skip == 11'd0);

  // ---- decode (from pf, in the start cycle) ----
  wire [4:0]  op      = pf[31:27];
  wire [2:0]  rd      = pf[26:24];
  wire [2:0]  ra      = pf[23:21];
  wire [3:0]  sub     = pf[20:17];
  wire [11:0] operand = pf[16:5];
  wire [4:0]  flags   = pf[4:0];

  wire illegal = ~(op == OP_NOP || op == OP_LI || op == OP_ALU || op == OP_BR ||
                   op == OP_ROLE_IF || op == OP_MARK || op == OP_RAW || op == OP_HALT);
  wire stop_op = (op == OP_HALT) | illegal;

  // ---- registers ----
  reg  [15:0] r [0:7];
  wire [15:0] a = r[ra];
  wire [15:0] b = flags[3] ? r[operand[2:0]] : {4'h0, operand};
  assign reg_rdata = r[reg_sel];

  reg  [15:0] alu;
  always @* begin
    case (sub)
      4'd0:    alu = a + b;
      4'd1:    alu = a - b;
      4'd2:    alu = a & b;
      4'd3:    alu = a | b;
      4'd4:    alu = a ^ b;
      4'd5:    alu = a << b[3:0];
      4'd6:    alu = a >> b[3:0];
      4'd7:    alu = {15'h0, a == b};
      4'd8:    alu = {15'h0, a != b};
      4'd9:    alu = {15'h0, a < b};
      default: alu = 16'h0;
    endcase
  end

  // ---- lines: config and the engine's own synchronizer ----
  reg  [7:0] sync_m, sync_s;   // 2 flops per pin, preloaded with the idle levels at RUN
  wire [2:0] l_pin  = line_cfg[{ra, 3'd0} +: 3];
  wire       l_od   = line_cfg[{ra, 3'd3}];
  wire       l_in   = sync_s[l_pin];   // the line, as the engine sees it now

  // Per pin, from the LINE table: the synchronizer preload (each line's idle
  // level), the drive at RUN (a non-gated PP line the role owns goes to idle,
  // isa §2), and which pins are OD (released at HALT).
  reg [7:0] preload, init_low, init_high, od_pins;
  integer li;
  always @* begin
    preload   = pin_in;
    init_low  = 8'h00;
    init_high = 8'h00;
    od_pins   = 8'h00;
    for (li = 0; li < 8; li = li + 1) begin
      preload[line_cfg[8*li +: 3]] = line_cfg[8*li + 6];
      if (line_cfg[8*li + 3])
        od_pins[line_cfg[8*li +: 3]] = 1'b1;
      else if (!line_cfg[8*li + 7] && role != 2'd2 && line_cfg[8*li + 4 +: 2] == role) begin
        if (line_cfg[8*li + 6]) init_high[line_cfg[8*li +: 3]] = 1'b1;
        else                    init_low[line_cfg[8*li +: 3]]  = 1'b1;
      end
    end
  end

  // ---- branches and the successor address ----
  wire       in_role   = operand[role] & (role != 2'd3);   // ROLE_IF mask
  wire       br_role   = ra[role] & (role != 2'd3);        // BR ROLE: mask in the ra field
  reg        to_flag;                                       // set by timed steps (later)
  reg        taken;
  always @* begin
    case (sub)
      BR_ALWAYS: taken = 1'b1;
      BR_Z:      taken = (a == 16'h0);
      BR_NZ:     taken = (a != 16'h0);
      BR_TO:     taken = to_flag;
      BR_ROLE:   taken = br_role;
      default:   taken = 1'b0;
    endcase
  end
  wire br_cond = (sub == BR_Z) | (sub == BR_NZ) | (sub == BR_TO) | (sub == BR_ROLE);

  wire [8:0] pc1 = fpc + 9'd1;
  reg  [8:0] next_pc;
  always @* begin
    next_pc = pc1;
    case (op)
      OP_BR:      if (taken) next_pc = operand[8:0];
      OP_ROLE_IF: if (!in_role) next_pc = pc1 + operand[11:3];
      OP_RAW:     if (sub == RAW_JPIN && l_in == operand[11]) next_pc = operand[8:0];
      default: ;
    endcase
  end

  assign fetch      = s0 | (start & ~stop_op);
  assign fetch_addr = s0 ? 9'd0 : next_pc;

  // ---- time: now, and tb as an age (now - tb) that saturates at 2^22 ----
  reg  [23:0] now_r;
  reg  [22:0] tb_age;
  wire        tick = running & ~(start & stop_op);   // now freezes at the halt cycle
  assign now_o = s0 ? 24'd0 : now_r;
  assign tb_o  = now_o - (s0 ? 24'd0 : {1'b0, tb_age});

  // DELAY: n rounded up to a multiple of U = 2; skip that many own cycles minus one
  wire [11:0] dly   = (operand == 12'd0) ? 12'd0 : operand - 12'd1;

  wire mark = start & (op == OP_MARK);
  reg  fv_valid;
  integer ri;
  wire ill  = start & illegal;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      started  <= 1'b0;
      halted   <= 1'b0;
      pf       <= 32'h0;   // NOP
      pf_valid <= 1'b0;
      fetch_d  <= 1'b0;
      fpc      <= 9'd0;
      xpc      <= 9'd0;
      skip     <= 11'd0;
      now_r    <= 24'd0;
      tb_age   <= 23'd0;
      to_flag  <= 1'b0;
      sync_m   <= 8'h00;
      sync_s   <= 8'h00;
      drv_low  <= 8'h00;
      drv_high <= 8'h00;
      viol     <= 16'h0;
      evt      <= 6'h0;
      irq      <= 1'b0;
      irq_id   <= 12'h0;
      fv_pc    <= 9'd0;
      fv_kind  <= 4'd0;
      fv_valid <= 1'b0;
      for (ri = 0; ri < 8; ri = ri + 1) r[ri] <= 16'h0;
    end else if (soft_reset) begin
      started  <= 1'b0;
      halted   <= 1'b0;
      pf       <= 32'h0;
      pf_valid <= 1'b0;
      fetch_d  <= 1'b0;
      fpc      <= 9'd0;
      xpc      <= 9'd0;
      skip     <= 11'd0;
      now_r    <= 24'd0;
      tb_age   <= 23'd0;
      to_flag  <= 1'b0;
      drv_low  <= 8'h00;
      drv_high <= 8'h00;
      viol     <= 16'h0;
      evt      <= 6'h0;
      irq      <= 1'b0;
      irq_id   <= 12'h0;
      fv_pc    <= 9'd0;
      fv_kind  <= 4'd0;
      fv_valid <= 1'b0;
      for (ri = 0; ri < 8; ri = ri + 1) r[ri] <= 16'h0;
    end else begin
      sync_m <= pin_in;
      sync_s <= sync_m;

      // fetch pipeline: the word lands in pf two cycles after its fetch
      fetch_d <= fetch;
      if (fetch) fpc <= fetch_addr;
      if (fetch_d) begin
        pf       <= sram_dout;
        pf_valid <= 1'b1;
      end

      if (!run) begin
        // stopped: release every pin; the state stays readable
        started  <= 1'b0;
        drv_low  <= 8'h00;
        drv_high <= 8'h00;
      end else if (s0) begin
        started  <= 1'b1;
        halted   <= 1'b0;
        pf_valid <= 1'b0;
        skip     <= 11'd0;
        now_r    <= 24'd1;
        tb_age   <= 23'd1;
        to_flag  <= 1'b0;
        // preload: the engine sees idle levels at now 0-1 and the real wire from
        // now 2 (as of now 0), as the model does
        sync_s   <= preload;
        drv_low  <= init_low;
        drv_high <= init_high;
      end else begin
        if (tick) begin
          now_r <= now_r + 24'd1;
          if (tb_age != AGE_MAX) tb_age <= tb_age + 23'd1;
        end
        if (running && own && skip != 11'd0) skip <= skip - 11'd1;

        if (start) begin
          pf_valid <= 1'b0;
          xpc      <= fpc;
          case (op)
            OP_LI:   r[rd] <= pf[20:5];
            OP_ALU:  if (sub <= 4'd9) r[rd] <= alu;
            OP_RAW:
              case (sub)
                RAW_SET: begin
                  drv_low[l_pin]  <= ~operand[0];
                  drv_high[l_pin] <= operand[0] & ~l_od;
                end
                RAW_REL: begin
                  drv_low[l_pin]  <= 1'b0;
                  drv_high[l_pin] <= 1'b0;
                end
                RAW_GET:   r[rd] <= {15'h0, l_in};
                RAW_DELAY: skip  <= dly[11:1];
                RAW_EVT: begin
                  r[rd] <= {10'h0, evt};
                  evt   <= evt & ~operand[5:0];
                end
                default: ;
              endcase
            OP_MARK:
              if (flags[4]) begin
                irq    <= 1'b1;
                irq_id <= operand;
              end
            default: ;
          endcase
          if (stop_op) begin
            // HALT and ILLEGAL: PP lines keep their level, OD lines are released (isa §4.1)
            halted   <= 1'b1;
            drv_low  <= drv_low & ~od_pins;
            drv_high <= drv_high & ~od_pins;
          end
        end
      end

      // host: registers while not running, W1C of VIOL and IRQ
      if (reg_we && !running) r[reg_sel] <= reg_wdata;
      if (irq_clr && !(mark && flags[4])) irq <= 1'b0;
      viol <= (viol & ~viol_clr) | (ill ? 16'h0400 : 16'h0);
      // FIRST_VIOL: the first fatal or timing kind since VIOL was last cleared to 0
      if (ill && !fv_valid) begin
        fv_pc    <= fpc;
        fv_kind  <= K_ILLEGAL;
        fv_valid <= 1'b1;
      end else if ((viol & ~viol_clr) == 16'h0) begin
        fv_valid <= 1'b0;
      end
    end
  end

  // ---- RTL trace port (architecture §5) ----
  wire        tr_issue  = start;
  wire        tr_step   = 1'b0;   // timed steps: later layer
  wire [8:0]  tr_pc     = start ? fpc : xpc;
  wire [1:0]  tr_op     = 2'd0;
  wire        tr_do     = 1'b0;
  wire        tr_mark   = mark;
  wire [15:0] tr_value  = mark ? {4'h0, operand} : 16'h0;
  wire [23:0] tr_now    = now_o;
  wire [23:0] tr_tb     = tb_o;
  wire [10:0] tr_evt    = {ill, 10'h0};
  wire        tr_br     = start & (op == OP_BR) & br_cond;
  wire        tr_taken  = taken;
  wire        tr_sample = 1'b0;
  wire [2:0]  tr_line   = 3'd0;
  wire        tr_halted = halted;

endmodule
