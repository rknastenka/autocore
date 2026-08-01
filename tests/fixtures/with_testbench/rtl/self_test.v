`timescale 1ns / 1ps
// autocore: rtl
//
// The magic comment forcing the RTL direction: this file's name matches the
// D16 pattern `*_test.*`, so without the comment it would be classified as a
// testbench and `chip` would lose a leaf. It is a built-in self test — real
// synthesised logic — and the comment is how a tree says so.
module self_test (
    input  wire        clk,
    input  wire [31:0] observed,
    output reg         ok
);

  always @(posedge clk) begin
    ok <= observed != 32'hdeadbeef;
  end

endmodule
