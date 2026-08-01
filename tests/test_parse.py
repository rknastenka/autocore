"""Unit tests for Stage 2, Parse.

The `ifdef_heavy` and `broken_file` fixtures pin the two headline behaviours: a
guarded instantiation appears only when its define is active, and a file that
will not parse costs a warning rather than the run. Narrower cases are built in
`tmp_path`, so the source sits next to the assertion about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from autocore.models import (
    FileFacts,
    Lang,
    ParseResult,
    ScanResult,
    TbDirective,
    TbEvidence,
    Warning,
)
from autocore.parse import ParseError, SvSlangBackend, parse_all, parse_sources
from autocore.scan import scan

FIXTURES = Path(__file__).parent / "fixtures"


def build(root: Path, layout: dict[str, str]) -> Path:
    """Materialise `{relative path: contents}` under `root`."""
    for path, contents in layout.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    return root


def facts_for(result: ParseResult, name: str) -> FileFacts:
    """The one `FileFacts` whose path ends in `name`."""
    matches = [f for f in result.files if f.path.as_posix().endswith(name)]
    assert len(matches) == 1, f"{name}: {[f.path.name for f in result.files]}"
    return matches[0]


def parse_one(path: Path, defines: tuple[str, ...] = (), **kwargs: object) -> FileFacts:
    """Run the real backend over a single file, outside the driver."""
    return SvSlangBackend(**kwargs).parse(path, defines)  # type: ignore[arg-type]


def names(result: ParseResult) -> tuple[str, ...]:
    return tuple(f.path.name for f in result.files)


# fixtures on disk


def test_single_module_fixture() -> None:
    result = parse_sources(scan(FIXTURES / "single_module"))

    assert result.warnings == ()
    (alu,) = result.files
    assert alu.language == Lang.VERILOG
    assert alu.declared == frozenset({"alu"})
    assert alu.instantiated == frozenset()
    assert alu.includes == frozenset()


def test_multi_file_hierarchy_fixture() -> None:
    result = parse_sources(scan(FIXTURES / "multi_file_hierarchy"))

    assert result.warnings == ()
    assert names(result) == ("defs.svh", "alu.v", "regfile.sv", "top.sv")

    top = facts_for(result, "rtl/top.sv")
    assert top.language == Lang.SYSTEMVERILOG
    assert top.declared == frozenset({"top"})
    assert top.instantiated == frozenset({"alu", "regfile"})
    assert top.includes == frozenset({"defs.svh"})

    header = facts_for(result, "include/defs.svh")
    assert header.declared == frozenset()
    assert header.instantiated == frozenset()


def test_header_directories_become_include_paths() -> None:
    """`for_tree` is what makes `include/` reachable from `rtl/` at all."""
    root = FIXTURES / "multi_file_hierarchy"
    backend = SvSlangBackend.for_tree(scan(root))

    assert backend.include_dirs == (root / "include",)


def test_without_include_paths_the_same_file_fails() -> None:
    """Why `for_tree` exists: an unreachable header takes its macros with it."""
    with pytest.raises(ParseError):
        parse_one(FIXTURES / "multi_file_hierarchy" / "rtl" / "top.sv")


# ifdef_heavy: the define-gated instantiation


def test_guarded_instantiation_appears_iff_the_define_is_passed() -> None:
    tree = scan(FIXTURES / "ifdef_heavy")

    without = facts_for(parse_sources(tree), "rtl/top.sv")
    with_mul = facts_for(parse_sources(tree, defines=["USE_MUL"]), "rtl/top.sv")

    assert "mul" not in without.instantiated
    assert "mul" in with_mul.instantiated
    assert "shifter" in without.instantiated
    assert "shifter" not in with_mul.instantiated
    # Anything outside the guard is unaffected either way.
    assert "adder" in without.instantiated
    assert "adder" in with_mul.instantiated


def test_ifdef_heavy_parses_clean_in_both_modes() -> None:
    """No diagnostics either way, so the assertion above measures the define."""
    tree = scan(FIXTURES / "ifdef_heavy")

    assert parse_sources(tree).warnings == ()
    assert parse_sources(tree, defines=["USE_MUL", "AC_WIDTH=32"]).warnings == ()


def test_a_valued_define_reaches_the_preprocessor(tmp_path: Path) -> None:
    """`NAME=VALUE`, the other half of `--define`."""
    build(
        tmp_path,
        {
            "top.sv": (
                "`ifndef PRIM\n"
                "`define PRIM prim_default\n"
                "`endif\n"
                "module top;\n"
                "  `PRIM u_prim ();\n"
                "endmodule\n"
            )
        },
    )
    tree = scan(tmp_path)

    assert facts_for(parse_sources(tree), "top.sv").instantiated == frozenset(
        {"prim_default"}
    )
    overridden = parse_sources(tree, defines=["PRIM=prim_fast"])
    assert facts_for(overridden, "top.sv").instantiated == frozenset({"prim_fast"})


# broken_file: never fatal


def test_broken_file_warns_and_is_excluded() -> None:
    result = parse_sources(scan(FIXTURES / "broken_file"))

    assert names(result) == ("good.v",)

    (warning,) = result.warnings
    assert warning.code == "ParseFailed"
    assert warning.path is not None
    assert warning.path.name == "broken.sv"
    assert warning.message


def test_the_healthy_neighbour_keeps_its_facts() -> None:
    result = parse_sources(scan(FIXTURES / "broken_file"))

    good = facts_for(result, "good.v")
    assert good.declared == frozenset({"good"})
    assert good.instantiated == frozenset({"counter"})


def test_a_tree_of_nothing_but_broken_files_still_returns(tmp_path: Path) -> None:
    build(tmp_path, {"a.sv": "module a (", "b.sv": "endmodule endmodule"})

    result = parse_sources(scan(tmp_path))

    assert result.files == ()
    assert len(result.warnings) == 2


def test_warning_messages_carry_no_paths_or_line_numbers(tmp_path: Path) -> None:
    """An embedded absolute path would make the output non-deterministic."""
    build(tmp_path, {"a.sv": "module a ("})

    (warning,) = parse_sources(scan(tmp_path)).warnings

    assert str(tmp_path) not in warning.message


def test_many_diagnostics_are_summarised(tmp_path: Path) -> None:
    build(tmp_path, {"a.sv": "!!! this is not verilog at all @@@ 12345 ;;;\n"})

    (warning,) = parse_sources(scan(tmp_path)).warnings

    assert warning.code == "ParseFailed"
    assert warning.message.count(";") == 2  # MAX_REPORTED_DIAGNOSTICS - 1 joiners
    assert warning.message.endswith("(and 2 more)")


def test_a_file_that_vanished_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    missing = tmp_path / "gone.v"
    result = parse_sources(ScanResult(root=tmp_path, files=(missing,)))

    assert result.files == ()
    (warning,) = result.warnings
    assert warning.code == "FileUnreadable"
    assert warning.path == missing


# declarations, instantiations, imports


def test_the_three_declaration_kinds(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "all.sv": (
                "package pkg_a;\nendpackage\n"
                "interface bus_if;\nendinterface\n"
                "module top;\nendmodule\n"
            )
        },
    )

    facts = parse_one(tmp_path / "all.sv")

    assert facts.declared == frozenset({"pkg_a", "bus_if", "top"})


def test_a_nested_module_is_declared_too(tmp_path: Path) -> None:
    build(
        tmp_path, {"n.sv": "module outer;\n  module inner;\n  endmodule\nendmodule\n"}
    )

    assert parse_one(tmp_path / "n.sv").declared == frozenset({"outer", "inner"})


def test_generate_block_instantiations_are_collected(tmp_path: Path) -> None:
    """Pre-elaboration, so the loop body counts without being unrolled."""
    build(
        tmp_path,
        {
            "g.sv": (
                "module top;\n"
                "  generate\n"
                "    for (genvar i = 0; i < 4; i++) begin : g\n"
                "      leaf u_leaf ();\n"
                "    end\n"
                "  endgenerate\n"
                "endmodule\n"
            )
        },
    )

    assert parse_one(tmp_path / "g.sv").instantiated == frozenset({"leaf"})


def test_an_interface_instance_counts_as_an_instantiation(tmp_path: Path) -> None:
    build(tmp_path, {"i.sv": "module top;\n  bus_if u_bus ();\nendmodule\n"})

    assert parse_one(tmp_path / "i.sv").instantiated == frozenset({"bus_if"})


def test_package_imports_in_every_shape(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "p.sv": (
                "import pkg_unit::*;\n"
                "module top;\n"
                "  import pkg_wild::*;\n"
                "  import pkg_one::thing, pkg_two::other;\n"
                "endmodule\n"
            )
        },
    )

    facts = parse_one(tmp_path / "p.sv")

    assert facts.imported_pkgs == frozenset(
        {"pkg_unit", "pkg_wild", "pkg_one", "pkg_two"}
    )


def test_a_file_with_nothing_in_it_yields_empty_facts(tmp_path: Path) -> None:
    build(tmp_path, {"empty.v": ""})

    facts = parse_one(tmp_path / "empty.v")

    assert facts.declared == frozenset()
    assert facts.instantiated == frozenset()
    assert facts.imported_pkgs == frozenset()
    assert facts.includes == frozenset()


# includes, both paths


def test_a_resolved_include_is_recorded_without_its_quotes(tmp_path: Path) -> None:
    build(tmp_path, {"hdr/defs.svh": "`define W 8\n", "src/top.sv": ""})
    (tmp_path / "src" / "top.sv").write_text(
        '`include "defs.svh"\nmodule top;\n  wire [`W-1:0] w;\nendmodule\n'
    )

    facts = parse_one(tmp_path / "src" / "top.sv", include_dirs=(tmp_path / "hdr",))

    assert facts.includes == frozenset({"defs.svh"})


def test_an_unresolved_include_survives_in_trivia(tmp_path: Path) -> None:
    """With no file to splice, the directive stays behind as trivia."""
    build(tmp_path, {"top.sv": '`include "nowhere.svh"\nmodule top;\nendmodule\n'})

    facts = parse_one(tmp_path / "top.sv")

    assert facts.includes == frozenset({"nowhere.svh"})
    assert facts.declared == frozenset({"top"})  # parsing continued


def test_an_unresolved_include_is_not_a_parse_failure(tmp_path: Path) -> None:
    build(tmp_path, {"top.sv": '`include "nowhere.svh"\nmodule top;\nendmodule\n'})

    result = parse_sources(scan(tmp_path))

    assert result.warnings == ()
    assert names(result) == ("top.sv",)


def test_angle_bracket_includes_lose_their_brackets(tmp_path: Path) -> None:
    build(tmp_path, {"top.sv": "`include <vendor.svh>\nmodule top;\nendmodule\n"})

    assert parse_one(tmp_path / "top.sv").includes == frozenset({"vendor.svh"})


def test_resolved_and_unresolved_includes_land_in_one_set(tmp_path: Path) -> None:
    build(tmp_path, {"hdr/there.svh": "`define X 1\n"})
    build(
        tmp_path,
        {
            "src/top.sv": (
                '`include "there.svh"\n`include "missing.svh"\nmodule top;\nendmodule\n'
            )
        },
    )

    facts = parse_one(tmp_path / "src" / "top.sv", include_dirs=(tmp_path / "hdr",))

    assert facts.includes == frozenset({"there.svh", "missing.svh"})


def test_a_transitively_included_header_is_recorded(tmp_path: Path) -> None:
    """slang reports the whole chain; Resolve needs every directory in it."""
    build(
        tmp_path,
        {
            "hdr/outer.svh": '`include "inner.svh"\n',
            "hdr/inner.svh": "`define X 1\n",
            "src/top.sv": '`include "outer.svh"\nmodule top;\nendmodule\n',
        },
    )

    facts = parse_one(tmp_path / "src" / "top.sv", include_dirs=(tmp_path / "hdr",))

    assert facts.includes == frozenset({"outer.svh", "inner.svh"})


# the backend seam


def test_vhdl_is_reported_not_parsed(tmp_path: Path) -> None:
    """Scan collects `.vhd`; there is no backend for it yet."""
    """Scan collects `.vhd`; there is no backend for it yet."""
    build(tmp_path, {"top.v": "module top;\nendmodule\n", "old.vhd": "entity e is"})

    result = parse_sources(scan(tmp_path))

    assert names(result) == ("top.v",)
    (warning,) = result.warnings
    assert warning.code == "UnsupportedLanguage"
    assert warning.path is not None
    assert warning.path.name == "old.vhd"


def test_the_backend_refuses_vhdl_directly_too(tmp_path: Path) -> None:
    build(tmp_path, {"old.vhd": "entity e is"})

    with pytest.raises(ParseError) as caught:
        parse_one(tmp_path / "old.vhd")

    assert caught.value.code == "UnsupportedLanguage"


def test_parse_error_becomes_exactly_one_warning() -> None:
    error = ParseError(Path("/rtl/a.sv"), "expected ')'", code="ParseFailed")

    assert error.as_warning() == Warning(
        "ParseFailed", "expected ')'", Path("/rtl/a.sv")
    )


@dataclass(frozen=True)
class StubBackend:
    """A `ParserBackend` that never touches the disk."""

    languages = frozenset({Lang.VERILOG, Lang.SYSTEMVERILOG})

    def parse(self, path: Path, defines: object = ()) -> FileFacts:
        if path.name.startswith("bad"):
            raise ParseError(path, "stub refused")
        return FileFacts(path=path, language=Lang.VERILOG)


def test_the_driver_takes_any_backend(tmp_path: Path) -> None:
    build(tmp_path, {"good.v": "", "bad.v": ""})

    result = parse_all(scan(tmp_path), StubBackend(), max_workers=1)

    assert names(result) == ("good.v",)
    assert [w.message for w in result.warnings] == ["stub refused"]


def test_a_backend_that_raises_something_unexpected_is_survivable(
    tmp_path: Path,
) -> None:
    class Exploding:
        languages = frozenset({Lang.VERILOG})

        def parse(self, path: Path, defines: object = ()) -> FileFacts:
            raise ValueError("kaboom")

    build(tmp_path, {"a.v": ""})

    result = parse_all(scan(tmp_path), Exploding(), max_workers=1)

    assert result.files == ()
    assert result.warnings[0].code == "ParseFailed"
    assert "kaboom" in result.warnings[0].message


# tb evidence: evidence only, the parser classifies nothing


def evidence(tmp_path: Path, source: str) -> TbEvidence:
    build(tmp_path, {"e.sv": source})
    return parse_one(tmp_path / "e.sv").tb_evidence


def test_a_textbook_testbench_carries_all_three_bits(tmp_path: Path) -> None:
    found = evidence(
        tmp_path,
        "module tb;\n"
        "  dut u_dut ();\n"
        "  initial begin\n"
        "    #10 $finish;\n"
        "  end\n"
        "endmodule\n",
    )

    assert found == TbEvidence(
        has_finish_or_stop=True, empty_port_list=True, initial_heavy=True
    )
    assert found.strong and not found.partial


def test_ordinary_rtl_carries_none_of_them(tmp_path: Path) -> None:
    found = evidence(
        tmp_path,
        "module adder (\n"
        "    input wire [7:0] a,\n"
        "    output reg [7:0] y\n"
        ");\n"
        "  always @* y = a + 1;\n"
        "endmodule\n",
    )

    assert found == TbEvidence()
    assert not found.strong and not found.partial


def test_dollar_stop_counts_the_same_as_dollar_finish(tmp_path: Path) -> None:
    assert evidence(
        tmp_path, "module m (input a);\n  initial $stop;\nendmodule\n"
    ).has_finish_or_stop


def test_another_system_task_is_not_evidence(tmp_path: Path) -> None:
    """`$display` is as common in RTL as anywhere, so only `$finish` and
    `$stop` count."""
    assert not evidence(
        tmp_path, 'module m (input a);\n  initial $display("hi");\nendmodule\n'
    ).has_finish_or_stop


def test_both_spellings_of_an_empty_port_list(tmp_path: Path) -> None:
    assert evidence(tmp_path, "module m;\nendmodule\n").empty_port_list
    assert evidence(tmp_path, "module m ();\nendmodule\n").empty_port_list


def test_one_real_port_is_enough_to_not_be_empty(tmp_path: Path) -> None:
    assert not evidence(tmp_path, "module m (input clk);\nendmodule\n").empty_port_list
    # The non-ANSI spelling counts as ports too.
    assert not evidence(
        tmp_path, "module m (clk);\n  input clk;\nendmodule\n"
    ).empty_port_list


def test_any_module_in_the_file_having_empty_ports_is_enough(tmp_path: Path) -> None:
    """`FileFacts` is per file, so the evidence is too."""
    assert evidence(
        tmp_path, "module a (input x);\nendmodule\nmodule b;\nendmodule\n"
    ).empty_port_list


def test_initial_heavy_weighs_initial_against_structural_blocks(
    tmp_path: Path,
) -> None:
    # One stimulus block against one clock generator: a testbench.
    assert evidence(
        tmp_path,
        "module m (output reg clk);\n"
        "  initial clk = 0;\n"
        "  always #5 clk = ~clk;\n"
        "endmodule\n",
    ).initial_heavy
    # One power-on reset against two always blocks and an assign: RTL.
    assert not evidence(
        tmp_path,
        "module m (input clk, output reg q, output reg r, output wire w);\n"
        "  initial q = 0;\n"
        "  always @(posedge clk) q <= ~q;\n"
        "  always @(posedge clk) r <= q;\n"
        "  assign w = q;\n"
        "endmodule\n",
    ).initial_heavy


def test_an_instantiated_dut_does_not_weigh_against_initial(tmp_path: Path) -> None:
    """A testbench always instantiates its DUT, so counting instantiations
    would penalise the files this evidence is meant to notice."""
    assert evidence(
        tmp_path,
        "module m (input clk);\n"
        "  dut a ();\n  dut b ();\n  dut c ();\n"
        "  initial #10 ;\n"
        "endmodule\n",
    ).initial_heavy


def test_no_initial_block_is_never_initial_heavy(tmp_path: Path) -> None:
    assert not evidence(tmp_path, "module m;\nendmodule\n").initial_heavy


def test_partial_evidence_is_evidence_that_does_not_classify(tmp_path: Path) -> None:
    """The `UnclearTbStatus` shape: no ports, no `$finish`."""
    found = evidence(tmp_path, "module m;\n  initial #10 ;\nendmodule\n")

    assert found.partial and not found.strong


# magic comments: a user directive, not evidence


def directive_of(tmp_path: Path, source: str) -> TbDirective | None:
    build(tmp_path, {"d.sv": source})
    return parse_one(tmp_path / "d.sv").tb_directive


def test_both_magic_comments_are_read(tmp_path: Path) -> None:
    assert (
        directive_of(tmp_path, "// autocore: tb\nmodule m;\nendmodule\n")
        is TbDirective.TB
    )
    assert (
        directive_of(tmp_path, "// autocore: rtl\nmodule m;\nendmodule\n")
        is TbDirective.RTL
    )


def test_a_file_without_one_has_no_directive(tmp_path: Path) -> None:
    assert directive_of(tmp_path, "module m;\nendmodule\n") is None


def test_a_comment_above_a_directive_is_still_found(tmp_path: Path) -> None:
    """Why the trivia scan recurses: slang folds a comment written above a
    preprocessor directive into that directive's own trivia, where it never
    reaches a real token."""
    assert (
        directive_of(
            tmp_path,
            "// autocore: tb\n`timescale 1ns / 1ps\nmodule m;\nendmodule\n",
        )
        is TbDirective.TB
    )


def test_spacing_case_and_comment_style_are_all_forgiven(tmp_path: Path) -> None:
    for comment in (
        "//autocore:tb",
        "//   AutoCore  :  TB",
        "/* autocore: tb */",
        "// note to self -- autocore: tb -- keeps it out of rtl",
    ):
        assert (
            directive_of(tmp_path, f"{comment}\nmodule m;\nendmodule\n")
            is TbDirective.TB
        ), comment


def test_prose_mentioning_autocore_does_not_classify(tmp_path: Path) -> None:
    for comment in (
        "// autocore generated this file",
        "// autocore: leave this alone",
        "// autocorrect: tb",
    ):
        assert directive_of(tmp_path, f"{comment}\nmodule m;\nendmodule\n") is None, (
            comment
        )


def test_a_comment_anywhere_in_the_file_counts(tmp_path: Path) -> None:
    assert (
        directive_of(tmp_path, "module m;\nendmodule\n// autocore: tb\n")
        is TbDirective.TB
    )


def test_a_file_carrying_both_records_the_conflict(tmp_path: Path) -> None:
    """Recording the contradiction keeps the result independent of walk order.
    Resolve is what says so out loud."""
    assert (
        directive_of(
            tmp_path, "// autocore: tb\n// autocore: rtl\nmodule m;\nendmodule\n"
        )
        is TbDirective.CONFLICTING
    )


def test_the_same_directive_twice_is_not_a_conflict(tmp_path: Path) -> None:
    assert (
        directive_of(
            tmp_path, "// autocore: tb\nmodule m;\n// autocore: tb\nendmodule\n"
        )
        is TbDirective.TB
    )


# parallelism, ordering and determinism


def test_results_are_sorted_by_path(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "z/z.v": "module zz;\nendmodule\n",
            "z/a.v": "module za;\nendmodule\n",
            "m.v": "module m;\nendmodule\n",
            "a/z.v": "module az;\nendmodule\n",
            "a/a.v": "module aa;\nendmodule\n",
        },
    )

    result = parse_sources(scan(tmp_path))
    relative = [f.path.relative_to(tmp_path).as_posix() for f in result.files]

    assert relative == sorted(relative)
    assert relative == ["a/a.v", "a/z.v", "m.v", "z/a.v", "z/z.v"]


def test_the_parallel_and_serial_paths_agree() -> None:
    """Five files, so the default path really does fan out over processes."""
    tree = scan(FIXTURES / "ifdef_heavy")

    assert parse_sources(tree, max_workers=1) == parse_sources(tree, max_workers=4)


def test_repeated_runs_agree() -> None:
    tree = scan(FIXTURES / "multi_file_hierarchy")

    assert parse_sources(tree) == parse_sources(tree)


def test_warnings_are_sorted_by_path_then_code(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "z.sv": "module z (",
            "a.sv": "module a (",
            "m.vhd": "entity e is",
        },
    )

    result = parse_sources(scan(tmp_path))
    keys = [w.sort_key for w in result.warnings]

    assert keys == sorted(keys)
    assert [w.path.name for w in result.warnings if w.path] == [
        "a.sv",
        "m.vhd",
        "z.sv",
    ]


def test_every_scanned_file_is_accounted_for(tmp_path: Path) -> None:
    """Files and warnings together cover everything Scan handed over."""
    build(
        tmp_path,
        {
            "ok.v": "module ok;\nendmodule\n",
            "bad.sv": "module bad (",
            "old.vhd": "entity e is",
        },
    )
    tree = scan(tmp_path)

    result = parse_sources(tree)
    seen = {f.path for f in result.files} | {w.path for w in result.warnings}

    assert seen == set(tree.files)


def test_an_empty_tree_parses_to_nothing(tmp_path: Path) -> None:
    assert parse_sources(scan(tmp_path)) == ParseResult()
