"""CLI tests — the contract, not the pipeline.

Everything pipeline-shaped is asserted elsewhere; these tests pin down what
the thin skin over ``autocore.generate()`` promises: warnings on stderr, YAML
in the output file or on stdout under ``--dry-run``, and the exit codes — 0
for success including success-with-warnings, 1 for fatal (the overwrite
refusal included), 2 for usage errors.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer.testing
from typer.testing import CliRunner

from autocore import __version__, interact
from autocore.cli import app

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"


# --------------------------------------------------------------------------
# scaffold smoke
# --------------------------------------------------------------------------


def test_version_flag_reports_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"autocore {__version__}"


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage: autocore" in result.output


def test_init_help_lists_every_documented_flag() -> None:
    result = runner.invoke(app, ["init", "--help"])

    assert result.exit_code == 0
    for flag in (
        "--name",
        "--library",
        "--output",
        "--top",
        "--tb-glob",
        "--define",
        "--yes",
        "--non-interactive",
        "--force",
        "--dry-run",
        "--verbose",
        "--quiet",
    ):
        assert flag in result.output


def test_the_tb_glob_help_says_it_replaces_the_defaults() -> None:
    """It could plausibly extend them instead, so the help has to say which."""
    result = runner.invoke(app, ["init", "--help"])

    assert "REPLACES" in result.output


# --------------------------------------------------------------------------
# --dry-run: YAML on stdout, warnings on stderr, exit 0
# --------------------------------------------------------------------------


def test_dry_run_prints_exactly_the_golden_to_stdout() -> None:
    result = runner.invoke(app, ["init", str(FIXTURES / "single_module"), "--dry-run"])

    assert result.exit_code == 0
    assert result.stdout == (GOLDEN / "single_module.core").read_text()
    assert result.stderr == ""


def test_warnings_go_to_stderr_and_success_with_warnings_exits_zero() -> None:
    result = runner.invoke(app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run"])

    assert result.exit_code == 0
    assert "MultipleTops" in result.stderr
    assert "ExcludedFromRtl" in result.stderr
    # stdout stays pure YAML.
    assert result.stdout.startswith("CAPI=2:\n")
    assert "warning" not in result.stdout


def test_quiet_suppresses_warnings_but_not_the_yaml() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "-q"]
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.startswith("CAPI=2:\n")


def test_verbose_adds_an_info_summary_on_stderr() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "single_module"), "--dry-run", "-v"]
    )

    assert result.exit_code == 0
    assert "info:" in result.stderr
    assert "'alu'" in result.stderr


def test_verbose_and_quiet_exclude_each_other() -> None:
    result = runner.invoke(app, ["init", str(FIXTURES / "single_module"), "-v", "-q"])

    assert result.exit_code == 2


# --------------------------------------------------------------------------
# flags that reach the pipeline
# --------------------------------------------------------------------------


def test_name_and_library_shape_the_vlnv() -> None:
    result = runner.invoke(
        app,
        [
            "init",
            str(FIXTURES / "single_module"),
            "--dry-run",
            "--name",
            "alpha",
            "--library",
            "mylib",
        ],
    )

    assert result.exit_code == 0
    assert "name: :mylib:alpha:0.1.0" in result.stdout


def test_top_overrides_detection_and_silences_the_ambiguity() -> None:
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--top", "soc_b"],
    )

    assert result.exit_code == 0
    assert "toplevel: soc_b" in result.stdout
    assert "rtl/soc_b.v" in result.stdout
    assert "MultipleTops" not in result.stderr


def test_an_undeclared_top_is_fatal() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "single_module"), "--dry-run", "--top", "ghost"]
    )

    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "ghost" in result.stderr


def test_define_switches_the_guarded_instantiation() -> None:
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "ifdef_heavy"), "--dry-run", "--define", "USE_MUL"],
    )

    assert result.exit_code == 0
    assert "rtl/mul.v" in result.stdout
    assert "rtl/shifter.v" not in result.stdout


def test_a_testbench_tree_emits_the_sim_target() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "with_testbench"), "--dry-run", "-v"]
    )

    assert result.exit_code == 0
    assert result.stdout == (GOLDEN / "with_testbench.core").read_text()
    assert "sim toplevel 'chip_tb', 3 file(s) in the tb fileset" in result.stderr


def test_tb_glob_reaches_the_pipeline_and_replaces_the_defaults() -> None:
    """`multiple_tops` has no testbench until a glob says otherwise; naming
    `soc_b.v` moves it out of rtl and into a sim target of its own."""
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--tb-glob", "soc_b.*"],
    )

    assert result.exit_code == 0
    assert "toplevel: soc_b\n" in result.stdout
    assert "default_tool: verilator" in result.stdout
    # `soc_b` is no longer an rtl top candidate, so the ambiguity is gone too.
    assert "MultipleTops" not in result.stderr


# --------------------------------------------------------------------------
# --define UX: both forms documented, garbage refused, -v accountable
# --------------------------------------------------------------------------


def test_the_define_help_shows_both_forms() -> None:
    """A flag that takes NAME and NAME=VALUE has to say so; guessing wrong
    costs a silent misparse rather than an error."""
    result = runner.invoke(app, ["init", "--help"])

    assert "NAME=VALUE" in result.output
    assert "Repeatable" in result.output


def test_both_define_forms_are_accepted() -> None:
    result = runner.invoke(
        app,
        [
            "init",
            str(FIXTURES / "ifdef_heavy"),
            "--dry-run",
            "--define",
            "USE_MUL",
            "--define",
            "WIDTH=32",
        ],
    )

    assert result.exit_code == 0
    assert "rtl/mul.v" in result.stdout


def test_a_define_may_carry_an_empty_value() -> None:
    """``-DNAME=`` is how a build says "defined, expanding to nothing"."""
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "single_module"), "--dry-run", "--define", "NDEBUG="],
    )

    assert result.exit_code == 0


def test_a_malformed_define_is_a_usage_error() -> None:
    """Slang takes predefines as opaque strings, so garbage would define
    nothing and surface later as an unexplained parse failure instead."""
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "single_module"), "--dry-run", "--define", "2BAD=1"],
    )

    assert result.exit_code == 2
    assert "NAME=VALUE" in result.output


def test_a_define_with_a_space_in_the_name_is_a_usage_error() -> None:
    result = runner.invoke(
        app,
        ["init", str(FIXTURES / "single_module"), "--dry-run", "--define", "FOO BAR"],
    )

    assert result.exit_code == 2


def test_verbose_lists_the_defines_in_effect() -> None:
    result = runner.invoke(
        app,
        [
            "init",
            str(FIXTURES / "ifdef_heavy"),
            "--dry-run",
            "-v",
            "--define",
            "USE_MUL",
            "--define",
            "WIDTH=32",
        ],
    )

    assert result.exit_code == 0
    assert "info: defines in effect: USE_MUL, WIDTH=32" in result.stderr


def test_verbose_says_so_when_no_define_is_in_effect() -> None:
    """The interesting case: a suspiciously small rtl fileset and a user
    wondering whether their -D reached the tool at all."""
    result = runner.invoke(
        app, ["init", str(FIXTURES / "ifdef_heavy"), "--dry-run", "-v"]
    )

    assert "info: defines in effect: none" in result.stderr


# --------------------------------------------------------------------------
# warning volume: grouped by default, whole under -v
# --------------------------------------------------------------------------


def externals(tmp_path: Path, count: int) -> Path:
    """A tree whose every leaf instantiates a module nobody declares."""
    root = tmp_path / "externals"
    root.mkdir()
    instances = "\n".join(f"  vendor_{i} u_{i} ();" for i in range(count))
    (root / "top.v").write_text(f"module top;\n{instances}\nendmodule\n")
    return root


def test_a_repeated_warning_code_collapses_into_one_counted_line(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["init", str(externals(tmp_path, 4)), "--dry-run"])

    assert result.exit_code == 0
    lines = [line for line in result.stderr.splitlines() if "ExternalReference" in line]
    assert len(lines) == 1
    assert lines[0].startswith("warning: 4 x [ExternalReference], e.g. ")
    # The count is not the whole story, so the line says where the rest is.
    assert "-v lists them all" in lines[0]


def test_a_code_below_the_threshold_is_not_grouped(tmp_path: Path) -> None:
    """Two of a kind is not a wall; collapsing them would hide a message to
    save no scrolling at all."""
    result = runner.invoke(app, ["init", str(externals(tmp_path, 2)), "--dry-run"])

    lines = [line for line in result.stderr.splitlines() if "ExternalReference" in line]
    assert len(lines) == 2
    assert all("vendor_" in line for line in lines)


def test_verbose_expands_every_repeated_warning(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", str(externals(tmp_path, 4)), "--dry-run", "-v"]
    )

    lines = [line for line in result.stderr.splitlines() if "ExternalReference" in line]
    assert len(lines) == 4
    for index in range(4):
        assert any(f"'vendor_{index}'" in line for line in lines)


def test_a_long_file_list_is_folded_until_verbose_asks() -> None:
    """`ExcludedFromRtl` names one file here and 18 on picorv32; the message
    carries the count and the names travel as details."""
    folded = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes"]
    )
    expanded = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes", "-v"]
    )

    (line,) = [line for line in folded.stderr.splitlines() if "ExcludedFromRtl" in line]
    assert "1 file(s) not reachable from top 'soc_a'" in line
    assert "rtl/soc_b.v" not in line
    assert "-v lists them" in line
    # Nothing is unreachable, it is only folded: -v indents the names beneath.
    assert "    rtl/soc_b.v" in expanded.stderr


def test_quiet_still_silences_everything(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", str(externals(tmp_path, 4)), "--dry-run", "-q"]
    )

    assert result.stderr == ""


# --------------------------------------------------------------------------
# an output above the tree it describes
# --------------------------------------------------------------------------


def test_an_output_outside_the_tree_warns_about_upward_paths(tmp_path: Path) -> None:
    """fusesoc 2.4.6 deprecates `../` file paths and will make them an error,
    so the choice that produces them is worth saying out loud."""
    root = project(tmp_path)
    out = tmp_path / "build" / "single_module.core"

    result = runner.invoke(app, ["init", str(root), "--output", str(out)])

    assert result.exit_code == 0
    (line,) = [
        line for line in result.stderr.splitlines() if "OutputAboveCoreDir" in line
    ]
    assert "1 file path(s) reach above its own directory" in line
    assert "../single_module/alu.v" in out.read_text()


def test_the_default_output_never_warns_about_upward_paths(tmp_path: Path) -> None:
    root = project(tmp_path)

    result = runner.invoke(app, ["init", str(root)])

    assert result.exit_code == 0
    assert "OutputAboveCoreDir" not in result.stderr


# --------------------------------------------------------------------------
# --yes and the prompt gate
#
# CliRunner's stdin is not a terminal, so every test above already runs the
# not-a-TTY leg of the gate. These pin the other two: a TTY that prompts, and
# a TTY silenced by --yes. The seam is entered the way the CLI enters it —
# `interact.decide` builds the real asker itself, so replacing that class is
# what a test replaces, and the gate under test is the real one.
# --------------------------------------------------------------------------


class FakeAsker:
    """The scripted `Asker` again; an empty queue means "pressed enter"."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.offered: list[list[str]] = []

    def select(self, question: str, choices, *, default: str) -> str:
        self.asked.append(question)
        self.offered.append([choice.label for choice in choices])
        return self.answers.pop(0) if self.answers else default


def at_a_terminal(monkeypatch, *answers: str) -> FakeAsker:
    """Put the CLI at a terminal with a scripted asker behind the seam.

    `CliRunner.invoke` installs its own `sys.stdin` for the duration of the
    call, so patching `sys.stdin` out here would simply be overwritten — the
    terminal has to be simulated on the runner's own stream class instead.
    Nothing about the gate is faked: the CLI still reads the real `sys.stdin`
    and `decide` still asks it the real question.
    """
    asker = FakeAsker(*answers)
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True)
    monkeypatch.setattr(interact, "QuestionaryAsker", lambda: asker)
    return asker


def test_a_terminal_prompts_and_the_answer_reaches_the_manifest(monkeypatch) -> None:
    asker = at_a_terminal(monkeypatch, "soc_b")

    result = runner.invoke(app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run"])

    assert result.exit_code == 0
    assert len(asker.asked) == 1
    assert "toplevel: soc_b" in result.stdout
    # Asked and answered: the warning about the guess is spent.
    assert "MultipleTops" not in result.stderr


def test_pressing_enter_reproduces_the_yes_output_byte_for_byte(monkeypatch) -> None:
    """Prompting happens outside the pipeline, so an all-defaults interactive
    run and a ``--yes`` run emit the same bytes."""
    with_yes = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes"]
    )
    at_a_terminal(monkeypatch)

    prompted = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run"]
    )

    assert prompted.exit_code == with_yes.exit_code == 0
    assert prompted.stdout == with_yes.stdout


def test_yes_silences_the_prompt_at_a_terminal(monkeypatch) -> None:
    """``--yes`` is not a no-op: this is the condition it silences."""
    asker = at_a_terminal(monkeypatch, "soc_b")

    result = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes"]
    )

    assert result.exit_code == 0
    assert asker.asked == []
    assert "toplevel: soc_a" in result.stdout
    # Not asked, so told instead.
    assert "MultipleTops" in result.stderr


def test_an_unambiguous_tree_asks_nothing_at_a_terminal(monkeypatch) -> None:
    asker = at_a_terminal(monkeypatch)

    result = runner.invoke(app, ["init", str(FIXTURES / "single_module"), "--dry-run"])

    assert result.exit_code == 0
    assert asker.asked == []
    assert result.stdout == (GOLDEN / "single_module.core").read_text()


def test_a_pipe_asks_nothing_without_yes() -> None:
    """The default CliRunner stdin, stated outright rather than assumed."""
    piped = runner.invoke(app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run"])
    with_yes = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes"]
    )

    assert piped.stdout == with_yes.stdout
    assert "MultipleTops" in piped.stderr


# --------------------------------------------------------------------------
# with_testbench and multiple_tops in both TTY-simulated and --yes modes
#
# Asserted as one block rather than left implicit in the tests above, because
# the claim is about *pairs* of runs: the tree with no question to
# ask must be unaffected by there being a terminal, and the tree with one must
# take the answer in the interactive mode and say what it assumed in the other.
# Both fixtures, both modes, four tests.
# --------------------------------------------------------------------------


def test_exit_criterion_with_testbench_under_yes() -> None:
    """The classified tree: two filesets, a sim target, and nothing to ask."""
    result = runner.invoke(
        app, ["init", str(FIXTURES / "with_testbench"), "--dry-run", "--yes"]
    )

    assert result.exit_code == 0
    assert result.stdout == (GOLDEN / "with_testbench.core").read_text()
    assert "toplevel: chip\n" in result.stdout
    assert "toplevel: chip_tb\n" in result.stdout
    # Every classification branch decided a file, so none had to guess.
    assert result.stderr == ""


def test_exit_criterion_with_testbench_at_a_terminal(monkeypatch) -> None:
    """A terminal changes nothing when the tree leaves nothing ambiguous."""
    asker = at_a_terminal(monkeypatch)

    result = runner.invoke(app, ["init", str(FIXTURES / "with_testbench"), "--dry-run"])

    assert result.exit_code == 0
    assert asker.asked == []
    assert result.stdout == (GOLDEN / "with_testbench.core").read_text()


def test_exit_criterion_multiple_tops_under_yes() -> None:
    """The ambiguous tree, unasked: the fallback decides and the warning says
    so."""
    result = runner.invoke(
        app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run", "--yes"]
    )

    assert result.exit_code == 0
    assert result.stdout == (GOLDEN / "multiple_tops.core").read_text()
    assert "toplevel: soc_a\n" in result.stdout
    # Told what was assumed, and what it cost: soc_b fell out of the closure.
    assert "[MultipleTops]" in result.stderr
    assert "[ExcludedFromRtl]" in result.stderr


def test_exit_criterion_multiple_tops_at_a_terminal(monkeypatch) -> None:
    """The ambiguous tree, asked: one question, with the numbers the fallback
    weighed, and an answer that reaches the manifest."""
    asker = at_a_terminal(monkeypatch, "soc_b")

    result = runner.invoke(app, ["init", str(FIXTURES / "multiple_tops"), "--dry-run"])

    assert result.exit_code == 0
    assert len(asker.asked) == 1
    assert asker.offered == [
        ["soc_a (2 file(s) in its closure)", "soc_b (2 file(s) in its closure)"]
    ]
    assert "toplevel: soc_b\n" in result.stdout
    assert "rtl/soc_b.v" in result.stdout
    # Answered, so nothing was assumed and nothing is warned about.
    assert "[MultipleTops]" not in result.stderr


# --------------------------------------------------------------------------
# usage errors (exit 2)
# --------------------------------------------------------------------------


def test_a_name_outside_the_vlnv_charset_is_a_usage_error() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "single_module"), "--name", "bad name!"]
    )

    assert result.exit_code == 2
    assert "--name" in result.output


def test_a_library_outside_the_vlnv_charset_is_a_usage_error() -> None:
    result = runner.invoke(
        app, ["init", str(FIXTURES / "single_module"), "--library", "a:b"]
    )

    assert result.exit_code == 2


def test_a_missing_path_is_a_usage_error() -> None:
    result = runner.invoke(app, ["init", str(FIXTURES / "no_such_tree")])

    assert result.exit_code == 2


# --------------------------------------------------------------------------
# the write site: default output, overwrite refusal, --force
# --------------------------------------------------------------------------


def project(tmp_path: Path, fixture: str = "single_module") -> Path:
    """A disposable copy of a fixture tree, safe to write into."""
    target = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, target)
    return target


def test_the_default_output_is_path_name_dot_core(tmp_path: Path) -> None:
    root = project(tmp_path)

    result = runner.invoke(app, ["init", str(root)])

    assert result.exit_code == 0
    written = root / "single_module.core"
    assert written.read_text() == (GOLDEN / "single_module.core").read_text()
    assert f"wrote {written}" in result.stderr
    assert result.stdout == ""  # without --dry-run, stdout carries no YAML


def test_dash_name_renames_the_default_output(tmp_path: Path) -> None:
    root = project(tmp_path)

    result = runner.invoke(app, ["init", str(root), "--name", "alpha"])

    assert result.exit_code == 0
    assert (root / "alpha.core").is_file()


def test_d20_an_existing_output_is_refused_with_exit_1(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "single_module.core").write_text("precious\n")

    result = runner.invoke(app, ["init", str(root)])

    assert result.exit_code == 1
    assert "--force" in result.stderr
    assert (root / "single_module.core").read_text() == "precious\n"


def test_force_overwrites_the_existing_output(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / "single_module.core").write_text("precious\n")

    result = runner.invoke(app, ["init", str(root), "--force"])

    assert result.exit_code == 0
    assert (root / "single_module.core").read_text().startswith("CAPI=2:\n")


def test_output_elsewhere_gets_relative_paths_and_parents(tmp_path: Path) -> None:
    root = project(tmp_path)
    out = tmp_path / "cores" / "deep" / "out.core"

    result = runner.invoke(app, ["init", str(root), "--output", str(out)])

    assert result.exit_code == 0
    assert "- ../../single_module/alu.v" in out.read_text()
