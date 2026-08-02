"""Unit tests for Stage 3 - Resolve.

Two kinds of test share this file. The fixture tests run the real pipeline —
Scan then Parse then Resolve — over the on-disk trees, because Resolve's
contract is only meaningful against what the earlier stages actually produce.
The graph-shape tests (tops, duplicates, cycles, ties, include matching) build
`ScanResult` / `ParseResult` pairs by hand instead: `resolve` never touches
the filesystem, so the shape under test is visible right next to the
assertion about it, and no RTL needs to exist.
"""

from __future__ import annotations

from pathlib import Path

from autocore.models import (
    FileFacts,
    Lang,
    MultipleTops,
    ParseResult,
    ProjectModel,
    ScanResult,
    TbDirective,
    TbEvidence,
    TopCandidate,
    UnclearTbStatus,
)
from autocore.parse import parse_sources
from autocore.resolve import resolve
from autocore.scan import scan

FIXTURES = Path(__file__).parent / "fixtures"

ROOT = Path("/proj")

#: No evidence at all, and the strong-evidence branch, as shorthands below.
NONE_SEEN = TbEvidence()
STRONG = TbEvidence(has_finish_or_stop=True, empty_port_list=True)


def build(root: Path, layout: dict[str, str]) -> Path:
    """Materialise `{relative path: contents}` under `root`."""
    for path, contents in layout.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    return root


def run(root: Path, defines: tuple[str, ...] = ()) -> ProjectModel:
    """The real pipeline: Scan -> Parse -> Resolve."""
    tree = scan(root)
    return resolve(tree, parse_sources(tree, defines=defines))


def ff(
    path: str,
    *,
    declared: tuple[str, ...] = (),
    instantiated: tuple[str, ...] = (),
    imported: tuple[str, ...] = (),
    includes: tuple[str, ...] = (),
    evidence: TbEvidence = NONE_SEEN,
    directive: TbDirective | None = None,
    root: Path = ROOT,
) -> FileFacts:
    """A hand-built `FileFacts` under `root`."""
    return FileFacts(
        path=root / path,
        language=Lang.VERILOG,
        declared=frozenset(declared),
        instantiated=frozenset(instantiated),
        imported_pkgs=frozenset(imported),
        includes=frozenset(includes),
        tb_evidence=evidence,
        tb_directive=directive,
    )


def model_of(
    *facts: FileFacts,
    scanned: tuple[str, ...] = (),
    include_candidates: tuple[str, ...] = (),
    root: Path = ROOT,
    top: str | None = None,
    tb_globs: tuple[str, ...] | None = None,
    tb_overrides: dict[Path, TbDirective] | None = None,
) -> ProjectModel:
    """Resolve hand-built facts, as if Scan and Parse had produced them.

    `scanned` adds files Scan saw but Parse produced no facts for; include
    candidates are added to the scanned set automatically, as Scan would.
    """
    files = tuple(sorted(facts, key=lambda f: f.path.as_posix()))
    candidates = frozenset(root / c for c in include_candidates)
    paths = sorted(
        {f.path for f in files} | {root / s for s in scanned} | candidates,
        key=Path.as_posix,
    )
    tree = ScanResult(root=root, files=tuple(paths), include_candidates=candidates)
    return resolve(
        tree,
        ParseResult(files=files),
        top=top,
        tb_globs=tb_globs,
        tb_overrides=tb_overrides,
    )


def rel_set(paths: frozenset[Path], root: Path) -> tuple[str, ...]:
    return tuple(sorted(p.relative_to(root).as_posix() for p in paths))


def rel_order(paths: tuple[Path, ...], root: Path) -> tuple[str, ...]:
    return tuple(p.relative_to(root).as_posix() for p in paths)


def by_code(model: ProjectModel, code: str) -> list:
    return [w for w in model.warnings if w.code == code]


def tops(*candidates: tuple[str, int]) -> MultipleTops:
    """A `MultipleTops` from ``(name, closure size)`` pairs.

    The sizes are part of the assertion, not noise: they are what the
    top-detection fallback weighed and what the prompt shows, so a candidate
    list that stops carrying them stops being enough to build a prompt from.
    """
    return MultipleTops(
        candidates=tuple(TopCandidate(name, size) for name, size in candidates)
    )


# --------------------------------------------------------------------------
# fixtures through the real pipeline
# --------------------------------------------------------------------------


def test_single_module_fixture() -> None:
    model = run(FIXTURES / "single_module")

    assert model.top == "alu"
    assert rel_set(model.rtl, FIXTURES / "single_module") == ("alu.v",)
    assert rel_order(model.compile_order, FIXTURES / "single_module") == ("alu.v",)
    assert model.warnings == ()
    assert model.ambiguities == ()


def test_multi_file_hierarchy_fixture() -> None:
    root = FIXTURES / "multi_file_hierarchy"
    model = run(root)

    assert model.top == "top"
    assert model.warnings == ()
    assert rel_set(model.rtl, root) == (
        "include/defs.svh",
        "rtl/core/alu.v",
        "rtl/core/regfile.sv",
        "rtl/top.sv",
    )
    # Dependencies first, ties alphabetical: the header and both leaves are
    # all ready at once, so they come out in path order, then the top.
    assert rel_order(model.compile_order, root) == (
        "include/defs.svh",
        "rtl/core/alu.v",
        "rtl/core/regfile.sv",
        "rtl/top.sv",
    )
    assert rel_set(model.include_files, root) == ("include/defs.svh",)
    assert model.include_dirs == (root / "include",)
    assert model.external_refs == frozenset()


def test_external_prims_fixture() -> None:
    root = FIXTURES / "external_prims"
    model = run(root)

    assert model.top == "chip"
    assert model.external_refs == frozenset({"SB_PLL40_CORE"})
    (warning,) = by_code(model, "ExternalReference")
    assert warning.path == root / "rtl" / "chip.v"
    assert "SB_PLL40_CORE" in warning.message
    # External refs never cost a file: both sources stay in the closure.
    assert rel_set(model.rtl, root) == ("rtl/alu_core.v", "rtl/chip.v")
    assert by_code(model, "ExcludedFromRtl") == []
    assert model.ambiguities == ()


def test_multiple_tops_fixture_applies_the_d15_fallback() -> None:
    root = FIXTURES / "multiple_tops"
    model = run(root)

    # Neither candidate matches the directory name and both closures are two
    # files (each soc plus the shared alu), so the tie breaks alphabetically.
    assert model.top == "soc_a"
    assert model.ambiguities == (tops(("soc_a", 2), ("soc_b", 2)),)
    (warning,) = by_code(model, "MultipleTops")
    assert "soc_a" in warning.message and "soc_b" in warning.message

    # The loser falls outside the closure and is reported by name.
    assert rel_set(model.rtl, root) == ("rtl/common_alu.v", "rtl/soc_a.v")
    (excluded,) = by_code(model, "ExcludedFromRtl")
    assert excluded.details == ("rtl/soc_b.v",)
    # Excluded from rtl, not from the evidence: its facts are still there.
    assert any(f.path.name == "soc_b.v" for f in model.files)


def test_the_closure_follows_the_active_define(tmp_path: Path) -> None:
    """The leaf the define switches off falls outside the closure.

    An `ifdef`-disabled module is referenced by nobody, which makes it a top
    *candidate* — MultipleTops fires here by construction, and the
    closure-size fallback is what keeps the intended top on top: `a_top`
    drags two files behind it, the orphaned leaf only itself.
    """
    build(
        tmp_path,
        {
            "top.sv": (
                "module a_top;\n"
                "`ifdef USE_B\n"
                "  leaf_b u_leaf ();\n"
                "`else\n"
                "  leaf_a u_leaf ();\n"
                "`endif\n"
                "endmodule\n"
            ),
            "leaf_a.v": "module leaf_a;\nendmodule\n",
            "leaf_b.v": "module leaf_b;\nendmodule\n",
        },
    )

    without = run(tmp_path)
    with_b = run(tmp_path, defines=("USE_B",))

    assert without.top == "a_top" and with_b.top == "a_top"
    assert rel_set(without.rtl, tmp_path) == ("leaf_a.v", "top.sv")
    assert rel_set(with_b.rtl, tmp_path) == ("leaf_b.v", "top.sv")
    assert "leaf_b.v" in by_code(without, "ExcludedFromRtl")[0].details
    assert "leaf_a.v" in by_code(with_b, "ExcludedFromRtl")[0].details


def test_ifdef_heavy_candidates_depend_on_the_define() -> None:
    """The switched-off leaf in ifdef_heavy is a candidate, per the rule —
    but its closure is one file against `top`'s four, so the closure-size
    fallback keeps `top` on top in both parses."""
    root = FIXTURES / "ifdef_heavy"

    without = run(root)
    with_mul = run(root, defines=("USE_MUL",))

    assert without.ambiguities == (tops(("mul", 1), ("top", 4)),)
    assert with_mul.ambiguities == (tops(("shifter", 1), ("top", 4)),)
    assert without.top == "top"
    assert with_mul.top == "top"
    assert "closure" in by_code(without, "MultipleTops")[0].message


def test_with_testbench_fixture_exercises_all_of_d16() -> None:
    """Every classification branch decides one file in this tree, and the two magic
    comments pull in opposite directions (see the fixture's README)."""
    root = FIXTURES / "with_testbench"
    model = run(root)

    assert model.warnings == ()
    assert model.ambiguities == ()
    assert model.top == "chip"
    assert model.tb_top == "chip_tb"
    assert rel_set(model.testbenches, root) == (
        "bench/chip_tb.sv",  # filename rule
        "bench/clkgen.sv",  # // autocore: tb
        "bench/scoreboard.sv",  # strong evidence alone
    )
    # `rtl/self_test.v` matches `*_test.*` and is RTL anyway: the comment wins.
    assert rel_set(model.rtl, root) == ("rtl/alu.v", "rtl/chip.v", "rtl/self_test.v")
    # `chip_tb` instantiates `chip`, so its closure covers all of rtl; the tb
    # fileset is what is left after subtracting it.
    assert rel_order(model.tb_compile_order, root) == (
        "bench/clkgen.sv",
        "bench/scoreboard.sv",
        "bench/chip_tb.sv",
    )


def test_the_tb_glob_flag_reaches_the_fixture_through_resolve() -> None:
    root = FIXTURES / "with_testbench"
    tree = scan(root)
    model = resolve(tree, parse_sources(tree), tb_globs=("*_nothing.*",))

    # `chip_tb.sv` loses the filename rule but keeps its strong evidence;
    # the two magic comments are untouched either way.
    assert rel_set(model.testbenches, root) == (
        "bench/chip_tb.sv",
        "bench/clkgen.sv",
        "bench/scoreboard.sv",
    )


def test_broken_file_fixture_keeps_its_parse_warning() -> None:
    root = FIXTURES / "broken_file"
    model = run(root)

    assert model.top == "good"
    assert rel_set(model.rtl, root) == ("rtl/good.v",)
    # The parse failure is carried through, the unparseable file is named as
    # excluded, and good.v's dangling instantiation is an external ref.
    assert len(by_code(model, "ParseFailed")) == 1
    assert "rtl/broken.sv" in by_code(model, "ExcludedFromRtl")[0].details
    assert model.external_refs == frozenset({"counter"})


# --------------------------------------------------------------------------
# step 1 - symbol table and duplicates
# --------------------------------------------------------------------------


def test_duplicate_declaration_first_sorted_path_wins() -> None:
    model = model_of(
        ff("one/alu.v", declared=("alu",)),
        ff("two/alu.v", declared=("alu",)),
        ff("top.v", declared=("top",), instantiated=("alu",)),
    )

    (warning,) = by_code(model, "DuplicateDeclaration")
    assert warning.path == ROOT / "two/alu.v"
    assert "one/alu.v" in warning.message
    # The winner carries the edge, so the loser falls outside the closure.
    assert rel_set(model.rtl, ROOT) == ("one/alu.v", "top.v")
    assert "two/alu.v" in by_code(model, "ExcludedFromRtl")[0].details


# --------------------------------------------------------------------------
# step 2 - edges and external refs
# --------------------------------------------------------------------------


def test_an_imported_package_is_a_dependency_not_a_top() -> None:
    model = model_of(
        ff("pkg.sv", declared=("p_pkg",)),
        ff("top.sv", declared=("top",), imported=("p_pkg",)),
    )

    assert model.top == "top"
    assert model.ambiguities == ()
    assert rel_order(model.compile_order, ROOT) == ("pkg.sv", "top.sv")


def test_an_undeclared_import_is_an_external_ref_too() -> None:
    model = model_of(ff("top.sv", declared=("top",), imported=("ghost_pkg",)))

    assert model.external_refs == frozenset({"ghost_pkg"})
    (warning,) = by_code(model, "ExternalReference")
    assert warning.path == ROOT / "top.sv"


# --------------------------------------------------------------------------
# step 3 - testbench classification, in full
# --------------------------------------------------------------------------


def test_tb_filenames_are_classified_before_top_detection() -> None:
    """Without classification `tb_chip` would be the sole top; with it, a
    testbench neither becomes a candidate nor disqualifies one."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("tb_chip.sv", declared=("tb_chip",), instantiated=("chip",)),
    )

    assert model.top == "chip"
    assert rel_set(model.testbenches, ROOT) == ("tb_chip.sv",)
    assert rel_set(model.rtl, ROOT) == ("chip.v",)
    # A testbench outside the rtl closure is where it belongs — not "excluded".
    assert by_code(model, "ExcludedFromRtl") == []


def test_all_four_filename_patterns_and_their_case_insensitivity() -> None:
    """`testbench.*` is what picorv32 calls its six tool-specific
    benches, and the evidence half
    cannot reach them because they do not parse."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("chip_tb.v", declared=("chip_tb",), instantiated=("chip",)),
        ff("tb_chip.sv", declared=("tb_chip",), instantiated=("chip",)),
        ff("chip_test.sv", declared=("chip_test",), instantiated=("chip",)),
        ff("TB_Chip2.SV", declared=("tb_chip2",), instantiated=("chip",)),
        ff("testbench.v", declared=("testbench",), instantiated=("chip",)),
    )

    assert rel_set(model.testbenches, ROOT) == (
        "TB_Chip2.SV",
        "chip_tb.v",
        "chip_test.sv",
        "tb_chip.sv",
        "testbench.v",
    )
    assert model.top == "chip"


def test_a_file_scan_saw_but_parse_could_not_read_is_still_classified() -> None:
    """The filename branch is the only rule that reaches an unparseable
    testbench, and picorv32's benches are exactly that case."""
    model = model_of(ff("chip.v", declared=("chip",)), scanned=("tb_broken.sv",))

    assert rel_set(model.testbenches, ROOT) == ("tb_broken.sv",)
    assert by_code(model, "ExcludedFromRtl") == []


def test_strong_evidence_classifies_a_file_no_pattern_matches() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff(
            "harness.sv",
            declared=("harness",),
            instantiated=("chip",),
            evidence=STRONG,
        ),
    )

    assert rel_set(model.testbenches, ROOT) == ("harness.sv",)
    assert model.top == "chip"


def test_half_the_evidence_classifies_nothing_and_is_reported_as_unclear() -> None:
    """Partial evidence trips neither classification branch, so it becomes data —
    an `UnclearTbStatus`. Nothing here prompts; that gate is `interact.py`'s.

    The warning beside the ambiguity is what the non-prompting paths owe the
    user: `--yes` and a pipe both apply the documented default silently
    otherwise."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff(
            "monitor.sv",
            declared=("monitor",),
            instantiated=("chip",),
            evidence=TbEvidence(has_finish_or_stop=True),
        ),
    )

    assert model.testbenches == frozenset()
    assert model.ambiguities[0] == UnclearTbStatus(path=ROOT / "monitor.sv")
    (warning,) = by_code(model, "UnclearTbStatus")
    assert warning.path == ROOT / "monitor.sv"
    assert "treated as RTL" in warning.message
    # Classified as RTL, so it really does disqualify `chip`.
    assert model.top == "monitor"


# --------------------------------------------------------------------------
# step 3 - tb_overrides: an answered UnclearTbStatus, fed back in
# --------------------------------------------------------------------------


def unclear(path: str, name: str) -> FileFacts:
    """A file with exactly enough evidence to be asked about, and no more."""
    return ff(
        path,
        declared=(name,),
        instantiated=("chip",),
        evidence=TbEvidence(has_finish_or_stop=True),
    )


def test_an_override_can_force_a_file_to_be_a_testbench() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        unclear("monitor.sv", "monitor"),
        tb_overrides={ROOT / "monitor.sv": TbDirective.TB},
    )

    assert rel_set(model.testbenches, ROOT) == ("monitor.sv",)
    assert model.tb_top == "monitor"
    # Answered, so neither the ambiguity nor the warning survives the re-run.
    assert model.ambiguities == ()
    assert by_code(model, "UnclearTbStatus") == []
    assert model.top == "chip"


def test_an_override_can_force_a_file_to_be_rtl() -> None:
    """The same shape as the default, but chosen rather than assumed — which
    is the whole difference the warning is there to mark."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        unclear("monitor.sv", "monitor"),
        tb_overrides={ROOT / "monitor.sv": TbDirective.RTL},
    )

    assert model.testbenches == frozenset()
    assert model.ambiguities == ()
    assert by_code(model, "UnclearTbStatus") == []
    assert model.top == "monitor"


def test_an_override_only_touches_the_path_it_names() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        unclear("a_monitor.sv", "a_monitor"),
        unclear("z_monitor.sv", "z_monitor"),
        tb_overrides={ROOT / "a_monitor.sv": TbDirective.TB},
    )

    assert rel_set(model.testbenches, ROOT) == ("a_monitor.sv",)
    assert model.ambiguities == (UnclearTbStatus(path=ROOT / "z_monitor.sv"),)


def test_an_override_outranks_a_magic_comment() -> None:
    """They cannot collide in practice — a file carrying a comment is never
    unclear, so it is never asked about — but the precedence has to be
    decided rather than accidental: the newer statement of intent wins."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("clkgen.v", declared=("clkgen",), directive=TbDirective.TB),
        tb_overrides={ROOT / "clkgen.v": TbDirective.RTL},
    )

    assert model.testbenches == frozenset()


def test_unclear_statuses_come_out_in_path_order() -> None:
    partial = TbEvidence(initial_heavy=True)
    model = model_of(
        ff("z.v", declared=("z",), evidence=partial),
        ff("a.v", declared=("a",), evidence=partial),
        ff("m.v", declared=("m",), evidence=partial),
    )

    unclear = [a for a in model.ambiguities if isinstance(a, UnclearTbStatus)]
    assert [a.path.name for a in unclear] == ["a.v", "m.v", "z.v"]


def test_strong_evidence_is_never_unclear() -> None:
    model = model_of(ff("chip.v", declared=("chip",)), ff("h.sv", evidence=STRONG))

    assert model.ambiguities == ()


# --------------------------------------------------------------------------
# step 3 - magic comments override both directions
# --------------------------------------------------------------------------


def test_the_tb_comment_beats_a_name_and_evidence_that_say_nothing() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("clkgen.v", declared=("clkgen",), directive=TbDirective.TB),
    )

    assert rel_set(model.testbenches, ROOT) == ("clkgen.v",)
    assert model.top == "chip"


def test_the_rtl_comment_beats_the_filename_rule() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",), instantiated=("self_test",)),
        ff("self_test.v", declared=("self_test",), directive=TbDirective.RTL),
    )

    assert model.testbenches == frozenset()
    assert model.top == "chip"
    assert rel_set(model.rtl, ROOT) == ("chip.v", "self_test.v")


def test_the_rtl_comment_beats_strong_evidence_too() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",), instantiated=("bist",)),
        ff("bist.v", declared=("bist",), evidence=STRONG, directive=TbDirective.RTL),
    )

    assert model.testbenches == frozenset()
    assert rel_set(model.rtl, ROOT) == ("bist.v", "chip.v")


def test_a_file_carrying_both_comments_warns_and_falls_through() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff(
            "chip_tb.v",
            declared=("chip_tb",),
            instantiated=("chip",),
            directive=TbDirective.CONFLICTING,
        ),
    )

    (warning,) = by_code(model, "ConflictingTbDirective")
    assert warning.path == ROOT / "chip_tb.v"
    # Fell through to the filename rule, which still says testbench.
    assert rel_set(model.testbenches, ROOT) == ("chip_tb.v",)
    assert model.top == "chip"


# --------------------------------------------------------------------------
# step 3 - --tb-glob replaces the filename patterns
# --------------------------------------------------------------------------


def test_tb_glob_replaces_the_defaults_rather_than_extending_them() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("chip_tb.v", declared=("chip_tb",), instantiated=("chip",)),
        ff("sim_chip.v", declared=("sim_chip",), instantiated=("chip",)),
        tb_globs=("sim_*.*",),
    )

    # The built-in `*_tb.*` no longer applies at all.
    assert rel_set(model.testbenches, ROOT) == ("sim_chip.v",)


def test_tb_glob_is_case_insensitive_on_both_sides() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("SIM_Chip.V", declared=("sim_chip",), instantiated=("chip",)),
        tb_globs=("SIM_*.V",),
    )

    assert rel_set(model.testbenches, ROOT) == ("SIM_Chip.V",)


def test_tb_glob_never_disables_the_evidence_branch_or_the_comments() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("harness.v", declared=("harness",), instantiated=("chip",), evidence=STRONG),
        ff("clkgen.v", declared=("clkgen",), directive=TbDirective.TB),
        tb_globs=("nothing_matches_this",),
    )

    assert rel_set(model.testbenches, ROOT) == ("clkgen.v", "harness.v")


# --------------------------------------------------------------------------
# the testbench top and the tb fileset
# --------------------------------------------------------------------------


def tb_order(model: ProjectModel, root: Path = ROOT) -> tuple[str, ...]:
    return rel_order(model.tb_compile_order, root)


def test_the_tb_top_is_the_testbench_nobody_else_instantiates() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("chip_tb.v", declared=("chip_tb",), instantiated=("chip", "stim")),
        ff("stim_tb.v", declared=("stim",)),
    )

    assert model.tb_top == "chip_tb"
    assert by_code(model, "MultipleTestbenchTops") == []
    # One obvious bench: nothing for Emit to flag in the generated file.
    assert model.tb_top_alternatives == ()


def test_a_bench_instantiating_the_rtl_top_stays_a_candidate() -> None:
    """Only testbench-to-testbench references disqualify: instantiating the
    DUT is what a bench is *for*."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("chip_tb.v", declared=("chip_tb",), instantiated=("chip",)),
    )

    assert model.tb_top == "chip_tb"


def test_the_tb_fileset_is_the_closure_minus_whatever_rtl_already_holds() -> None:
    """`chip_tb`'s closure runs right through the rtl top and out the other
    side, but the tb fileset must
    not carry the rtl closure a second time."""
    model = model_of(
        ff("rtl/chip.v", declared=("chip",), instantiated=("alu",)),
        ff("rtl/alu.v", declared=("alu",)),
        ff("bench/chip_tb.v", declared=("chip_tb",), instantiated=("chip", "scb")),
        ff("bench/scb.v", declared=("scb",), evidence=STRONG),
    )

    assert rel_set(model.rtl, ROOT) == ("rtl/alu.v", "rtl/chip.v")
    assert tb_order(model) == ("bench/scb.v", "bench/chip_tb.v")
    assert by_code(model, "ExcludedFromRtl") == []


def test_a_tree_with_no_testbench_has_no_sim_target_at_all() -> None:
    model = model_of(ff("chip.v", declared=("chip",)))

    assert model.testbenches == frozenset()
    assert model.tb_top == ""
    assert model.tb_compile_order == ()


def test_several_testbench_tops_reuse_the_d15_fallback_with_a_warning() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("a_tb.v", declared=("a_tb",)),
        ff("z_tb.v", declared=("z_tb",), instantiated=("chip", "helper")),
        ff("helper_tb.v", declared=("helper",)),
    )

    # `a_tb` would win alphabetically; `z_tb` drags three more files behind it.
    assert model.tb_top == "z_tb"
    (warning,) = by_code(model, "MultipleTestbenchTops")
    assert "a_tb" in warning.message and "closure" in warning.message
    # Not one of the three ambiguity types — and not a prompt either: the
    # losers travel as alternatives, for Emit to flag in the file.
    assert model.ambiguities == ()
    # The losing *candidates*, not every bench: `helper` was never in the
    # running, because `z_tb` instantiates it.
    assert model.tb_top_alternatives == ("a_tb",)


def test_a_testbench_outside_the_tb_top_closure_is_dropped_with_a_warning() -> None:
    """It is still classified — which is what keeps it out of rtl — it just
    has no place in a sim target built from one toplevel. Being defensible
    does not make it silent: the file is in neither fileset, so it is named."""
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("a_tb.v", declared=("a_tb",), instantiated=("chip", "helper")),
        ff("helper_tb.v", declared=("helper",)),
        ff("z_tb.v", declared=("z_tb",)),
    )

    assert model.tb_top == "a_tb"
    assert tb_order(model) == ("helper_tb.v", "a_tb.v")
    assert "z_tb.v" in rel_set(model.testbenches, ROOT)
    (warning,) = by_code(model, "ExcludedFromTb")
    assert warning.details == ("z_tb.v",)
    assert "'a_tb'" in warning.message
    # It was never a candidate for rtl, so it is not "excluded" from there.
    assert by_code(model, "ExcludedFromRtl") == []


def test_a_testbench_inside_the_closure_is_not_reported_as_dropped() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("chip_tb.v", declared=("chip_tb",), instantiated=("chip", "scb")),
        ff("scb_tb.v", declared=("scb",)),
    )

    assert by_code(model, "ExcludedFromTb") == []


def test_mutually_instantiating_testbenches_fall_back_alphabetically() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff("a_tb.v", declared=("a_tb",), instantiated=("b_tb",)),
        ff("b_tb.v", declared=("b_tb",), instantiated=("a_tb",)),
    )

    assert model.tb_top == "a_tb"
    (warning,) = by_code(model, "NoTestbenchTop")
    assert "'a_tb'" in warning.message
    # A knot of benches is as much a guess as several free candidates are.
    assert model.tb_top_alternatives == ("b_tb",)


def test_a_testbench_that_declares_nothing_yields_no_sim_target() -> None:
    model = model_of(ff("chip.v", declared=("chip",)), ff("chip_tb.v"))

    assert rel_set(model.testbenches, ROOT) == ("chip_tb.v",)
    assert model.tb_top == ""
    assert model.tb_compile_order == ()
    # No sim target to belong to, so it is dropped — and said so. The loudest
    # version of the case above: every classified bench falls out at once.
    (warning,) = by_code(model, "ExcludedFromTb")
    assert "no sim toplevel" in warning.message
    assert warning.details == ("chip_tb.v",)


def test_a_header_the_bench_alone_includes_is_flagged_in_the_tb_fileset() -> None:
    model = model_of(
        ff("chip.v", declared=("chip",)),
        ff(
            "chip_tb.v",
            declared=("chip_tb",),
            instantiated=("chip",),
            includes=("t.svh",),
        ),
        ff("bench/t.svh"),
        include_candidates=("bench/t.svh",),
    )

    assert tb_order(model) == ("bench/t.svh", "chip_tb.v")
    assert rel_set(model.include_files, ROOT) == ("bench/t.svh",)
    assert model.include_dirs == (ROOT / "bench",)


# --------------------------------------------------------------------------
# step 4 - top detection and its fallback
# --------------------------------------------------------------------------


def test_d15_prefers_the_directory_name_match() -> None:
    root = Path("/proj/chip_b")
    model = model_of(
        ff("a.v", declared=("chip_a",), root=root),
        ff("b.v", declared=("chip_b",), root=root),
        root=root,
    )

    # Alphabetical would say chip_a; the directory name says chip_b.
    assert model.top == "chip_b"
    assert model.ambiguities == (tops(("chip_a", 1), ("chip_b", 1)),)
    assert "directory" in by_code(model, "MultipleTops")[0].message


def test_d15_directory_match_is_sanitized() -> None:
    root = Path("/proj/My-Chip")
    model = model_of(
        ff("a.v", declared=("alpha_top",), root=root),
        ff("m.v", declared=("my_chip",), root=root),
        root=root,
    )

    assert model.top == "my_chip"


def test_d15_prefers_the_largest_closure_over_the_alphabet() -> None:
    model = model_of(
        ff("a.v", declared=("a_orphan",)),
        ff("z.v", declared=("z_top",), instantiated=("leaf",)),
        ff("leaf.v", declared=("leaf",)),
    )

    # Alphabetical would say a_orphan; z_top's two-file closure outweighs it.
    assert model.top == "z_top"
    assert "closure" in by_code(model, "MultipleTops")[0].message
    assert rel_set(model.rtl, ROOT) == ("leaf.v", "z.v")


def test_d15_directory_name_still_beats_a_larger_closure() -> None:
    root = Path("/proj/small")
    model = model_of(
        ff("small.v", declared=("small",), root=root),
        ff("big.v", declared=("big_top",), instantiated=("leaf",), root=root),
        ff("leaf.v", declared=("leaf",), root=root),
        root=root,
    )

    assert model.top == "small"
    assert "directory" in by_code(model, "MultipleTops")[0].message


def test_d15_equal_closures_tie_break_alphabetically() -> None:
    model = model_of(
        ff("m.v", declared=("m_top",), instantiated=("m_leaf",)),
        ff("m_leaf.v", declared=("m_leaf",)),
        ff("z.v", declared=("z_top",), instantiated=("z_leaf",)),
        ff("z_leaf.v", declared=("z_leaf",)),
    )

    assert model.top == "m_top"
    assert "alphabetically" in by_code(model, "MultipleTops")[0].message


def test_a_forced_top_skips_detection_and_its_ambiguity() -> None:
    """The --top escape hatch: resolve takes the caller's word, so the loser
    of a would-be MultipleTops simply falls out of the closure."""
    model = model_of(
        ff("a.v", declared=("a_top",)),
        ff("b.v", declared=("b_top",), instantiated=("leaf",)),
        ff("leaf.v", declared=("leaf",)),
        top="a_top",
    )

    assert model.top == "a_top"
    assert model.ambiguities == ()
    assert by_code(model, "MultipleTops") == []
    assert rel_set(model.rtl, ROOT) == ("a.v",)
    (excluded,) = by_code(model, "ExcludedFromRtl")
    assert excluded.details == ("b.v", "leaf.v")


def test_zero_candidates_fall_back_alphabetically_with_a_warning() -> None:
    model = model_of(
        ff("a.v", declared=("a",), instantiated=("b",)),
        ff("b.v", declared=("b",), instantiated=("a",)),
    )

    assert model.top == "a"
    (warning,) = by_code(model, "NoTopCandidate")
    assert "'a'" in warning.message
    assert model.ambiguities == ()


def test_intra_file_instantiation_does_not_disqualify() -> None:
    """File granularity, deliberately: this is how picorv32 keeps `picorv32`
    on the candidate list next to the wrappers that instantiate it from the
    same file."""
    model = model_of(
        ff("core.v", declared=("core", "core_axi"), instantiated=("core",)),
    )

    assert model.ambiguities == (tops(("core", 1), ("core_axi", 1)),)
    # Both candidates live in the same file, so their closures are identical
    # and the tie breaks alphabetically; /proj matches neither by name.
    assert model.top == "core"


def test_a_recursive_module_stays_a_candidate() -> None:
    model = model_of(ff("r.v", declared=("r",), instantiated=("r",)))

    assert model.top == "r"
    assert model.warnings == ()
    assert by_code(model, "CircularDependency") == []


# --------------------------------------------------------------------------
# the rtl closure
# --------------------------------------------------------------------------


def test_files_outside_the_closure_are_named_in_one_warning() -> None:
    model = model_of(
        ff("top.v", declared=("top",), instantiated=("used",)),
        ff("used.v", declared=("used",)),
        ff("z_unit.v", declared=("z_unit",), instantiated=("z_leaf",)),
        ff("z_leaf.v", declared=("z_leaf",)),
        scanned=("never_parsed.v",),
    )

    # The disconnected z_unit is a candidate too; its closure ties with
    # top's at two files, the tie breaks alphabetically in top's favour,
    # and the whole island is excluded.
    assert model.top == "top"
    assert rel_set(model.rtl, ROOT) == ("top.v", "used.v")
    (warning,) = by_code(model, "ExcludedFromRtl")
    # The count is the message; the names are the details, so that a tree with
    # 18 of them (picorv32) still reads as one line on stderr.
    assert warning.message.startswith("3 file(s) not reachable")
    assert warning.details == ("never_parsed.v", "z_leaf.v", "z_unit.v")


def test_an_unused_header_is_excluded_and_named() -> None:
    model = model_of(
        ff("top.v", declared=("top",)),
        ff("hdr/unused.svh"),
        include_candidates=("hdr/unused.svh",),
    )

    assert model.include_files == frozenset()
    assert model.include_dirs == ()
    assert by_code(model, "ExcludedFromRtl")[0].details == ("hdr/unused.svh",)


# --------------------------------------------------------------------------
# step 5 - compile order
# --------------------------------------------------------------------------


def test_dependencies_come_before_dependents_ties_alphabetical() -> None:
    model = model_of(
        ff("a_top.v", declared=("a_top",), instantiated=("z_leaf", "m_leaf")),
        ff("z.v", declared=("z_leaf",)),
        ff("m.v", declared=("m_leaf",)),
    )

    assert rel_order(model.compile_order, ROOT) == ("m.v", "z.v", "a_top.v")
    assert frozenset(model.compile_order) == model.rtl


def test_a_freed_file_can_overtake_an_already_ready_one() -> None:
    """One node at a time: emitting `a.v` unblocks `b.v`, which sorts before
    the always-ready `z.v`."""
    model = model_of(
        ff("top.v", declared=("top",), instantiated=("b", "z")),
        ff("a.v", declared=("a",)),
        ff("b.v", declared=("b",), instantiated=("a",)),
        ff("z.v", declared=("z",)),
    )

    order = rel_order(model.compile_order, ROOT)
    assert order == ("a.v", "b.v", "z.v", "top.v")


def test_a_cycle_breaks_at_the_alphabetically_last_edge() -> None:
    model = model_of(
        ff("a.v", declared=("a",), instantiated=("b",)),
        ff("b.v", declared=("b",), instantiated=("a",)),
    )

    # Edges sort as (a.v, b.v) < (b.v, a.v); the last one is dropped, so b.v
    # compiles first and the warning sits on the file that kept its edge cut.
    assert rel_order(model.compile_order, ROOT) == ("b.v", "a.v")
    (warning,) = by_code(model, "CircularDependency")
    assert warning.path == ROOT / "b.v"
    assert "a.v" in warning.message


def test_two_independent_cycles_cost_two_warnings() -> None:
    model = model_of(
        ff("top.v", declared=("top",), instantiated=("c", "e")),
        ff("c.v", declared=("c",), instantiated=("d",)),
        ff("d.v", declared=("d",), instantiated=("c",)),
        ff("e.v", declared=("e",), instantiated=("f",)),
        ff("f.v", declared=("f",), instantiated=("e",)),
    )

    assert len(by_code(model, "CircularDependency")) == 2
    order = rel_order(model.compile_order, ROOT)
    assert order.index("top.v") == len(order) - 1


# --------------------------------------------------------------------------
# step 6 - include resolution
# --------------------------------------------------------------------------


def test_includes_match_by_basename_wherever_the_header_lives() -> None:
    model = model_of(
        ff("rtl/top.v", declared=("top",), includes=("inc/defs.svh",)),
        include_candidates=("hdr/defs.svh",),
    )

    assert rel_set(model.include_files, ROOT) == ("hdr/defs.svh",)
    assert model.include_dirs == (ROOT / "hdr",)
    assert by_code(model, "UnresolvedInclude") == []
    # The matched header joined the closure over the include edge alone, and
    # a header without facts still sorts ahead of its includer.
    assert rel_order(model.compile_order, ROOT) == ("hdr/defs.svh", "rtl/top.v")


def test_a_basename_collision_matches_every_candidate() -> None:
    model = model_of(
        ff("top.v", declared=("top",), includes=("defs.svh",)),
        include_candidates=("a/defs.svh", "b/defs.svh"),
    )

    assert rel_set(model.include_files, ROOT) == ("a/defs.svh", "b/defs.svh")
    assert model.include_dirs == (ROOT / "a", ROOT / "b")


def test_include_dirs_are_sorted_and_deduped() -> None:
    model = model_of(
        ff("top.v", declared=("top",), includes=("z.svh", "a.svh", "b.svh")),
        include_candidates=("hdr/a.svh", "hdr/z.svh", "aaa/b.svh"),
    )

    assert model.include_dirs == (ROOT / "aaa", ROOT / "hdr")


def test_an_unresolved_include_warns_and_does_not_vanish() -> None:
    model = model_of(ff("top.v", declared=("top",), includes=("nowhere.svh",)))

    (warning,) = by_code(model, "UnresolvedInclude")
    assert warning.path == ROOT / "top.v"
    assert "nowhere.svh" in warning.message
    assert model.include_files == frozenset()


def test_a_module_declaring_header_is_demoted_to_a_source() -> None:
    model = model_of(
        ff("prims.vh", declared=("prim",)),
        ff("top.v", declared=("top",), instantiated=("prim",)),
        include_candidates=("prims.vh",),
    )

    # It reaches rtl over the instantiation edge like any source file, and
    # contributes nothing to include_files or include_dirs.
    assert rel_set(model.rtl, ROOT) == ("prims.vh", "top.v")
    assert model.include_files == frozenset()
    assert model.include_dirs == ()
    assert model.top == "top"


def test_including_a_demoted_header_is_reported_not_resolved() -> None:
    """The strict reading of the demotion rule: a header that declares modules
    left the candidate pool, so an `include` naming it no longer matches and
    must be reported rather than silently dropped. The file still reaches the
    closure — over the instantiation edge, like the source file it now is."""
    model = model_of(
        ff("prims.vh", declared=("prim",)),
        ff("top.v", declared=("top",), instantiated=("prim",), includes=("prims.vh",)),
        include_candidates=("prims.vh",),
    )

    (warning,) = by_code(model, "UnresolvedInclude")
    assert "prims.vh" in warning.message
    assert rel_set(model.rtl, ROOT) == ("prims.vh", "top.v")
    assert model.include_files == frozenset()


# --------------------------------------------------------------------------
# edges of the input space
# --------------------------------------------------------------------------


def test_an_empty_tree_resolves_to_an_empty_model() -> None:
    model = model_of()

    assert model.top == ""
    assert model.rtl == frozenset()
    assert model.compile_order == ()
    (warning,) = model.warnings
    assert warning.code == "NoTopCandidate"
    assert model.ambiguities == ()


# --------------------------------------------------------------------------
# ordering and determinism
# --------------------------------------------------------------------------


def test_warnings_are_sorted_by_path_then_code() -> None:
    model = model_of(
        ff("z.v", declared=("z",), instantiated=("ghost",), includes=("no.svh",)),
        ff("a.v", declared=("a",), instantiated=("other_ghost",)),
    )

    keys = [w.sort_key for w in model.warnings]
    assert keys == sorted(keys)


def test_repeated_resolves_agree() -> None:
    for fixture in ("multi_file_hierarchy", "multiple_tops", "external_prims"):
        assert run(FIXTURES / fixture) == run(FIXTURES / fixture)
