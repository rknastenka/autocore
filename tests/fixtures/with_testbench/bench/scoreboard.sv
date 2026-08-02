
module scoreboard;

  int errors;

  initial begin
    errors = 0;
    #900;
    if (errors != 0) begin
      $display("scoreboard: %0d error(s)", errors);
      $stop;
    end
    $finish;
  end

endmodule
