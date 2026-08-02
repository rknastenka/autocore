
module chip (
    input  wire        clk,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [31:0] y,
    output wire        ok
);

  alu u_alu (
      .a(a),
      .b(b),
      .y(y)
  );

  self_test u_self_test (
      .clk     (clk),
      .observed(y),
      .ok      (ok)
  );

endmodule
