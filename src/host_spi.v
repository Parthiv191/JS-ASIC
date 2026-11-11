`default_nettype none

// Host SPI target (architecture §4): mode 0, MSB first, SCK <= clk/8.
// The inputs are already through the 2-flop synchronizers.
//
// Frame: CMD [7] read, [6] pair · ADDR[15:8] · ADDR[7:0] · data...
//   write: each data byte is a `wr` pulse at `addr`; then addr advances.
//   read:  1 dummy byte, then the byte at addr, addr+1, ... Each `rd` pulse
//          says addr names the next byte to send; the bus must present it on
//          `rdata` within 32 clk (it gets at least 64). Fetching ahead has no
//          side effects; the bus must keep it that way.
//   pair:  the address toggles bit 0 instead of incrementing.
// A CS rise ends the frame (`frame_end`) and resets the bit and byte counters.
module host_spi (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        sck,
    input  wire        mosi,
    input  wire        cs,         // active high (CS_N inverted)
    input  wire [7:0]  rdata,      // byte at `addr`, for reads
    output wire        miso,       // straight from a flop
    output reg  [15:0] addr,
    output reg         wr,         // pulse: write `wdata` to `addr`
    output reg  [7:0]  wdata,
    output reg         rd,         // pulse: `addr` changed in a read frame
    output reg         frame_end   // pulse: CS rose
);

  localparam CMD = 2'd0, ADDR_H = 2'd1, ADDR_L = 2'd2, DATA = 2'd3;

  reg        sck_d, cs_d;
  reg [2:0]  bitcnt;
  reg [6:0]  sr;        // bits received so far in this byte
  reg [7:0]  tx;        // tx[7] is on MISO
  reg [1:0]  phase;
  reg        is_read, pair;

  wire       rise    = cs & sck & ~sck_d;
  wire       done    = rise & (bitcnt == 3'd7);
  wire [7:0] byte_in = {sr, mosi};
  wire [15:0] next_addr = pair ? {addr[15:1], ~addr[0]} : addr + 16'd1;

  assign miso = tx[7];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sck_d     <= 1'b0;
      cs_d      <= 1'b0;
      bitcnt    <= 3'd0;
      sr        <= 7'd0;
      tx        <= 8'd0;
      phase     <= CMD;
      is_read   <= 1'b0;
      pair      <= 1'b0;
      addr      <= 16'd0;
      wr        <= 1'b0;
      wdata     <= 8'd0;
      rd        <= 1'b0;
      frame_end <= 1'b0;
    end else begin
      sck_d     <= sck;
      cs_d      <= cs;
      wr        <= 1'b0;
      rd        <= 1'b0;
      frame_end <= cs_d & ~cs;

      // A write lands on the cycle after its last bit; advance after it.
      if (wr) addr <= next_addr;

      if (!cs) begin
        bitcnt <= 3'd0;
        phase  <= CMD;
        tx     <= 8'd0;
      end else if (rise) begin
        bitcnt <= bitcnt + 3'd1;
        sr     <= byte_in[6:0];
        tx     <= {tx[6:0], 1'b0};
        if (done) begin
          case (phase)
            CMD: begin
              is_read <= byte_in[7];
              pair    <= byte_in[6];
              phase   <= ADDR_H;
            end
            ADDR_H: begin
              addr[15:8] <= byte_in;
              phase      <= ADDR_L;
            end
            ADDR_L: begin
              addr[7:0] <= byte_in;
              phase     <= DATA;
              tx        <= 8'd0;     // the dummy byte
              rd        <= is_read;
            end
            DATA: begin
              if (is_read) begin
                tx   <= rdata;
                addr <= next_addr;
                rd   <= 1'b1;
              end else begin
                wdata <= byte_in;
                wr    <= 1'b1;
              end
            end
          endcase
        end
      end
    end
  end

endmodule
