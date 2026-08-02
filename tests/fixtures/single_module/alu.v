
module alu #(
    parameter integer WIDTH = 8
) (
    input  wire [WIDTH-1:0] a,
    input  wire [WIDTH-1:0] b,
    input  wire [1:0]       op,
    output reg  [WIDTH-1:0] y
);

  always @* begin
    case (op)
      2'b00:   y = a + b;
      2'b01:   y = a - b;
      2'b10:   y = a & b;
      default: y = a | b;
    endcase
  end

endmodule
