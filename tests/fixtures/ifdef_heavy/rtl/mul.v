// Reached only when USE_MUL is defined.
module mul #(
    parameter integer WIDTH = 8
) (
    input  wire             clk,
    input  wire [WIDTH-1:0] a,
    input  wire [WIDTH-1:0] b,
    output reg  [WIDTH-1:0] y
);

  always @(posedge clk) begin
    y <= a * b;
  end

endmodule
