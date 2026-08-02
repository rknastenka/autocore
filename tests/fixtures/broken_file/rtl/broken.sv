// This file does not parse, on purpose. The port list is never closed and
// `endmodule` never arrives.
module broken (
    input logic clk,
    input logic
