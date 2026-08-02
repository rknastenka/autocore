# ifdef_heavy

Everything interesting in this tree sits behind a `` `ifdef ``.

Parsed with no defines, `top` instantiates `adder` and `shifter`. Parsed with
`--define USE_MUL` it instantiates `adder` and `mul` instead. pyslang collects
instantiations pre-elaboration, so a guarded instantiation appears if and only
if the define is active — this fixture is what asserts that

`AC_WIDTH` covers the `NAME=VALUE` half of `--define`: `include/config.svh`
supplies a default only when nobody passed one, so `--define AC_WIDTH=32` wins
without editing the tree.
