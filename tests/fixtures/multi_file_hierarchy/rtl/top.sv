
`include "defs.svh"

module top (
    input  logic                    clk,
    input  logic                    rst_n,
    input  logic [`AC_ADDR_W-1:0]   addr,
    output logic [`AC_DATA_W-1:0]   result
);

  logic [`AC_DATA_W-1:0] operand_a;
  logic [`AC_DATA_W-1:0] operand_b;

  regfile u_regfile (
      .clk  (clk),
      .rst_n(rst_n),
      .addr (addr),
      .a    (operand_a),
      .b    (operand_b)
  );

  alu u_alu (
      .a(operand_a),
      .b(operand_b),
      .y(result)
  );

endmodule
