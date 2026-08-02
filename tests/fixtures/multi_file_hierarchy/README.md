# multi_file_hierarchy

`top` (SystemVerilog) instantiates `regfile` (SystemVerilog) and `alu` (plain
Verilog), with shared macros in `include/defs.svh`. Exercises subdirectories,
mixed languages in one fileset, and an include candidate that declares no
modules.

This file is here on purpose: Scan must ignore anything that is not one of the
six source extensions.
