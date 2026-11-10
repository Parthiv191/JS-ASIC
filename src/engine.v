`default_nettype none

// One protocol engine (isa.md).
//
// Built: NOP, LI, ALU, BR, ROLE_IF, MARK, RAW, HALT, and the timed steps COND
// FALL/RISE (do, follow, MEAS), HOLD (do, follow) and XFER on unclocked lanes
// (do, follow, CHK, RESYNC, MSB; OD owners read their bits back), with ABORT
// into abort_pc. Not built yet, and halting as ILLEGAL until they are: COND
// START/STOP (bus detector), clocked lanes, PUSH/POP and src/dst FIFO, CRC, FZ,
// PROF, and profiles/lines/lanes past the built ones.
//
// Fetch and issue (isa §4.0): own cycles are those with ph == E. An
// instruction starts only on an own cycle, presents its successor's address in
// its start cycle, and the word lands in `pf` two cycles later. A successor
// starts on the first own cycle after its predecessor completes (and >= 2 after
// its start, which own-cycle parity gives for free).
//
// Time (isa §4.1): every time point is kept as a signed 24-bit age `now - x`
// (-2^23..2^23, enough for a future tb up to 16 x 0xFFFF away) that counts up each cycle and
// saturates at 2^22, so the 24-bit clamp is free and
// each timing check is one compare (the RTL note in §4.1):
//   tb_age   = now - tb               anc_age = now - window anchor
//   slot_age = now - (owner's next bit time)
//   samp_age = now - (next sample time)
//
// The tr_* signals are the RTL trace port (architecture §5): simulation only,
// nothing loads them, so synthesis removes them.
module engine #(
    parameter E = 0                   // engine index: own cycles are ph == E
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         ph,           // free-running phase bit
    input  wire         run,          // RUN, already aligned to a ph = 0 cycle
    input  wire         soft_reset,   // pulse: back to the reset state
    input  wire [1:0]   role,         // P0 0, P1 1, MON 2
    input  wire [63:0]  line_cfg,     // LINE[0..7]: pin[2:0] od[3] owner[5:4] idle[6] gated[7]
    input  wire [95:0]  lane_cfg,     // LANE[0..3], bytes b0-b2 (architecture §4)
    input  wire [335:0] prof_tab,     // PROF[1..7]: {max, nom, min} per profile
    input  wire [8:0]   abort_pc,
    input  wire [7:0]   pin_in,       // every pin as it is now (drives merged, before sync)

    // program SRAM (column 1)
    output wire         fetch,        // read fetch_addr this cycle
    output wire [8:0]   fetch_addr,
    input  wire [31:0]  sram_dout,    // A_DOUT: drives only pf

    // pin drive flops, one pair per pin (the line -> pin map is applied before them)
    output reg  [7:0]   drv_low,
    output reg  [7:0]   drv_high,

    // host
    input  wire [2:0]   reg_sel,
    input  wire         reg_we,       // write reg_wdata to r[reg_sel] (only while not running)
    input  wire [15:0]  reg_wdata,
    output wire [15:0]  reg_rdata,
    input  wire [15:0]  viol_clr,     // W1C
    input  wire         irq_clr,
    output wire         running,
    output reg          halted,
    output reg  [15:0]  viol,
    output reg  [5:0]   evt,
    output reg          irq,
    output reg  [11:0]  irq_id,
    output reg  [8:0]   xpc,          // pc of the instruction executing (or last executed)
    output reg  [8:0]   fv_pc,
    output reg  [3:0]   fv_kind,
    output wire [23:0]  now_o,
    output wire [23:0]  tb_o
);

  // ---- constants (isa §1, §3, §7) ----
  localparam [4:0] OP_NOP = 5'd0, OP_COND = 5'd1, OP_HOLD = 5'd2, OP_XFER = 5'd3, OP_LI = 5'd4,
                   OP_ALU = 5'd5, OP_BR = 5'd6, OP_ROLE_IF = 5'd7, OP_MARK = 5'd10,
                   OP_RAW = 5'd11, OP_HALT = 5'd12;
  localparam [3:0] RAW_SET = 4'd0, RAW_REL = 4'd1, RAW_GET = 4'd2, RAW_DELAY = 4'd3,
                   RAW_JPIN = 4'd4, RAW_EVT = 4'd5;
  localparam [3:0] BR_ALWAYS = 4'd0, BR_Z = 4'd1, BR_NZ = 4'd2, BR_TO = 4'd3, BR_ROLE = 4'd4;
  localparam [1:0] K_COND = 2'd1, K_HOLD = 2'd2, K_XFER = 2'd3;  // tr_op codes
  // event kinds
  localparam OVERRUN = 0, EARLY = 1, TIMEOUT = 2, LEVEL = 3, EDGE = 4, ABORT = 6, ILLEGAL = 10;
  localparam signed [23:0] AGE_MAX = 24'sh400000;  // 2^22: the 24-bit time clamp (isa §4.1)
  localparam signed [23:0] EDGE_LAT = 24'sd3;
  localparam signed [23:0] ISSUE = 24'sd2;
  localparam signed [23:0] QUANT = 24'sd1;

  function signed [23:0] sat;  // the 2^22 clamp
    input signed [23:0] x;
    sat = (x > AGE_MAX) ? AGE_MAX : x;
  endfunction
  function signed [23:0] u16;  // a 16-bit count as a signed age
    input [15:0] x;
    u16 = {8'h0, x};
  endfunction
  function [15:0] clamp16;     // MEAS: min(max(m, 0), 0xFFFF)
    input signed [23:0] x;
    clamp16 = (x < 0) ? 16'h0 : (x > 24'sh00FFFF) ? 16'hFFFF : x[15:0];
  endfunction

  // ---- run state ----
  reg  started;                     // RUN seen; cleared when RUN falls
  wire own = (ph == E[0]);
  wire s0  = run & ~started & own;  // first own cycle after RUN rises
  assign running = run & started & ~halted;

  // ---- fetch and the prefetch register ----
  reg  [31:0] pf;
  (* keep *) reg [3:0] pf_pi;      // a copy of pf[20:17] that only drives the profile mux
  reg         pf_valid, fetch_d, drop_d;
  reg  [8:0]  fpc;                  // address of the last fetch = pc of the word in pf
  reg  [10:0] skip;                 // own cycles still to wait (RAW DELAY)
  reg         busy;                 // a timed step is running (after its start cycle)
  reg         abort_pend;           // fetch abort_pc on the next own cycle

  wire start = running & own & pf_valid & (skip == 11'd0) & ~busy & ~abort_pend;

  // ---- decode (from pf, in the start cycle) ----
  wire [4:0]  op      = pf[31:27];
  wire [2:0]  rd      = pf[26:24];
  wire [2:0]  ra      = pf[23:21];
  wire [3:0]  sub     = pf[20:17];
  wire [11:0] operand = pf[16:5];
  wire [4:0]  flags   = pf[4:0];
  // T shape
  wire [1:0]  t_owner = pf[26:25];
  wire [3:0]  t_line  = pf[24:21];
  wire [3:0]  t_prof  = pf[20:17];
  wire        t_does  = (role != 2'd2) && (t_owner == role || t_owner == 2'd2);

  // the lane (XFER)
  reg  [23:0] lane;
  always @*
    case (t_line[1:0])
      2'd0: lane = lane_cfg[23:0];
      2'd1: lane = lane_cfg[47:24];
      2'd2: lane = lane_cfg[71:48];
      default: lane = lane_cfg[95:72];
    endcase
  wire        lane_ck = lane[6];   // has_clk
  wire [3:0]  x_n1    = operand[3:0];   // n - 1
  wire [3:0]  x_src   = operand[7:4];
  wire [3:0]  x_dst   = operand[11:8];

  wire timed   = (op == OP_COND) | (op == OP_HOLD) | (op == OP_XFER);
  // Reserved codes halt as ILLEGAL in the start cycle (isa §3): step owner 3, profile >= 8,
  // line >= 8, lane >= 4, src 4-7, dst 1 and 3-7, ALU sub 10-15, BR 5-15, RAW 6-15.
  // COND START/STOP, clocked lanes and src/dst FIFO/FZ are defined but not built yet.
  wire t_ok    = (t_prof[3] == 1'b0) && (t_owner != 2'd3) &&
                 (op == OP_COND ? (t_line[3] == 1'b0 && operand[1] == 1'b0) :
                  op == OP_HOLD ? (t_line[3] == 1'b0) :
                  (t_line[3:2] == 2'b00 && !lane_ck &&
                   (x_src == 4'd0 || x_src == 4'd1 || x_src[3]) &&
                   (x_dst == 4'd0 || x_dst[3])));
  wire untimed = (op == OP_NOP || op == OP_LI || (op == OP_ALU && sub <= 4'd9) ||
                  (op == OP_BR && sub <= BR_ROLE) || op == OP_ROLE_IF || op == OP_MARK ||
                  (op == OP_RAW && sub <= RAW_EVT) || op == OP_HALT);
  wire illegal = ~(untimed | (timed & t_ok));
  wire stop_op = (op == OP_HALT) | illegal;

  // ---- registers ----
  reg  [15:0] r [0:7];
  reg         wq_v;      // a register write in flight: r[wq_rd] <= wq_val next cycle
  reg  [2:0]  wq_rd;
  reg  [15:0] wq_val;
  // Every register write lands one cycle late through wq_*; the read ports bypass it, so an
  // instruction that starts the cycle after a step completes still sees the new value.
  wire [15:0] a = (wq_v && wq_rd == ra) ? wq_val : r[ra];
  // one read port serves the ALU's rb and XFER's src (their index fields differ)
  wire [2:0]  rb_i = (op == OP_XFER) ? operand[6:4] : operand[2:0];
  wire [15:0] rb   = (wq_v && wq_rd == rb_i) ? wq_val : r[rb_i];
  wire [15:0] b    = flags[3] ? rb : {4'h0, operand};
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
  reg  [7:0] sync_m, sync_s, sync_p;   // 2 sync flops per pin, and the previous cycle's value
  wire [2:0] l_pin  = line_cfg[{ra, 3'd0} +: 3];   // RAW: the line in the ra field
  wire       l_od   = line_cfg[{ra, 3'd3}];
  wire       l_in   = sync_s[l_pin];

  // Per pin, from the LINE table: the synchronizer preload (each line's idle
  // level), the drive at RUN (a non-gated PP line the role owns goes to idle,
  // isa §2), and which pins are OD (released at HALT and abort).
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
  wire       in_role = operand[role] & (role != 2'd3);   // ROLE_IF mask
  wire       br_role = ra[role] & (role != 2'd3);        // BR ROLE: mask in the ra field
  reg        to_flag;
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

  wire abort_fetch = abort_pend & own & running;
  assign fetch      = s0 | (start & ~stop_op) | abort_fetch;
  assign fetch_addr = s0 ? 9'd0 : abort_fetch ? abort_pc : next_pc;

  // ---- time ----
  reg  [23:0]        now_r;
  reg  signed [23:0] tb_age, anc_age;
  // window accumulated since the anchor (isa §4.1), stored as the edge-check thresholds on
  // anc_age: thr_lo = min + EDGE_LAT - QUANT, thr_hi = max + EDGE_LAT + QUANT (mod 2^24)
  reg  [23:0]        thr_lo, thr_hi;
  reg                acc_inf;
  wire               tick = running & ~(start & stop_op);   // time freezes at the halt cycle
  assign now_o = s0 ? 24'd0 : now_r;

  // ---- the timed step: fields latched at its start (s_*), or straight from pf in the start cycle ----
  reg  [1:0]  s_kind;
  reg         s_do, s_od, s_abort, s_chk, s_resync, s_msb, s_level, s_meas, s_profz;
  reg  [2:0]  s_pin, s_rd, s_line;
  reg  [3:0]  s_n1, s_dst;
  reg  [15:0] s_min, s_nom, s_max, s_v, s_rx;
  reg  [3:0]  s_k, s_j;                // bits driven, bits sampled
  reg  signed [23:0] slot_age, samp_age;

  reg  [47:0] p7;                      // this engine's private profile 7, copied at RUN
  reg  [47:0] prof;
  always @* begin
    case (pf_pi[2:0])
      3'd0:    prof = {16'hFFFF, 16'h0000, 16'h0000};   // IDLE = (0, 0, INF)
      3'd7:    prof = p7;
      default: prof = prof_tab[(t_prof[2:0] - 3'd1) * 48 +: 48];
    endcase
  end

  wire        st     = start & timed & t_ok;  // a timed step starts this cycle
  wire [2:0]  st_line = (op == OP_XFER) ? lane[2:0] : t_line[2:0];
  wire [15:0] st_v   = (x_src == 4'd0) ? 16'h0 : (x_src == 4'd1) ? 16'hFFFF : rb;

  // In the start cycle the fields come from pf; after it, from the latched s_*. Selecting on
  // busy (a flop) instead of st keeps the instruction decode off every datapath.
  wire        sel     = ~busy;
  wire        c_on    = st | busy;
  wire [1:0]  c_kind  = sel ? (op == OP_COND ? K_COND : op == OP_HOLD ? K_HOLD : K_XFER) : s_kind;
  wire        c_do    = sel ? t_does : s_do;
  wire [2:0]  c_line  = sel ? st_line : s_line;
  wire [2:0]  c_pin   = sel ? line_cfg[{st_line, 3'd0} +: 3] : s_pin;
  wire        c_od    = sel ? line_cfg[{st_line, 3'd3}] : s_od;
  wire [15:0] c_min   = sel ? prof[15:0]  : s_min;
  wire [15:0] c_nom   = sel ? prof[31:16] : s_nom;
  wire [15:0] c_max   = sel ? prof[47:32] : s_max;
  wire        c_abort = sel ? flags[2] : s_abort;
  wire        c_chk   = sel ? flags[0] : s_chk;
  wire        c_rsync = sel ? flags[1] : s_resync;
  wire        c_msb   = sel ? flags[3] : s_msb;
  wire        c_level = sel ? operand[0] : s_level;   // COND: RISE 1 / FALL 0; HOLD: level
  wire        c_meas  = sel ? (op == OP_COND && operand[2]) : s_meas;
  wire [2:0]  c_rd    = sel ? operand[5:3] : s_rd;
  wire        c_profz = sel ? (t_prof == 4'd0) : s_profz;
  wire [3:0]  c_n1    = sel ? (op == OP_XFER ? x_n1 : 4'd0) : s_n1;
  wire [3:0]  c_dst   = sel ? x_dst : s_dst;
  wire [15:0] c_v     = sel ? st_v : s_v;
  wire [15:0] c_rx    = sel ? 16'h0 : s_rx;
  wire [3:0]  c_k     = sel ? 4'd0 : s_k;
  wire [3:0]  c_j     = sel ? 4'd0 : s_j;
  wire        c_first = sel;

  wire signed [23:0] nom   = u16(c_nom);
  wire signed [23:0] half  = u16({1'b0, c_nom[15:1]});
  wire               bounded = (c_max != 16'hFFFF);
  wire signed [23:0] h3     = half + EDGE_LAT;
  wire signed [23:0] tb_p1  = tb_age + 24'sd1;   // from flops: early
  wire signed [23:0] anc_p1 = anc_age + 24'sd1;
  wire signed [23:0] c_slot = sel ? tb_age : slot_age;
  wire signed [23:0] c_samp = sel ? tb_age - h3 : samp_age;
  wire signed [23:0] c_slot_p1 = sel ? tb_p1 : slot_age + 24'sd1;
  wire signed [23:0] c_samp_p1 = sel ? tb_p1 - h3 : samp_age + 24'sd1;

  wire       inp  = sync_s[c_pin];
  wire       edg  = sync_s[c_pin] ^ sync_p[c_pin];

  // value bit positions: bit k of the wire carries value bit k (LSB first) or n-1-k (MSB)
  wire [3:0] pos_k = c_msb ? c_n1 - c_k : c_k;
  wire [3:0] pos_j = c_msb ? c_n1 - c_j : c_j;

  // Start-cycle results that nothing reads before the next own cycle (>= start + 2) land one
  // cycle late, off the decode -> profile -> arithmetic path: the window add (acc_pend), the
  // register write (wq_*) and the event registers (ev_q). The window check reads acc
  // with the pending add folded in.
  reg                acc_pend, acc_clrq, evq_st;
  reg  [10:0]        ev_q;
  reg  [8:0]         evpc_q;
  // the window, with last cycle's clear and add applied (thr_*, acc_inf lag one cycle behind)
  localparam [23:0]  THR_LO0 = EDGE_LAT - QUANT, THR_HI0 = EDGE_LAT + QUANT;  // the empty window
  wire [23:0]        thr_lo_e  = (acc_clrq ? THR_LO0 : thr_lo) + (acc_pend ? {8'h0, s_min} : 24'h0);
  wire [23:0]        thr_hi_e  = (acc_clrq ? THR_HI0 : thr_hi) + (acc_pend ? {8'h0, s_max} : 24'h0);
  // The checks compare anc_age with thr_* as 24-bit signed values. The old form, m = anc_age - E
  // against the 24-bit signed min - Q and max + Q, wraps at thr = 2^23 + 3, not 2^23: for
  // thr in [2^23, 2^23 + 2] the bound is above any age (<= 2^22), so lo fails and hi passes.
  wire               thr_lo_mid = thr_lo_e[23] & ~|thr_lo_e[22:2] & ~&thr_lo_e[1:0];
  wire               thr_hi_mid = thr_hi_e[23] & ~|thr_hi_e[22:2] & ~&thr_hi_e[1:0];
  wire               acc_inf_e = (~acc_clrq & acc_inf) | (acc_pend & (s_max == 16'hFFFF));

  // ---- one cycle of the step: what it does this cycle (combinational) ----
  reg                done, abort_now, to_set;
  reg  [10:0]        ev;            // event kinds logged this cycle
  reg                drv_en, drv_v;
  reg                wr_en;
  reg  [2:0]         wr_rd;
  reg  [15:0]        wr_val;
  // next-cycle ages: *_nx already includes this cycle's + 1; tb/anc update only when *_upd
  reg  signed [23:0] tb_nx, anc_nx, slot_nx, samp_nx, s_cur, s_cur_p1;
  reg                tb_upd, anc_upd;
  reg                acc_clr, acc_add;
  reg                k_inc, j_inc, smp;
  reg  [15:0]        rx_new;
  reg  [15:0]        value;         // the step's record value
  reg                sample_tr;     // a follower sample (trace)

  reg signed [23:0] m;
  reg               in_win;
  reg [15:0]        rxs;

  always @* begin
    done = 1'b0; abort_now = 1'b0; to_set = 1'b0; ev = 11'h0;
    drv_en = 1'b0; drv_v = 1'b0;
    wr_en = 1'b0; wr_rd = 3'd0; wr_val = 16'h0;
    tb_upd = 1'b0; anc_upd = 1'b0; tb_nx = tb_p1; anc_nx = anc_p1;
    slot_nx = c_slot_p1; samp_nx = c_samp_p1; s_cur = c_samp; s_cur_p1 = c_samp_p1;
    acc_clr = 1'b0; acc_add = 1'b0;
    k_inc = 1'b0; j_inc = 1'b0; smp = 1'b0;
    rx_new = c_rx; value = 16'h0; sample_tr = 1'b0;
    m = 24'sd0; in_win = 1'b0; rxs = c_rx;

    if (c_on) begin
      case (c_kind)
        // COND FALL/RISE (isa §4.2)
        K_COND:
          if (c_do) begin
            if (tb_age >= nom) begin            // at max(now, tb + nom)
              if (bounded && tb_age - nom > ((c_nom == 16'h0) ? ISSUE : 24'sd0)) ev[OVERRUN] = 1'b1;
              drv_en = 1'b1; drv_v = c_level;
              value  = c_meas ? clamp16(tb_age) : 16'h0;
              if (c_meas) begin wr_en = 1'b1; wr_rd = c_rd; wr_val = clamp16(tb_age); end
              tb_upd = 1'b1; tb_nx = 24'sd1; anc_upd = 1'b1; anc_nx = 24'sd1; acc_clr = 1'b1;
              done = 1'b1;
            end
          end else begin
            if (edg && inp == c_level) begin   // the edge wins over a timeout
              m = tb_age - EDGE_LAT;
              if (c_min != 16'h0 && m < u16(c_min) - QUANT) ev[EARLY] = 1'b1;
              value = c_meas ? clamp16(m) : 16'h0;
              if (c_meas) begin wr_en = 1'b1; wr_rd = c_rd; wr_val = clamp16(m); end
              tb_upd = 1'b1; tb_nx = EDGE_LAT + 24'sd1; anc_upd = 1'b1; anc_nx = EDGE_LAT + 24'sd1;
              acc_clr = 1'b1;
              done = 1'b1;
            end else if (bounded && tb_age - EDGE_LAT > u16(c_max) + QUANT) begin
              ev[TIMEOUT] = 1'b1;
              if (c_abort) abort_now = 1'b1;
              else begin
                to_set = 1'b1;
                tb_upd = 1'b1; tb_nx = tb_p1 - nom; anc_upd = 1'b1; anc_nx = tb_p1 - nom;
                acc_clr = 1'b1;
                value  = c_meas ? 16'hFFFF : 16'h0;
                if (c_meas) begin wr_en = 1'b1; wr_rd = c_rd; wr_val = 16'hFFFF; end
              end
              done = 1'b1;
            end
          end

        // HOLD (isa §4.2)
        K_HOLD:
          if (c_do) begin
            if (tb_age >= 24'sd0) begin         // at max(now, tb), in_win the issue slot
              if (!c_profz && tb_age > ISSUE) ev[OVERRUN] = 1'b1;
              drv_en = 1'b1; drv_v = c_level;
              value  = {15'h0, c_level};
              tb_upd = 1'b1; tb_nx = tb_p1 - nom; acc_add = 1'b1;
              done = 1'b1;
            end
          end else begin
            if (c_first && tb_age > half + EDGE_LAT) ev[OVERRUN] = 1'b1;
            if (tb_age >= half + EDGE_LAT) begin  // the midpoint of the owner's slot
              sample_tr = 1'b1;
              value = {15'h0, inp};
              if (inp != c_level) begin
                ev[LEVEL] = 1'b1;
                if (c_abort) abort_now = 1'b1;
              end
              if (!abort_now) begin tb_upd = 1'b1; tb_nx = tb_p1 - nom; acc_add = 1'b1; end
              done = 1'b1;
            end
          end

        // XFER, unclocked lane (isa §4.3)
        K_XFER: begin
          if (c_do) begin
            // owner: bit k at tb + k*P; the issue slot covers bit 0
            if (c_k <= c_n1 && c_slot >= 24'sd0) begin
              if (c_slot > ((c_k == 4'd0) ? ISSUE : 24'sd0)) ev[OVERRUN] = 1'b1;
              drv_en = 1'b1; drv_v = c_v[pos_k];
              k_inc = 1'b1; slot_nx = c_slot_p1 - nom; acc_add = 1'b1;
              if (!c_od && c_k == c_n1) begin   // a PP owner completes on its last bit
                tb_upd = 1'b1; tb_nx = c_slot_p1 - nom;
                value  = c_v & ~(16'hFFFF << {1'b0, c_n1} << 1);
                done   = 1'b1;
              end
            end
            // an OD owner reads its bits back where a follower samples them (isa §6)
            if (c_od && c_j <= c_n1 && c_samp >= 24'sd0) begin
              rxs = c_rx; rxs[pos_j] = inp; rx_new = rxs;
              j_inc = 1'b1; samp_nx = c_samp_p1 - nom;
              if (c_j == c_n1) begin
                tb_upd = 1'b1; tb_nx = slot_nx;     // base + n*P, after this cycle's drive
                value  = rxs;
                done   = 1'b1;
              end
            end
          end else begin
            // follower: edges against the window, samples at a + (k - ak)P + P/2 + E
            if (edg) begin
              // anc_age - E in [min - Q, max + Q]
              in_win = (anc_age >= $signed(thr_lo_e)) && !thr_lo_mid
                    && (acc_inf_e || thr_hi_mid || anc_age <= $signed(thr_hi_e));
              if (c_chk && !in_win) begin
                ev[EDGE] = 1'b1;
                if (c_abort) abort_now = 1'b1;
              end
              if (c_rsync && in_win) begin              // (a, ak) := (e, k)
                s_cur = -half; s_cur_p1 = 24'sd1 - half;
              end
            end
            samp_nx = s_cur_p1;
            if (!abort_now && s_cur >= 24'sd0) begin
              if (s_cur > 24'sd0) ev[OVERRUN] = 1'b1;
              sample_tr = 1'b1;
              rxs = c_rx; rxs[pos_j] = inp; rx_new = rxs;
              j_inc = 1'b1; acc_add = 1'b1;
              if (c_j == c_n1) begin
                tb_upd = 1'b1; tb_nx = s_cur_p1 - nom + h3;   // a + (n - ak)P
                value  = rxs;
                done   = 1'b1;
              end
              samp_nx = s_cur_p1 - nom;
            end
            if (abort_now) done = 1'b1;
          end
          if (done && !abort_now && c_dst[3]) begin
            wr_en = 1'b1; wr_rd = c_dst[2:0]; wr_val = value;
          end
        end
        default: ;
      endcase

      if (abort_now) begin
        ev[ABORT] = 1'b1;
        done = 1'b1;
        tb_upd = 1'b1; tb_nx = 24'sd1; anc_upd = 1'b1; anc_nx = 24'sd1; acc_clr = 1'b1;
        wr_en = 1'b0;
      end
    end
  end

  // DELAY: n rounded up to a multiple of U = 2; skip that many own cycles minus one
  wire [11:0] dly  = (operand == 12'd0) ? 12'd0 : operand - 12'd1;
  wire        mark = start & (op == OP_MARK);
  // untimed register writes (LI, ALU, RAW GET/EVT) also land one cycle after the start
  wire        u_wr = start & ((op == OP_LI) | (op == OP_ALU && sub <= 4'd9) |
                              (op == OP_RAW && (sub == RAW_GET || sub == RAW_EVT)));
  wire [15:0] u_val = (op == OP_LI) ? pf[20:5] : (op == OP_ALU) ? alu :
                      (sub == RAW_GET) ? {15'h0, l_in} : {10'h0, evt};
  wire        ill  = start & illegal;
  wire [10:0] evs  = ev | {ill, 10'h0};

  // FIRST_VIOL: the lowest fatal or timing kind (0-6, 10) logged this cycle
  reg [3:0] fv_new;
  reg       fv_any;
  integer   fi;
  always @* begin
    fv_any = |ev_q[6:0] | ev_q[10];
    fv_new = 4'd10;
    for (fi = 6; fi >= 0; fi = fi - 1)
      if (ev_q[fi]) fv_new = fi[3:0];
  end

  reg     fv_valid;
  integer ri;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      started  <= 1'b0;
      halted   <= 1'b0;
      pf       <= 32'h0;   // NOP
      pf_pi    <= 4'h0;
      pf_valid <= 1'b0;
      fetch_d  <= 1'b0;
      drop_d   <= 1'b0;
      fpc      <= 9'd0;
      xpc      <= 9'd0;
      skip     <= 11'd0;
      busy     <= 1'b0;
      abort_pend <= 1'b0;
      now_r    <= 24'd0;
      tb_age   <= 24'sd0;
      anc_age  <= 24'sd0;
      thr_lo   <= THR_LO0;
      thr_hi   <= THR_HI0;
      acc_inf  <= 1'b0;
      to_flag  <= 1'b0;
      sync_m   <= 8'h00;
      sync_s   <= 8'h00;
      sync_p   <= 8'h00;
      drv_low  <= 8'h00;
      drv_high <= 8'h00;
      viol     <= 16'h0;
      evt      <= 6'h0;
      irq      <= 1'b0;
      irq_id   <= 12'h0;
      fv_pc    <= 9'd0;
      fv_kind  <= 4'd0;
      fv_valid <= 1'b0;
      p7       <= {16'hFFFF, 32'h0};
      acc_pend <= 1'b0; acc_clrq <= 1'b0; wq_v <= 1'b0; wq_rd <= 3'd0; wq_val <= 16'h0;
      ev_q <= 11'h0; evpc_q <= 9'd0; evq_st <= 1'b0;
      s_kind <= 2'd0; s_do <= 1'b0; s_od <= 1'b0; s_abort <= 1'b0; s_chk <= 1'b0;
      s_resync <= 1'b0; s_msb <= 1'b0; s_level <= 1'b0; s_meas <= 1'b0; s_profz <= 1'b0;
      s_pin <= 3'd0; s_rd <= 3'd0; s_line <= 3'd0; s_n1 <= 4'd0; s_dst <= 4'd0;
      s_min <= 16'h0; s_nom <= 16'h0; s_max <= 16'h0; s_v <= 16'h0; s_rx <= 16'h0;
      s_k <= 4'd0; s_j <= 4'd0; slot_age <= 24'sd0; samp_age <= 24'sd0;
      for (ri = 0; ri < 8; ri = ri + 1) r[ri] <= 16'h0;
    end else if (soft_reset) begin
      started  <= 1'b0;
      halted   <= 1'b0;
      pf       <= 32'h0;
      pf_pi    <= 4'h0;
      pf_valid <= 1'b0;
      fetch_d  <= 1'b0;
      drop_d   <= 1'b0;
      fpc      <= 9'd0;
      xpc      <= 9'd0;
      skip     <= 11'd0;
      busy     <= 1'b0;
      abort_pend <= 1'b0;
      now_r    <= 24'd0;
      tb_age   <= 24'sd0;
      anc_age  <= 24'sd0;
      thr_lo   <= THR_LO0;
      thr_hi   <= THR_HI0;
      acc_inf  <= 1'b0;
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
      acc_pend <= 1'b0; acc_clrq <= 1'b0; wq_v <= 1'b0; ev_q <= 11'h0; evq_st <= 1'b0;
      for (ri = 0; ri < 8; ri = ri + 1) r[ri] <= 16'h0;
    end else begin
      sync_m <= pin_in;
      sync_s <= sync_m;
      sync_p <= sync_s;

      // fetch pipeline: the word lands in pf two cycles after its fetch. An
      // abort drops a word still in flight (its fetch was the old successor).
      fetch_d <= fetch;
      drop_d  <= fetch & abort_now;
      if (fetch) fpc <= fetch_addr;
      // pf loads every arriving word; only pf_valid depends on an abort dropping it
      if (fetch_d) begin
        pf       <= sram_dout;
        pf_pi    <= sram_dout[20:17];
      end
      if (fetch_d && !drop_d && !abort_now) pf_valid <= 1'b1;

      if (!run) begin
        // stopped: release every pin; the state stays readable
        started  <= 1'b0;
        busy     <= 1'b0;
        abort_pend <= 1'b0;
        drv_low  <= 8'h00;
        drv_high <= 8'h00;
      end else if (s0) begin
        started  <= 1'b1;
        halted   <= 1'b0;
        pf_valid <= 1'b0;
        skip     <= 11'd0;
        busy     <= 1'b0;
        abort_pend <= 1'b0;
        now_r    <= 24'd1;
        tb_age   <= 24'sd1;     // tb = 0
        anc_age  <= 24'sd1;     // anchor = 0
        thr_lo   <= THR_LO0;
        thr_hi   <= THR_HI0;
        acc_inf  <= 1'b0;
        acc_pend <= 1'b0;
        acc_clrq <= 1'b0;
        to_flag  <= 1'b0;
        p7       <= prof_tab[288 +: 48];
        // preload: the engine sees idle levels at now 0-1 and the real wire from
        // now 2 (as of now 0), as the model does
        sync_s   <= preload;
        drv_low  <= init_low;
        drv_high <= init_high;
      end else begin
        if (tick) now_r <= now_r + 24'd1;
        // ages: this cycle's value (after the step's updates), plus one
        // a step's new value is never above 2^22, so only the plain increment saturates
        tb_age   <= tb_upd  ? tb_nx  : !tick ? tb_age  : (tb_age  >= AGE_MAX) ? AGE_MAX : tb_p1;
        anc_age  <= anc_upd ? anc_nx : !tick ? anc_age : (anc_age >= AGE_MAX) ? AGE_MAX : anc_p1;
        slot_age <= slot_nx;
        samp_age <= samp_nx;
        // every window update lands one cycle late; readers use thr_*_e, acc_inf_e
        acc_pend <= acc_add & ~acc_clr;
        acc_clrq <= acc_clr;
        thr_lo   <= thr_lo_e;
        thr_hi   <= thr_hi_e;
        acc_inf  <= acc_inf_e;

        if (running && own && skip != 11'd0) skip <= skip - 11'd1;

        // the step's own state
        if (st) begin
          s_kind <= c_kind; s_do <= c_do; s_od <= c_od; s_abort <= c_abort; s_chk <= c_chk;
          s_resync <= c_rsync; s_msb <= c_msb; s_level <= c_level; s_meas <= c_meas;
          s_profz <= c_profz; s_pin <= c_pin; s_line <= c_line; s_rd <= c_rd; s_n1 <= c_n1; s_dst <= c_dst;
          s_min <= c_min; s_nom <= c_nom; s_max <= c_max; s_v <= c_v;
          to_flag <= 1'b0;
        end
        if (c_on) begin
          s_k  <= c_k + {3'd0, k_inc};
          s_j  <= c_j + {3'd0, j_inc};
          s_rx <= rx_new;
          busy <= ~done;
        end
        if (to_set) to_flag <= 1'b1;
        if (drv_en) begin
          drv_low[c_pin]  <= ~drv_v;
          drv_high[c_pin] <= drv_v & ~c_od;
        end
        // register writes (untimed and step results) go through wq_* and land next cycle
        wq_v <= wr_en | u_wr;
        if (u_wr) begin wq_rd <= rd; wq_val <= u_val; end
        else if (wr_en) begin wq_rd <= wr_rd; wq_val <= wr_val; end
        if (wq_v) r[wq_rd] <= wq_val;
        if ((busy && ev[TIMEOUT]) || (evq_st && ev_q[TIMEOUT])) evt[2] <= 1'b1;
        if ((busy && (ev[LEVEL] || ev[EDGE])) || (evq_st && (ev_q[LEVEL] || ev_q[EDGE]))) evt[3] <= 1'b1;

        if (abort_now) begin
          // release OD lines, refetch from abort_pc (isa §4.1, §4.0)
          abort_pend <= 1'b1;
          pf_valid   <= 1'b0;
          drv_low    <= drv_low & ~od_pins;
          drv_high   <= drv_high & ~od_pins;
        end
        if (abort_fetch) abort_pend <= 1'b0;

        if (start) begin
          pf_valid <= 1'b0;
          xpc      <= fpc;
          case (op)
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
                RAW_DELAY: skip  <= dly[11:1];
                RAW_EVT: begin
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
      ev_q   <= evs;
      evq_st <= ~busy;
      evpc_q <= start ? fpc : xpc;
      viol <= (viol & ~viol_clr) | {5'h0, ev_q};
      if (fv_any && !fv_valid) begin
        fv_pc    <= evpc_q;
        fv_kind  <= fv_new;
        fv_valid <= 1'b1;
      end else if ((viol & ~viol_clr) == 16'h0) begin
        fv_valid <= 1'b0;
      end
    end
  end

  assign tb_o = now_o - (s0 ? 24'd0 : tb_age);

  // ---- RTL trace port (architecture §5) ----
  wire        tr_issue  = start;
  wire        tr_step   = c_on & done & ~abort_now;
  wire [8:0]  tr_pc     = start ? fpc : xpc;
  wire [1:0]  tr_op     = c_kind;
  wire        tr_do     = c_do;
  wire        tr_mark   = mark;
  wire [15:0] tr_value  = mark ? {4'h0, operand} : value;
  wire [23:0] tr_now    = now_o;
  wire signed [23:0] tb_tr = tb_upd ? tb_nx - 24'sd1 : tb_age;   // tb after this cycle
  wire [23:0] tr_tb     = now_o - tb_tr[23:0];
  wire [10:0] tr_evt    = evs;
  // branch outcomes: conditional BR, RAW JPIN, and ROLE_IF (taken = skipped)
  wire        jpin      = (op == OP_RAW) && (sub == RAW_JPIN);
  wire        tr_br     = start & ~illegal & (((op == OP_BR) & br_cond) | jpin | (op == OP_ROLE_IF));
  wire        tr_taken  = (op == OP_ROLE_IF) ? ~in_role : jpin ? (l_in == operand[11]) : taken;
  wire        tr_sample = c_on & sample_tr;
  wire [2:0]  tr_line   = c_line;
  wire        tr_halted = halted;

endmodule
