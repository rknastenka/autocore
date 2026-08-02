// autocore: tb
`timescale 1ns / 1ps

module clkgen (
    output reg clk
);

  initial clk = 1'b0;

  always #5 clk = ~clk;

endmodule
