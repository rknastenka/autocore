
<p align="left">
  <img src="logo.png" alt="Logo"  height="110">
</p>

<!-- # AutoCore -->

[![PyPI](https://img.shields.io/pypi/v/auto-core.svg)](https://pypi.org/project/auto-core/)
[![License: BSD-2-Clause](https://img.shields.io/badge/license-BSD--2--Clause-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

---

AutoCore is a CLI tool that scans an RTL tree and emits a FuseSoC [CAPI2](https://fusesoc.readthedocs.io/en/stable/ref/capi2.html) `.core` file.

It exists to lower the barrier to start using [FuseSoc](https://github.com/olofk/fusesoc) and [Edalize](https://github.com/olofk/edalize),
you only run one command `autocore init` and get a `.core` file. Review what it wrote,
then own the file yourself. 
Getting your core file specifically for your project RTL structure in one click, 
without reading any fuseSoc docs or spending hours fixing syntax errors

It's written fully in python, and has a base architecture pipeline of:
**Scan → Parse → Resolve → Emit**. This pipeline supports Verilog and SystemVerilog trees end to end
producing an `rtl` fileset with a
`default` target, plus a `tb` fileset and a `sim` target when it finds
testbenches.
VHDL is not supported yet; `.vhd`/`.vhdl` files are reported
and skipped.


## Installation

```sh
pipx install git+https://github.com/rknastenka/autocore.git
autocore --version
```

The PyPI distribution coming soon


## Usage

```sh
autocore init path/to/rtl-tree            # writes path/to/rtl-tree/<name>.core
autocore init . --dry-run                 # print it instead, and decide later
autocore init . --define SYNTHESIS --define WIDTH=32
autocore init . --top my_chip --yes       # no questions, no guessing
```

Warnings go to stderr and the manifest to the file (or to stdout under
`--dry-run`), so the two never mix. Repetitive warnings are summarised into
one counted line; `-v` expands every one of them in full, and `-q` silences
them. `autocore init --help` lists every flag.



## Limitations

AutoCore handles the mechanical side of writing a `.core` file; finding the files, figuring out what instantiates what, ordering them, taking a guess at the toplevel. That's most of the typing, but none of the judgement. The list below covers the judgement calls, so you know what to double-check instead of finding out the hard way. *Read what it generates before you trust it.* Once it runs, the file is yours. AutoCore won't touch it again.

### Macros that cross file boundaries

Each file is parsed on its own, exactly as it sits on disk. A macro one file
defines and another one uses is invisible: nothing is elaborated, no
compile-unit scope is shared, and there is no include-order guesswork. If a
file leans on such a macro, it usually fails to parse (see below), and if the
macro decides which module gets instantiated, the instantiation is simply not
seen.

`--define NAME` and `--define NAME=VALUE` are the escape hatch. They apply to
every file in the tree, so a project whose build passes `-DSYNTHESIS` should
run auto-core the same way:

```sh
autocore init . --define SYNTHESIS --define XLEN=32 -v
```

`-v` prints the defines in effect, which is worth checking before believing a
suspiciously small `rtl` fileset. The same applies to `` `ifdef ``-guarded
instantiations: they are collected if and only if the define is active, so
one run describes one configuration of the tree — the one you asked for.

Module names built out of macros are beyond the escape hatch. Nothing short
of elaboration can resolve those, and auto-core does not elaborate.

### Vendor primitives and other external references

A module instantiated somewhere in the tree but declared nowhere in it, an
`SB_SPRAM256KA`, an `MMCME2_BASE`, an encrypted core, a file you did not
scan, is reported as an external reference and otherwise left alone. That is
never an error: it is normal, and the generated manifest is still correct
about the files it does describe. What it cannot do is supply the missing
module, so a build from that manifest fails at the tool (Verilator says
`MODMISSING`) until you add the vendor library yourself.

### Files that fail to parse

A file auto-core cannot parse produces a warning naming it and is left out of
the graph entirely, never a crash, never a partial guess. The usual causes
are cross-file macros, tool-specific dialects, and SystemVerilog assertions
in files that are not really compiled with the rest of the design. If a
parse failure took out something that belongs in your build, add it to the
generated file by hand, or try the `--define` that the file's own build uses.

### Cross-core dependencies

auto-core emits **one** self-contained `.core` file describing **one** tree.
It never writes a `depend:` entry, because it has no way to know the VLNV of
another core, and inventing one would be worse than saying nothing.

The concrete case: [serv](https://github.com/olofk/serv)'s testbench
instantiates `vlog_tb_utils`, which lives in FuseSoC's separate
`fusesoc:utils:vlog_tb_utils` core. auto-core reports `vlog_tb_utils` as an
external reference and stops there; the generated `sim` target will not build
until a human adds

```yaml
    depend:
      - fusesoc:utils:vlog_tb_utils
```

to the testbench fileset. Anything your design pulls in from another core —
a shared bus library, a vendor package, a CPU you did not write — needs the
same one-line edit.

### Testbench classification is a rule, not an understanding

A file is a testbench if its name matches `*_tb.*`, `tb_*.*`, `*_test.*` or
`testbench.*`, or if it both calls `$finish`/`$stop` and declares a module
with no ports. Everything else is design. Where that rule guesses wrong, say
so in the file itself with `// autocore: tb` or `// autocore: rtl`, or
replace the filename patterns with `--tb-glob`. Exactly one `sim` target is
generated, from one testbench toplevel; benches outside its closure are
named in a warning rather than silently dropped.

### Where the `.core` file goes

The default output sits at the root of the scanned tree, which is where
FuseSoC wants it. Pointing `--output` somewhere else, a `build/`
subdirectory, a directory beside the tree, makes the generated paths reach
upward (`../rtl/alu.v`), which FuseSoC 2.4.6 accepts with a deprecation
warning and a future version will reject. auto-core warns when the output you
chose has that shape.

---

**that's it, enjoy your day :)**