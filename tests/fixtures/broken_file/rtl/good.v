// The healthy half of the fixture: parses, declares `good`, instantiates
// `counter`, and must survive its neighbour failing.
module good (
    input  wire       clk,
    input  wire       rst_n,
    output wire [7:0] count
);

  counter u_counter (
      .clk  (clk),
      .rst_n(rst_n),
      .q    (count)
  );

endmodule
