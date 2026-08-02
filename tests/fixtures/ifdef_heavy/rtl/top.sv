// `mul` is instantiated if and only if USE_MUL is defined; `shifter` takes its
// place otherwise. Both leaves exist on disk, so the difference between the two
// parses is entirely about the define, not about what Scan found.
`include "config.svh"

module top (
    input  wire                 clk,
    input  wire [`AC_WIDTH-1:0] a,
    input  wire [`AC_WIDTH-1:0] b,
    output wire [`AC_WIDTH-1:0] y
);

  wire [`AC_WIDTH-1:0] sum;

  adder #(
      .WIDTH(`AC_WIDTH)
  ) u_adder (
      .a(a),
      .b(b),
      .y(sum)
  );

`ifdef USE_MUL
  mul #(
      .WIDTH(`AC_WIDTH)
  ) u_mul (
      .clk(clk),
      .a  (sum),
      .b  (b),
      .y  (y)
  );
`else
  shifter #(
      .WIDTH(`AC_WIDTH)
  ) u_shifter (
      .a(sum),
      .y(y)
  );
`endif

endmodule
