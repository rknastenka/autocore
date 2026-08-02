// Fixture: `chip` instantiates one module that exists in the tree and one
// vendor primitive that does not. The missing name must become an
// external_ref plus a warning, never a failure.
module chip (
    input  wire        clk,
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [31:0] y
);

  wire clk_pll;

  SB_PLL40_CORE u_pll (
      .REFERENCECLK(clk),
      .PLLOUTCORE  (clk_pll)
  );

  alu_core u_alu (
      .a(a),
      .b(b),
      .y(y)
  );

endmodule
