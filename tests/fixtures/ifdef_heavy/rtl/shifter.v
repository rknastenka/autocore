// Reached only when USE_MUL is *not* defined.
module shifter #(
    parameter integer WIDTH = 8
) (
    input  wire [WIDTH-1:0] a,
    output wire [WIDTH-1:0] y
);

  assign y = a << 1;

endmodule
