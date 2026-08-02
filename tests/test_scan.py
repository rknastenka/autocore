"""Unit tests for Stage 1 - Scan.

The two on-disk fixtures stay realistic RTL trees, because the golden-file
tests reuse them for comparison. Everything hostile — ``build/`` directories, symlink
loops, ``.gitignore`` files that would hide their own test data from git — is
built in ``tmp_path`` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autocore.models import ScanResult
from autocore.scan import ALWAYS_SKIP_DIRS, scan

FIXTURES = Path(__file__).parent / "fixtures"


def rel(result: ScanResult) -> tuple[str, ...]:
    """The scan result as paths relative to its root, POSIX-style."""
    return tuple(p.relative_to(result.root).as_posix() for p in result.files)


def rel_of(paths: frozenset[Path], root: Path) -> tuple[str, ...]:
    """The same, for the unordered ``include_candidates`` set."""
    return tuple(sorted(p.relative_to(root).as_posix() for p in paths))


def build(root: Path, layout: dict[str, str]) -> Path:
    """Materialise ``{relative path: contents}`` under ``root``."""
    for path, contents in layout.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    return root


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def test_single_module_fixture() -> None:
    result = scan(FIXTURES / "single_module")

    assert rel(result) == ("alu.v",)
    assert result.include_candidates == frozenset()


def test_multi_file_hierarchy_fixture() -> None:
    result = scan(FIXTURES / "multi_file_hierarchy")

    # Sorted by relative POSIX path: "include/..." < "rtl/core/..." < "rtl/top".
    assert rel(result) == (
        "include/defs.svh",
        "rtl/core/alu.v",
        "rtl/core/regfile.sv",
        "rtl/top.sv",
    )
    # README.md is in that tree on purpose and must not appear.
    assert not any(p.suffix == ".md" for p in result.files)


def test_header_is_the_only_include_candidate() -> None:
    result = scan(FIXTURES / "multi_file_hierarchy")

    assert rel_of(result.include_candidates, result.root) == ("include/defs.svh",)
    assert result.include_candidates <= set(result.files)


# --------------------------------------------------------------------------
# extension filtering
# --------------------------------------------------------------------------


def test_collects_exactly_the_six_source_extensions(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "a.v": "",
            "b.sv": "",
            "c.vh": "",
            "d.svh": "",
            "e.vhd": "",
            "f.vhdl": "",
            "g.txt": "",
            "h.py": "",
            "i.core": "",
            "j.veo": "",
            "k.sv.bak": "",
            "Makefile": "",
        },
    )

    assert rel(scan(tmp_path)) == ("a.v", "b.sv", "c.vh", "d.svh", "e.vhd", "f.vhdl")


def test_extensions_match_case_insensitively(tmp_path: Path) -> None:
    build(tmp_path, {"UPPER.V": "", "Mixed.SvH": ""})

    result = scan(tmp_path)

    assert rel(result) == ("Mixed.SvH", "UPPER.V")
    assert rel_of(result.include_candidates, result.root) == ("Mixed.SvH",)


def test_dotfile_named_like_an_extension_is_not_a_source(tmp_path: Path) -> None:
    build(tmp_path, {".v": "", "real.v": ""})

    assert rel(scan(tmp_path)) == ("real.v",)


# --------------------------------------------------------------------------
# unconditional skip list
# --------------------------------------------------------------------------


def test_always_skipped_directories(tmp_path: Path) -> None:
    layout = {f"{name}/hidden.v": "" for name in sorted(ALWAYS_SKIP_DIRS)}
    layout["keep.v"] = ""
    build(tmp_path, layout)

    assert rel(scan(tmp_path)) == ("keep.v",)


def test_skip_list_applies_at_any_depth(tmp_path: Path) -> None:
    build(tmp_path, {"rtl/build/gen.v": "", "rtl/sim_build/tb.v": "", "rtl/top.v": ""})

    assert rel(scan(tmp_path)) == ("rtl/top.v",)


def test_skip_list_matches_whole_directory_names_only(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            "builds/a.v": "",
            "workspace/b.v": "",
            "prebuild/c.v": "",
            "build.v": "",
        },
    )

    assert rel(scan(tmp_path)) == (
        "build.v",
        "builds/a.v",
        "prebuild/c.v",
        "workspace/b.v",
    )


def test_hidden_directories_are_skipped(tmp_path: Path) -> None:
    build(tmp_path, {".cache/a.v": "", ".github/b.v": "", "visible/c.v": ""})

    assert rel(scan(tmp_path)) == ("visible/c.v",)


def test_hidden_root_is_still_scanned(tmp_path: Path) -> None:
    build(tmp_path, {".hidden_root/a.v": ""})

    assert rel(scan(tmp_path / ".hidden_root")) == ("a.v",)


# --------------------------------------------------------------------------
# .gitignore (pathspec 1.x)
# --------------------------------------------------------------------------


def test_gitignore_excludes_files_and_directories(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            ".gitignore": "*.bak.v\ngenerated/\n",
            "keep.v": "",
            "stale.bak.v": "",
            "generated/gen.v": "",
            "generated/deep/deeper.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("keep.v",)


def test_gitignore_negation_reincludes(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            ".gitignore": "*.v\n!keep.v\n",
            "keep.v": "",
            "drop.v": "",
            "sub/keep.v": "",
            "sub/drop.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("keep.v", "sub/keep.v")


def test_nested_gitignore_overrides_its_parent(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            ".gitignore": "*.v\n",
            "top.v": "",
            "sub/.gitignore": "!*.v\n",
            "sub/rescued.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("sub/rescued.v",)


def test_nested_gitignore_can_add_exclusions(tmp_path: Path) -> None:
    build(
        tmp_path,
        {
            ".gitignore": "*.bak.v\n",
            "top.v": "",
            "sub/.gitignore": "local.v\n",
            "sub/local.v": "",
            "sub/shared.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("sub/shared.v", "top.v")


def test_ignored_directory_cannot_be_reincluded_from_within(tmp_path: Path) -> None:
    """Git's rule: a parent directory that is excluded is excluded, full stop."""
    build(
        tmp_path,
        {
            ".gitignore": "vendor/\n",
            "vendor/.gitignore": "!*.v\n",
            "vendor/prim.v": "",
            "top.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("top.v",)


def test_gitignore_patterns_are_relative_to_their_own_directory(
    tmp_path: Path,
) -> None:
    build(
        tmp_path,
        {
            "sub/.gitignore": "/local.v\n",
            "local.v": "",
            "sub/local.v": "",
            "sub/deep/local.v": "",
        },
    )

    assert rel(scan(tmp_path)) == ("local.v", "sub/deep/local.v")


def test_skip_list_beats_a_gitignore_negation(tmp_path: Path) -> None:
    build(tmp_path, {".gitignore": "!build/\n", "build/gen.v": "", "top.v": ""})

    assert rel(scan(tmp_path)) == ("top.v",)


def test_unreadable_gitignore_is_not_fatal(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").mkdir()  # a directory where a file was expected
    build(tmp_path, {"top.v": ""})

    assert rel(scan(tmp_path)) == ("top.v",)


# --------------------------------------------------------------------------
# symlinks
# --------------------------------------------------------------------------


def test_symlinked_directory_is_followed(tmp_path: Path) -> None:
    build(tmp_path, {"tree/top.v": "", "external/prim.v": ""})
    (tmp_path / "tree" / "vendor").symlink_to(tmp_path / "external")

    assert rel(scan(tmp_path / "tree")) == ("top.v", "vendor/prim.v")


def test_symlink_cycle_terminates(tmp_path: Path) -> None:
    build(tmp_path, {"tree/top.v": "", "tree/sub/leaf.v": ""})
    (tmp_path / "tree" / "sub" / "loop").symlink_to(tmp_path / "tree")

    assert rel(scan(tmp_path / "tree")) == ("sub/leaf.v", "top.v")


def test_self_referential_symlink_terminates(tmp_path: Path) -> None:
    build(tmp_path, {"tree/top.v": ""})
    (tmp_path / "tree" / "here").symlink_to(tmp_path / "tree" / "here")

    assert rel(scan(tmp_path / "tree")) == ("top.v",)


def test_dangling_symlink_is_skipped(tmp_path: Path) -> None:
    build(tmp_path, {"top.v": ""})
    (tmp_path / "gone.v").symlink_to(tmp_path / "nowhere.v")
    (tmp_path / "gonedir").symlink_to(tmp_path / "nowhere")

    assert rel(scan(tmp_path)) == ("top.v",)


def test_symlinked_file_is_collected(tmp_path: Path) -> None:
    build(tmp_path, {"tree/top.v": "", "external/prim.v": ""})
    (tmp_path / "tree" / "prim.v").symlink_to(tmp_path / "external" / "prim.v")

    assert rel(scan(tmp_path / "tree")) == ("prim.v", "top.v")


# --------------------------------------------------------------------------
# ordering, determinism and error handling
# --------------------------------------------------------------------------


def test_output_is_sorted_by_relative_posix_path(tmp_path: Path) -> None:
    # Created in reverse so readdir order is unlikely to match the answer.
    build(
        tmp_path,
        {
            "z/z.v": "",
            "z/a.v": "",
            "m.v": "",
            "a/z.v": "",
            "a/a.v": "",
            "a/nested/deep.v": "",
        },
    )

    result = scan(tmp_path)

    assert rel(result) == (
        "a/a.v",
        "a/nested/deep.v",
        "a/z.v",
        "m.v",
        "z/a.v",
        "z/z.v",
    )
    assert list(rel(result)) == sorted(rel(result))


def test_repeated_scans_agree(tmp_path: Path) -> None:
    build(tmp_path, {"a/x.v": "", "b/y.sv": "", "c/z.svh": ""})

    assert scan(tmp_path) == scan(tmp_path)


def test_result_is_independent_of_the_root_spelling(tmp_path: Path) -> None:
    build(tmp_path, {"rtl/top.v": "", "rtl/sub/leaf.sv": ""})

    absolute = scan(tmp_path)
    trailing = scan(Path(f"{tmp_path}{os.sep}"))

    assert rel(absolute) == rel(trailing)


def test_returned_paths_exist_under_the_root(tmp_path: Path) -> None:
    build(tmp_path, {"rtl/top.v": ""})

    result = scan(tmp_path)

    assert all(p.is_file() for p in result.files)
    assert all(p.is_relative_to(result.root) for p in result.files)


def test_empty_tree_scans_to_nothing(tmp_path: Path) -> None:
    result = scan(tmp_path)

    assert result.files == ()
    assert result.include_candidates == frozenset()


def test_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        scan(tmp_path / "nope")


def test_file_as_root_raises(tmp_path: Path) -> None:
    build(tmp_path, {"top.v": ""})

    with pytest.raises(NotADirectoryError):
        scan(tmp_path / "top.v")


def test_root_accepts_a_string(tmp_path: Path) -> None:
    build(tmp_path, {"top.v": ""})

    assert rel(scan(str(tmp_path))) == ("top.v",)
