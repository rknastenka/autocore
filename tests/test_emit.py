"""Unit tests for Stage 4, Emit.

Everything here runs on hand-built models. `to_manifest` and `emit` are pure,
so no RTL needs to exist and the shape under test sits next to the assertion
about it. Running the real pipeline over the fixtures is `test_e2e.py`'s job.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autocore import __version__
from autocore.emit import emit, to_manifest, write_core
from autocore.models import CoreManifest, ProjectModel, ToolOption

ROOT = Path("/proj")

#: The tree most tests emit into. The model's absolute paths and the
#: `to_manifest` root have to agree for the relative paths to come out tidy.
DIR = ROOT / "dir"


def pm(
    order: tuple[str, ...] = (),
    top: str = "",
    include_files: tuple[str, ...] = (),
    tb_top: str = "",
    tb_order: tuple[str, ...] = (),
    tb_alternatives: tuple[str, ...] = (),
    root: Path = DIR,
) -> ProjectModel:
    """A hand-built `ProjectModel` carrying just what `to_manifest` reads."""
    return ProjectModel(
        files=(),
        compile_order=tuple(root / p for p in order),
        top=top,
        tb_top=tb_top,
        tb_top_alternatives=tb_alternatives,
        tb_compile_order=tuple(root / p for p in tb_order),
        include_files=frozenset(root / p for p in include_files),
    )


def rtl_of(manifest: CoreManifest):
    (fileset,) = manifest.filesets
    assert fileset.name == "rtl"
    return fileset


# assembly: VLNV


def test_the_default_vlnv_comes_from_the_directory_name() -> None:
    root = ROOT / "my_chip"
    manifest = to_manifest(pm(("alu.v",), top="alu", root=root), root)

    assert manifest.vlnv == "::my_chip:0.1.0"


def test_the_directory_name_is_sanitized_to_the_fusesoc_charset() -> None:
    manifest = to_manifest(pm(root=ROOT / "My Chip (v2)!"), ROOT / "My Chip (v2)!")

    assert manifest.vlnv == "::My_Chip_v2:0.1.0"


def test_a_hopeless_directory_name_falls_back_to_core() -> None:
    manifest = to_manifest(pm(root=ROOT / "@@@"), ROOT / "@@@")

    assert manifest.vlnv == "::core:0.1.0"


def test_name_and_library_overrides_are_taken_verbatim() -> None:
    manifest = to_manifest(pm(), DIR, name="alpha", library="mylib")

    assert manifest.vlnv == ":mylib:alpha:0.1.0"


# assembly: fileset structure


def test_files_follow_compile_order_relative_and_posix() -> None:
    manifest = to_manifest(pm(("rtl/core/leaf.v", "rtl/top.v"), top="top"), DIR)

    rtl = rtl_of(manifest)
    assert [entry.path for entry in rtl.files] == ["rtl/core/leaf.v", "rtl/top.v"]


def test_paths_are_relative_to_the_core_location_not_the_root() -> None:
    root = DIR
    manifest = to_manifest(
        pm(("rtl/top.v",), top="top", root=root), root, core_dir=root / "cores"
    )

    assert [entry.path for entry in rtl_of(manifest).files] == ["../rtl/top.v"]


def test_the_dominant_language_sets_the_fileset_file_type() -> None:
    manifest = to_manifest(pm(("a.sv", "b.sv", "c.v"), top="a"), DIR)

    rtl = rtl_of(manifest)
    assert rtl.file_type == "systemVerilogSource"
    # Assembly records every file's own type; rendering is what collapses them.
    assert [entry.file_type for entry in rtl.files] == [
        "systemVerilogSource",
        "systemVerilogSource",
        "verilogSource",
    ]


def test_a_dominance_tie_breaks_alphabetically() -> None:
    manifest = to_manifest(pm(("a.sv", "b.v"), top="a"), DIR)

    assert rtl_of(manifest).file_type == "systemVerilogSource"


def test_include_files_are_flagged_and_headers_map_to_their_language() -> None:
    manifest = to_manifest(
        pm(
            ("inc/defs.svh", "inc/legacy.vh", "top.sv"),
            top="top",
            include_files=("inc/defs.svh", "inc/legacy.vh"),
        ),
        DIR,
    )

    defs, legacy, top = rtl_of(manifest).files
    assert defs.is_include_file and legacy.is_include_file
    assert not top.is_include_file
    assert defs.file_type == "systemVerilogSource"
    assert legacy.file_type == "verilogSource"


def test_the_default_target_names_the_rtl_fileset_and_the_top() -> None:
    manifest = to_manifest(pm(("alu.v",), top="alu"), DIR)

    (target,) = manifest.targets
    assert target.name == "default"
    assert target.filesets == ("rtl",)
    assert target.toplevel == "alu"
    assert target.default_tool is None  # that is the sim target's business


# assembly: the tb fileset and the sim target


def with_tb(tb_alternatives: tuple[str, ...] = ()) -> ProjectModel:
    return pm(
        ("rtl/alu.v", "rtl/chip.v"),
        top="chip",
        tb_top="chip_tb",
        tb_order=("bench/scb.sv", "bench/chip_tb.sv"),
        tb_alternatives=tb_alternatives,
    )


def test_a_testbench_adds_a_tb_fileset_and_a_sim_target() -> None:
    manifest = to_manifest(with_tb(), DIR)

    rtl, tb = manifest.filesets
    assert rtl.name == "rtl" and tb.name == "tb"
    assert [entry.path for entry in tb.files] == ["bench/scb.sv", "bench/chip_tb.sv"]
    assert rtl.file_type == "verilogSource"
    assert tb.file_type == "systemVerilogSource"

    default, sim = manifest.targets
    assert default.filesets == ("rtl",) and default.toplevel == "chip"
    assert sim.name == "sim"
    assert sim.filesets == ("rtl", "tb")
    assert sim.toplevel == "chip_tb"
    assert sim.default_tool == "verilator"
    assert sim.tools == (ToolOption("verilator", "mode", "binary"),)
    assert sim.toplevel_comment is None  # one obvious bench: nothing to flag


def test_the_default_target_carries_no_tool_options() -> None:
    """A `default` target is synthesis-shaped and has no business naming a
    simulator's mode."""
    default, _sim = to_manifest(with_tb(), DIR).targets

    assert default.tools == ()
    assert default.default_tool is None


def test_the_sim_target_pins_verilator_to_binary_mode() -> None:
    """Without it edalize defaults Verilator to `cc`, which needs a C++ driver,
    so the sim target would fail to link for want of a `main()`."""
    text = emit(to_manifest(with_tb(), DIR))

    assert "    tools:\n      verilator:\n        mode: binary\n" in text


def test_no_testbench_means_no_tb_fileset_and_no_sim_target() -> None:
    manifest = to_manifest(pm(("alu.v",), top="alu"), DIR)

    assert [fileset.name for fileset in manifest.filesets] == ["rtl"]
    assert [target.name for target in manifest.targets] == ["default"]


def test_a_tb_top_without_tb_files_emits_no_sim_target() -> None:
    manifest = to_manifest(pm(("alu.v",), top="alu", tb_top="ghost_tb"), DIR)

    assert [target.name for target in manifest.targets] == ["default"]


def test_a_tb_only_tree_names_only_the_filesets_it_built() -> None:
    """The sim target must not name an rtl fileset nobody emitted."""
    manifest = to_manifest(pm(tb_top="chip_tb", tb_order=("bench/chip_tb.sv",)), DIR)

    assert [fileset.name for fileset in manifest.filesets] == ["tb"]
    default, sim = manifest.targets
    assert default.filesets == ()
    assert sim.filesets == ("tb",)


def test_the_sim_target_renders_in_full() -> None:
    text = emit(to_manifest(with_tb(), DIR))

    assert text.endswith(
        "  sim:\n"
        "    filesets:\n"
        "      - rtl\n"
        "      - tb\n"
        "    toplevel: chip_tb\n"
        "    default_tool: verilator\n"
        "    tools:\n"
        "      verilator:\n"
        "        mode: binary\n"
    )


# exactly one sim toplevel, and a comment when it was a guess


def test_several_bench_candidates_still_yield_exactly_one_sim_target() -> None:
    manifest = to_manifest(with_tb(tb_alternatives=("a_tb", "z_tb")), DIR)

    sim_targets = [target for target in manifest.targets if target.name == "sim"]
    assert len(sim_targets) == 1
    assert sim_targets[0].toplevel == "chip_tb"


def test_an_ambiguous_sim_toplevel_is_flagged_in_the_file_itself() -> None:
    """The stderr warning is long gone by the time anyone opens the `.core`."""
    text = emit(to_manifest(with_tb(tb_alternatives=("a_tb", "z_tb")), DIR))

    assert (
        "    # autocore: several files could have been the sim toplevel "
        "(also: a_tb, z_tb).\n"
        "    # Check this is the testbench you meant.\n"
        "    toplevel: chip_tb\n"
    ) in text


def test_an_unambiguous_sim_toplevel_gets_no_comment() -> None:
    text = emit(to_manifest(with_tb(), DIR))

    assert "#" not in text.split("targets:")[1]


def test_the_ambiguity_comment_is_deterministic() -> None:
    """Determinism reaches the comments too, not just the data."""
    """Determinism reaches the comments too, not just the data."""
    model = with_tb(tb_alternatives=("a_tb", "z_tb"))

    assert emit(to_manifest(model, DIR)) == emit(to_manifest(model, DIR))


def test_an_empty_model_yields_no_fileset_and_a_bare_target() -> None:
    manifest = to_manifest(pm(), DIR)

    assert manifest.filesets == ()
    (target,) = manifest.targets
    assert target.filesets == () and target.toplevel is None


# rendering


def test_the_header_is_exactly_capi2_then_the_generated_by_comment() -> None:
    text = emit(to_manifest(pm(("alu.v",), top="alu"), DIR))

    lines = text.splitlines()
    assert lines[0] == "CAPI=2:"
    assert lines[1] == (
        f"# Generated by autocore v{__version__}. Edit freely; "
        "autocore will not touch this file again."
    )
    assert lines[2] == ""
    assert lines[3] == "name: ::dir:0.1.0"
    assert text.endswith("\n")


def test_the_generated_by_line_has_its_exact_format() -> None:
    """The e2e suite normalizes the version out of its byte comparison, so this
    is the only place the full format is pinned down."""
    text = emit(to_manifest(pm(("alu.v",), top="alu"), DIR))

    line = text.splitlines()[1]
    assert re.fullmatch(
        r"# Generated by autocore v\d+\.\d+\.\d+\. Edit freely; "
        r"autocore will not touch this file again\.",
        line,
    )
    assert f"autocore v{__version__}." in line


def test_a_file_matching_the_dominant_type_renders_as_a_bare_path() -> None:
    text = emit(to_manifest(pm(("a.sv", "b.sv", "c.v"), top="a"), DIR))

    assert "- a.sv\n" in text
    assert "- c.v:\n          file_type: verilogSource\n" in text


def test_an_include_file_gets_its_flag_and_no_redundant_file_type() -> None:
    text = emit(
        to_manifest(
            pm(("inc/defs.svh", "top.sv"), top="top", include_files=("inc/defs.svh",)),
            DIR,
        )
    )

    assert "- inc/defs.svh:\n          is_include_file: true\n" in text
    assert text.count("file_type") == 1  # the fileset-level key only


def test_no_empty_structural_keys_anywhere() -> None:
    text = emit(to_manifest(pm(("alu.v",), top="alu"), DIR))

    for forbidden in ("depend", "[]", "{}", "include_dirs", "null"):
        assert forbidden not in text


def test_an_empty_model_emits_only_the_name() -> None:
    text = emit(to_manifest(pm(), DIR))

    assert text.splitlines()[-1] == "name: ::dir:0.1.0"
    assert "filesets" not in text and "targets" not in text


def test_rendering_is_repeatable() -> None:
    model = pm(
        ("inc/defs.svh", "a.sv", "c.v"), top="a", include_files=("inc/defs.svh",)
    )

    first = emit(to_manifest(model, DIR))
    second = emit(to_manifest(model, DIR))
    assert first == second


# the write site


def test_write_core_writes_utf8_with_fixed_newlines(tmp_path: Path) -> None:
    target = tmp_path / "out.core"

    write_core("CAPI=2:\n# λ\n", target)

    assert target.read_bytes() == "CAPI=2:\n# λ\n".encode()


def test_write_core_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "out.core"
    target.write_text("precious\n")

    with pytest.raises(FileExistsError, match="--force"):
        write_core("CAPI=2:\n", target)
    assert target.read_text() == "precious\n"


def test_write_core_overwrites_with_force(tmp_path: Path) -> None:
    target = tmp_path / "out.core"
    target.write_text("precious\n")

    write_core("CAPI=2:\n", target, force=True)

    assert target.read_text() == "CAPI=2:\n"
