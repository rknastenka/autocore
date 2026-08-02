# broken_file

One file that parses and one that does not.

`rtl/broken.sv` never closes its port list and never reaches `endmodule`, so
pyslang reports error-severity diagnostics for it. 

what must happen next: a warning, the file dropped from the graph, and the rest of the
tree parsed as usual. `rtl/good.v` is that rest — it must come back with its
facts intact.

Nothing here may make the pipeline exit non-zero. A tree with a broken file in it
is still a tree autocore generates a `.core` for.
