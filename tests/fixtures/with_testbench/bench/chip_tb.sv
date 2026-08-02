`timescale 1ns / 1ps

module chip_tb;

  wire        clk;
  wire [31:0] y;
  wire        ok;

  clkgen u_clkgen (.clk(clk));

  chip dut (
      .clk(clk),
      .a  (32'd1),
      .b  (32'd2),
      .y  (y),
      .ok (ok)
  );

  scoreboard u_scoreboard ();

  initial begin
    #1000;
    $finish;
  end

endmodule
