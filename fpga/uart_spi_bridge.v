`default_nettype none

// UART-to-SPI bridge for FPGA bring-up: the PC drives the chip's host SPI port (architecture §4)
// over the board's USB-UART, so no extra hardware is needed.
//
// Protocol (8N1, CLKS_PER_BIT clk per bit): the PC sends 0xA5, a 16-bit length LSB first, then
// that many bytes. The bridge lowers CS, shifts each byte out on MOSI (mode 0, MSB first) and
// sends back the byte it read on MISO, then raises CS. Bytes before a 0xA5 header are ignored,
// and a length of 0 does nothing. SPI timing copies test/chip.py's Host.frame: CS low, then per
// bit MOSI set, HALF clk, MISO sampled, SCK high for HALF clk; HALF clk after the last bit CS
// rises, and stays high for at least GAP clk. HALF = 5 gives SCK = clk/10 (the port needs
// SCK <= clk/8).
module uart_spi_bridge #(
    parameter CLKS_PER_BIT = 40,  // 1 Mbaud at 40 MHz
    parameter HALF = 5,
    parameter GAP = 6
) (
    input  wire clk,
    input  wire rst_n,
    input  wire rx,
    output wire tx,
    output reg  sck,
    output reg  mosi,
    output reg  cs_n,
    input  wire miso
);

  // ---- UART receive: 2-flop synchronizer, start bit checked at mid-bit ----
  reg [1:0] rx_s = 2'b11;
  always @(posedge clk) rx_s <= {rx_s[0], rx};
  wire rxd = rx_s[1];

  reg [15:0] rx_cnt;
  reg [3:0]  rx_bit;
  reg [7:0]  rx_sh;
  reg        rx_busy, rx_valid;
  reg [7:0]  rx_byte;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rx_busy <= 0; rx_valid <= 0; rx_cnt <= 0; rx_bit <= 0; rx_sh <= 0; rx_byte <= 0;
    end else begin
      rx_valid <= 0;
      if (!rx_busy) begin
        if (!rxd) begin
          rx_busy <= 1; rx_cnt <= CLKS_PER_BIT / 2; rx_bit <= 0;
        end
      end else if (rx_cnt != 0) begin
        rx_cnt <= rx_cnt - 1;
      end else begin
        rx_cnt <= CLKS_PER_BIT - 1;
        if (rx_bit == 0) begin
          if (rxd) rx_busy <= 0;            // a glitch, not a start bit
          else rx_bit <= 1;
        end else if (rx_bit <= 8) begin
          rx_sh <= {rxd, rx_sh[7:1]};       // LSB first
          rx_bit <= rx_bit + 1;
        end else begin                       // stop bit
          rx_busy <= 0;
          if (rxd) begin rx_valid <= 1; rx_byte <= rx_sh; end
        end
      end
    end
  end

  // ---- UART transmit ----
  reg [15:0] tx_cnt;
  reg [3:0]  tx_bit;
  reg [9:0]  tx_sh;
  reg        tx_busy;
  reg        tx_start;
  reg [7:0]  tx_byte;
  assign tx = tx_busy ? tx_sh[0] : 1'b1;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tx_busy <= 0; tx_cnt <= 0; tx_bit <= 0; tx_sh <= 10'h3FF;
    end else if (!tx_busy) begin
      if (tx_start) begin
        tx_busy <= 1; tx_sh <= {1'b1, tx_byte, 1'b0}; tx_cnt <= CLKS_PER_BIT - 1; tx_bit <= 0;
      end
    end else if (tx_cnt != 0) begin
      tx_cnt <= tx_cnt - 1;
    end else begin
      tx_cnt <= CLKS_PER_BIT - 1;
      tx_sh <= {1'b1, tx_sh[9:1]};
      if (tx_bit == 9) tx_busy <= 0;
      else tx_bit <= tx_bit + 1;
    end
  end

  // ---- frame FSM and the SPI shifter ----
  localparam S_IDLE = 0, S_LEN0 = 1, S_LEN1 = 2, S_CSLOW = 3, S_WAITB = 4, S_SET = 5,
             S_LO = 6, S_HI = 7, S_BYTE = 8, S_END = 9, S_GAP = 10;
  reg [3:0]  st;
  reg [15:0] len;
  reg [7:0]  sh, rsh;
  reg [2:0]  nbit;
  reg [7:0]  wait_cnt;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st <= S_IDLE; len <= 0; sh <= 0; rsh <= 0; nbit <= 0; wait_cnt <= 0;
      sck <= 0; mosi <= 0; cs_n <= 1; tx_start <= 0; tx_byte <= 0;
    end else begin
      tx_start <= 0;
      case (st)
        S_IDLE:  if (rx_valid && rx_byte == 8'hA5) st <= S_LEN0;
        S_LEN0:  if (rx_valid) begin len[7:0] <= rx_byte; st <= S_LEN1; end
        S_LEN1:  if (rx_valid) begin
                   len[15:8] <= rx_byte;
                   if ({rx_byte, len[7:0]} == 16'd0) st <= S_IDLE;
                   else begin cs_n <= 0; wait_cnt <= HALF - 1; st <= S_CSLOW; end
                 end
        S_CSLOW: if (wait_cnt == 0) st <= S_WAITB; else wait_cnt <= wait_cnt - 1;
        S_WAITB: if (rx_valid) begin sh <= rx_byte; nbit <= 7; st <= S_SET; end
        S_SET:   begin mosi <= sh[7]; wait_cnt <= HALF - 1; st <= S_LO; end
        S_LO:    if (wait_cnt == 0) begin
                   rsh <= {rsh[6:0], miso};     // sample MISO at the rise
                   sck <= 1; wait_cnt <= HALF - 1; st <= S_HI;
                 end else wait_cnt <= wait_cnt - 1;
        S_HI:    if (wait_cnt == 0) begin
                   sck <= 0; sh <= {sh[6:0], 1'b0};
                   if (nbit == 0) st <= S_BYTE;
                   else begin nbit <= nbit - 1; st <= S_SET; end
                 end else wait_cnt <= wait_cnt - 1;
        S_BYTE:  if (!tx_busy) begin            // one byte in, one byte out: TX keeps up
                   tx_byte <= rsh; tx_start <= 1; len <= len - 1;
                   if (len == 1) begin wait_cnt <= HALF - 1; st <= S_END; end
                   else st <= S_WAITB;
                 end
        S_END:   if (wait_cnt == 0) begin cs_n <= 1; wait_cnt <= GAP - 1; st <= S_GAP; end
                 else wait_cnt <= wait_cnt - 1;
        S_GAP:   if (wait_cnt == 0) st <= S_IDLE; else wait_cnt <= wait_cnt - 1;
        default: st <= S_IDLE;
      endcase
    end
  end

endmodule
