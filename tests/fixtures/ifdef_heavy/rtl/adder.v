// Unguarded leaf: instantiated in both parses.
module adder #(
    parameter integer WIDTH = 8
) (
    input  wire [WIDTH-1:0] a,
    input  wire [WIDTH-1:0] b,
    output wire [WIDTH-1:0] y
);

  assign y = a + b;

endmodule
