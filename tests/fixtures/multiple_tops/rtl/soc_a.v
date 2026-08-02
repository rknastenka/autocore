
module soc_a (
    input  wire        clk,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [31:0] y
);

  common_alu u_alu (
      .a(a),
      .b(b),
      .y(y)
  );

endmodule
