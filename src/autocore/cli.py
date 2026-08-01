"""Command-line entry point for autocore.

This module is a CLI layer over `autocore.generate()` and
`autocore.regenerate()`. It turns command-line flags into `GenerateOptions`,
renders warnings for stderr, and either writes the generated `.core` file or
prints it to stdout under `--dry-run`.

No pipeline logic should live here. The CLI is responsible for argument
validation, user-facing output, exit behavior, and interactive plumbing, while
the actual scan/parse/resolve/emit decisions stay inside the library.

Exit codes follow a simple rule:
- 0: success, including success with warnings
- 1: fatal runtime failure, including overwrite refusal without `--force`
- 2: usage error, such as invalid flag values or a bad input path
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from autocore import (
    GenerateError,
    GenerateOptions,
    GenerateResult,
    __version__,
    generate,
    regenerate,
)
from autocore.emit import write_core
from autocore.interact import decide
from autocore.models import Warning as PipelineWarning

app = typer.Typer(
    name="autocore",
    help="Scan an RTL tree and emit one FuseSoC CAPI2 .core file.",
    no_args_is_help=True,
    add_completion=False,
)

#: Validation pattern for one FuseSoC VLNV part.
#: `--name` and `--library` are checked here because an invalid VLNV part is a
#: user input error, not a pipeline resolution problem.
_VLNV_PART_RE = re.compile(r"[A-Za-z0-9_.\-]+")

#: Accepted `--define` forms: `NAME` and `NAME=VALUE`.
#: The name portion follows SystemVerilog identifier rules, including `$`.
#: The value portion may be anything, including empty text.
_DEFINE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(=.*)?\Z", re.DOTALL)

#: Number of warnings with the same code needed before grouping kicks in.
#: This keeps common repeated diagnostics readable in normal output while
#: `-v` still shows everything in full.
WARNING_GROUP_THRESHOLD = 3


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"autocore {__version__}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the autocore version and exit.",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    """Root Typer callback for the autocore CLI."""


def _vlnv_part(value: str | None) -> str | None:
    """Validate one `--name` or `--library` VLNV part.

    The CLI rejects invalid values early so the user gets a clear usage error
    instead of a later failure deeper in the toolchain.
    """
    if value is None or _VLNV_PART_RE.fullmatch(value):
        return value
    raise typer.BadParameter(
        "must be non-empty and use only letters, digits, '_', '.' and '-'"
    )


def _defines(values: list[str] | None) -> list[str] | None:
    """Reject a ``--define`` that is neither ``NAME`` nor ``NAME=VALUE``.

    A malformed define is a usage error (exit 2) rather than something to
    forward: slang takes predefines as opaque strings, so ``--define 2=3`` or
    a shell-mangled ``--define "FOO BAR"`` would not fail; it would silently
    define nothing, and the missing macro would then show up as an unexplained
    parse failure or a module that lost half its instantiations. Failing at
    the flag is the only place the cause is still visible.
    """
    for value in values or ():
        if not _DEFINE_RE.match(value):
            raise typer.BadParameter(
                f"{value!r} is neither NAME nor NAME=VALUE: a define starts "
                "with a letter or '_' and continues with letters, digits, "
                "'_' or '$', optionally followed by '=' and any value"
            )
    return values


@app.command()
def init(
    path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            help="Root of the RTL tree to scan.",
        ),
    ] = Path("."),
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            callback=_vlnv_part,
            help="Core name part of the VLNV. Defaults to the sanitized "
            "directory name of PATH.",
        ),
    ] = None,
    library: Annotated[
        str | None,
        typer.Option(
            "--library",
            callback=_vlnv_part,
            help="Library part of the VLNV. Empty by default.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Where to write the manifest. Defaults to PATH/<name>.core; "
            "emitted file paths are relative to its directory.",
        ),
    ] = None,
    top: Annotated[
        str | None,
        typer.Option(
            "--top",
            help="Toplevel module to use instead of detecting one. Must be "
            "declared somewhere in the tree.",
        ),
    ] = None,
    define: Annotated[
        list[str] | None,
        typer.Option(
            "--define",
            callback=_defines,
            help="Preprocessor define, in either form: NAME (defined, with no "
            "value) or NAME=VALUE. Repeatable, e.g. --define SYNTHESIS "
            "--define WIDTH=32. Applied to every file in the tree, and the "
            "escape hatch for the one thing single-file parsing cannot see: a "
            "macro one file defines and another one uses. Anything else is "
            "rejected as a usage error rather than passed on. -v lists the "
            "defines in effect.",
        ),
    ] = None,
    tb_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--tb-glob",
            help="Filename glob marking a testbench, matched case-insensitively "
            "against the basename. Repeatable. REPLACES the built-in patterns "
            "(*_tb.*, tb_*.*, *_test.*, testbench.*) rather than adding to "
            "them; the evidence rule and the // autocore: tb|rtl comments "
            "still apply.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "--non-interactive",
            help="Never prompt; apply the documented default to every "
            "ambiguity and warn about it. Implied whenever stdin is not a "
            "terminal, which is what makes scripts and CI behave the same "
            "way with or without it.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing output file. Without it, an existing "
            "file is refused with exit code 1.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the manifest to stdout instead of writing a file.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Add an info summary on stderr, and print every warning in "
            "full: repeated codes stop being grouped and the file lists they "
            "carry are listed rather than counted.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Silence warnings and status lines."),
    ] = False,
) -> None:
    """Scan `PATH` and generate one FuseSoC CAPI2 `.core` manifest.

    This is the main user-facing command. It runs the pipeline, optionally asks
    about ambiguities, prints warnings and status information to stderr, and then
    either writes the result to a file or prints it to stdout under `--dry-run`.
    """
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet exclude each other")

    options = GenerateOptions(
        name=name,
        library=library,
        top=top,
        defines=tuple(define or ()),
        tb_globs=tuple(tb_glob or ()),
        core_dir=output.parent if output is not None else path,
    )
    try:
        result = generate(path, options)
        result = _apply_answers(result, path, options, assume_yes=yes)
    except GenerateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    warnings = result.model.warnings + _upward_path_warnings(result, output)
    if not quiet:
        for line in _warning_lines(warnings, path, verbose=verbose):
            typer.echo(line, err=True)
    if verbose:
        model = result.model
        sim = ""
        if model.tb_compile_order:
            sim = (
                f"sim toplevel '{model.tb_top}', "
                f"{len(model.tb_compile_order)} file(s) in the tb fileset, "
            )
        typer.echo(
            "info: defines in effect: "
            + (", ".join(options.defines) if options.defines else "none"),
            err=True,
        )
        typer.echo(
            f"info: toplevel '{model.top}', "
            f"{len(model.rtl)} file(s) in the rtl fileset, "
            f"{sim}"
            f"{len(warnings)} warning(s)",
            err=True,
        )

    if dry_run:
        typer.echo(result.text, nl=False)
        return

    if output is None:
        # The middle part of the VLNV is the core name, whether that came
        # from `--name` or from the sanitized directory name.
        output = path / f"{result.manifest.vlnv.split(':')[2]}.core"
    try:
        # Creating the parents is entry-point plumbing, not part of the
        # overwrite-refusal promise: that is about existing *files*.
        output.parent.mkdir(parents=True, exist_ok=True)
        write_core(result.text, output, force=force)
    # FileExistsError is the overwrite refusal; the rest is the filesystem.
    except OSError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not quiet:
        typer.echo(f"wrote {output}", err=True)


def _apply_answers(
    result: GenerateResult,
    root: Path,
    options: GenerateOptions,
    *,
    assume_yes: bool,
) -> GenerateResult:
    """Offer `result`'s ambiguities to the user and re-resolve if they chose.

    Whether anything is actually asked is `interact.decide`'s call, not this
    module's, the gate lives in one place and this is not it. An empty
    `Decisions` means nothing was asked or nothing was changed, and the first
    run stands untouched, which is why ``--yes`` output stays byte-identical
    to the goldens. Only Resolve and Emit re-run; the tree is read once.
    """
    decisions = decide(result.model, root, assume_yes=assume_yes, stdin=sys.stdin)
    if not decisions:
        return result
    return regenerate(
        result,
        replace(
            options,
            top=decisions.top if decisions.top is not None else options.top,
            tb_overrides=decisions.tb_overrides,
        ),
    )


def _upward_path_warnings(
    result: GenerateResult, output: Path | None
) -> tuple[PipelineWarning, ...]:
    """Warn when the chosen output forces file paths above the .core file.

    A manifest written outside the tree it describes refers to its files as
    ``../rtl/alu.v``, and fusesoc 2.4.6 deprecates exactly that: every such
    path raises a `FutureWarning` from `Core.export` saying it "is not within
    the directory containing the core file [...] and will be an error in a
    future FuseSoC version". Nothing is broken today and the default output is
    never affected, ``PATH/<name>.core`` sits above everything it lists, so
    this is a warning about a choice, made where the choice is made, and not
    part of the model any library caller shares.
    """
    upward = sorted(
        {
            entry.path
            for fileset in result.manifest.filesets
            for entry in fileset.files
            if entry.path.startswith("../")
        }
    )
    if not upward:
        return ()
    where = f"{output} " if output is not None else ""
    return (
        PipelineWarning(
            "OutputAboveCoreDir",
            f"the manifest {where}sits outside the tree it describes, so "
            f"{len(upward)} file path(s) reach above its own directory; "
            "fusesoc 2.4.6 deprecates that and will make it an error, so "
            "prefer an output inside the scanned tree",
            details=tuple(upward),
        ),
    )


def _warning_lines(
    warnings: Sequence[PipelineWarning], root: Path, *, verbose: bool
) -> list[str]:
    """Render `warnings` for stderr, grouped unless `verbose`.

    Under ``-v`` every warning gets its own line and every detail an indented
    one beneath it, nothing is summarised, because that is what the flag is
    for. Otherwise a code occurring `WARNING_GROUP_THRESHOLD` times or more
    collapses to one counted line carrying the first of its messages as the
    example, and details collapse to a pointer at ``-v``.

    Order is `warnings`' own, which Resolve sorted by ``(path, code)``; a
    grouped line takes the place of its code's first occurrence. Same input,
    same lines, always.
    """
    if verbose:
        lines: list[str] = []
        for warning in warnings:
            lines.append(_warning_line(warning, root))
            lines += [f"    {detail}" for detail in warning.details]
        return lines

    counts = Counter(warning.code for warning in warnings)
    lines = []
    grouped: set[str] = set()
    for warning in warnings:
        if counts[warning.code] < WARNING_GROUP_THRESHOLD:
            lines.append(_warning_line(warning, root, fold_details=True))
        elif warning.code not in grouped:
            grouped.add(warning.code)
            lines.append(
                f"warning: {counts[warning.code]} x [{warning.code}], e.g. "
                f"{_located(warning, root)}{warning.message} "
                "(-v lists them all)"
            )
    return lines


def _warning_line(
    warning: PipelineWarning, root: Path, *, fold_details: bool = False
) -> str:
    """Render one warning as a single stderr line."""
    line = f"warning: {_located(warning, root)}{warning.message} [{warning.code}]"
    if fold_details and warning.details:
        line += " (-v lists them)"
    return line


def _located(warning: PipelineWarning, root: Path) -> str:
    """Return the `path: ` prefix for a warning line, or an empty string."""
    if warning.path is None:
        return ""
    try:
        return f"{warning.path.relative_to(root).as_posix()}: "
    except ValueError:
        return f"{warning.path}: "
