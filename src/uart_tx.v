`default_nettype none

// Fixed-function UART transmitter: 8 data bits, no parity, 1 stop bit (8N1).
// Pulse `start` for one clock while `busy` is low and `data` gets sent on `tx`.
module uart_tx #(
    parameter CLKS_PER_BIT = 434  // 50 MHz / 115200 baud
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output wire       tx,
    output wire       busy
);

  localparam CNT_W = $clog2(CLKS_PER_BIT);

  // The whole frame lives in one shift register: {stop, data[7:0], start}.
  // We always send bit 0 and shift right, filling with 1s, so when the frame
  // is done the line is left high (idle) for free.
  reg [9:0]       shreg;
  reg [3:0]       bits_left;  // bits of the frame still to send
  reg [CNT_W-1:0] baud_cnt;   // clocks spent on the current bit

  assign tx   = shreg[0];
  assign busy = (bits_left != 0);

  always @(posedge clk) begin
    if (!rst_n) begin
      shreg     <= 10'h3FF;  // line idles high
      bits_left <= 0;
      baud_cnt  <= 0;
    end else if (!busy) begin
      if (start) begin
        shreg     <= {1'b1, data, 1'b0};
        bits_left <= 10;
        baud_cnt  <= 0;
      end
    end else if (baud_cnt == CLKS_PER_BIT - 1) begin
      // held this bit long enough, move to the next one
      shreg     <= {1'b1, shreg[9:1]};
      bits_left <= bits_left - 1;
      baud_cnt  <= 0;
    end else begin
      baud_cnt <= baud_cnt + 1;
    end
  end

endmodule
