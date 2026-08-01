
<p align="left">
  <img src="logo.png" alt="Logo"  height="110">
</p>

<!-- # AutoCore -->

[![PyPI](https://img.shields.io/pypi/v/auto-core.svg)](https://pypi.org/project/auto-core/)
[![License: BSD-2-Clause](https://img.shields.io/badge/license-BSD--2--Clause-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

---

<!-- AutoCore is a CLI tool that scans an RTL tree and emits a FuseSoC [CAPI2](https://fusesoc.readthedocs.io/en/stable/ref/capi2.html) `.core` file.

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
and skipped. -->

**Autocore** is a small open-source Python CLI that scans a folder of Verilog/SystemVerilog and generates a working FuseSoC `.core` manifest, the YAML file FuseSoC needs to know what the source files are, how they depend on each other, and what the top level module is. Normally these are written by hand, which is tedious and easy to get wrong. Autocore does it automatically: point it at RTL, run one command, get a valid manifest.

Under the hood it's a strict four stage pipeline, **Scan, Parse, Resolve, Emit**, built over frozen dataclasses, using `pyslang` to properly parse the SystemVerilog and Verilog and `ruamel.yaml` to write out comment preserving YAML. Everything is built around one hard rule: the same input tree has to produce byte identical output every time, no matter the machine or run.

It started as just an MVP that scans files, works out dependencies, and emits a `default` target. That part is solid. Testbench and simulation target support has since been added: it can detect testbenches, wire up a `sim` target, and knows to emit `tools: {verilator: {mode: binary}}` when a self contained SV testbench needs it, which is the one deliberate exception to an otherwise strict "don't guess at tool options" rule. When there's genuine ambiguity, like multiple plausible testbench tops, it picks one deterministically and drops a short warning comment into the YAML rather than stopping to ask.

Next up is VHDL support, and eventually PyPI packaging and release polish. Longer term there's an idea for a `--split-by-dir` mode that would carve a project into multiple `.core` files by directory (similar to how lowRISC's ibex is structured). Mechanically splitting files is doable, but figuring out what actually deserves its own versioned core isn't something a scan can infer, so that one remains a documented idea rather than planned work.

The whole thing is validated against `olofk/serv` as the main real world test case and `picorv32` as a stress test, using FuseSoC's own CAPI2 loader as the ground truth for whether something generated is actually valid.


## Installation

```sh
pipx install git+https://github.com/rknastenka/autocore.git
autocore --version
```

The PyPI distribution coming soon


## Usage

```sh
autocore init .                           # scans whatever directory you're in
```
or
```sh
autocore init path/to/rtl-tree            # writes path/to/rtl-tree/<name>.core
autocore init . --dry-run                 # print it instead, and decide later
autocore init . --define SYNTHESIS --define WIDTH=32
autocore init . --top my_chip --yes       # no questions, no guessing top
```

Warnings go to stderr and the manifest to the file (or to stdout under
`--dry-run`), so the two never mix. Repetitive warnings are summarised into
one counted line; `-v` expands every one of them in full, and `-q` silences
them. `autocore init --help` lists every flag.



## Reproducing the serv/picorv32 validation

There's a script for that, `tests/corpus.py`, you can point
it at fresh clones of both repos.


<details>
<summary>What the script does?</summary>

`tests/corpus.py` takes a real cloned repo, runs `autocore init` on it, and
checks the result three ways: whether autocore exited successfully, whether
FuseSoC's own strict loader accepts the `.core` file as schema valid and
resolves real files from it, and, optionally, whether Verilator actually
builds it. It isn't testing autocore's code directly, it's testing
autocore's output against the real consumers, FuseSoC and Verilator, that
would actually use it.

</details>


First, from inside your clone of this repo (the one with `pyproject.toml`
in it), install the dependencies:

```sh
cd path/to/autocore                # this repo folder
pip install -e ".[dev]"            # brings in fusesoc==2.4.6, the strict loader
sudo apt-get install verilator     # only needed if you pass --verilate
```

Then clone serv and picorv32 somewhere else, anywhere is fine:

```sh
git clone --depth 1 https://github.com/YosysHQ/picorv32 /tmp/picorv32
git clone --depth 1 https://github.com/olofk/serv /tmp/serv
```

And run the script (still from inside the autocore repo) against each one:

```sh
# picorv32 is the stress test. It has several plausible top level modules,
# so this checks that autocore picks one and warns about it, instead of
# just checking that the command succeeds.
python tests/corpus.py /tmp/picorv32 --expect-warning MultipleTops

# serv is the main case. Left to its own defaults autocore lands on a board
# wrapper, so this just confirms the manifest it writes is still valid.
python tests/corpus.py /tmp/serv

# Running it again on serv, but telling it the real top and asking it to
# build with Verilator, shows the generated .core isn't just valid on
# paper, it's something FuseSoC and the tool actually accept and can build.
python tests/corpus.py /tmp/serv --top serv_top --subdir autocore_verilator --verilate
```

Each of those runs does the same four things behind the scenes. It runs the
installed `autocore init --yes` command on the clone and captures its
warnings. It then runs the same thing again, in process this time through
`autocore.generate()`, and checks the two outputs match byte for byte (a
handy determinism check, since the two runs land on different Python hash
seeds). It loads the written `.core` file with FuseSoC's own strict loader
and checks the resolved file list looks right, not just that the file
parses. And if you passed `--verilate`, it hands the manifest to
`fusesoc run --tool=verilator` and checks the build or lint actually
succeeds.

The output always lands in a separate, clean subdirectory of the clone
(`--subdir`, `autocore_out` by default), since picorv32 already ships its
own `picorv32.core` that would otherwise get overwritten and also fails
strict validation on its own.

This used to run automatically in CI on every push, but that workflow is
currently switched off. So for now, running it yourself locally is the way to see the
validation happen.

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
one run describes one configuration of the tree, the one you asked for.

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

to the testbench fileset. Anything your design pulls in from another core,
a shared bus library, a vendor package, a CPU you did not write, needs the
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