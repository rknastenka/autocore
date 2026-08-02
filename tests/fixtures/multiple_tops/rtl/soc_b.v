module soc_b (
    input  wire        clk,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [31:0] y
);

  common_alu u_alu (
      .a(b),
      .b(a),
      .y(y)
  );

endmodule
