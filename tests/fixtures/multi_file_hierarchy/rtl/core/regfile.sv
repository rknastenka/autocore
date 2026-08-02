`include "defs.svh"

module regfile (
    input  logic                  clk,
    input  logic                  rst_n,
    input  logic [`AC_ADDR_W-1:0] addr,
    output logic [`AC_DATA_W-1:0] a,
    output logic [`AC_DATA_W-1:0] b
);

  logic [`AC_DATA_W-1:0] mem[(1 << `AC_ADDR_W)];

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      a <= '0;
      b <= '0;
    end else begin
      a <= mem[addr];
      b <= mem[addr+1];
    end
  end

endmodule
