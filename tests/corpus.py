"""Run autocore over a real tree and check what a real consumer makes of it.

    python tests/corpus.py REPO [--top NAME] [--subdir NAME]
                                [--expect-warning CODE] [--verilate]
                                [--target default|sim]

``REPO`` is a corpus clone or a copy of the `with_testbench` fixture; the
assertion is the same either way. The CI corpus job calls this once per
assertion. Not collected by pytest (no ``test_`` prefix).

One invocation runs the installed CLI as a subprocess, repeats the pipeline
in-process and compares the two outputs, loads the manifest with FuseSoC's
strict CAPI2 loader, then optionally asserts a warning code and runs Verilator.

The ``--verilate`` leg differs by target. ``default`` adds ``--mode=lint-only``,
since a pure-RTL target has no C++ driver and would fail to link for want of
``main()``; missing modules still fail loudly (MODMISSING). ``sim`` passes no
mode, so the generated ``verilator: {mode: binary}`` applies and the testbench
is built and run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fusesoc.capi2.core import Core
from fusesoc.capi2.coreparser import Core2Parser

from autocore import GenerateOptions, generate
from autocore.models import ProjectModel
from autocore.scan import scan


def main() -> None:
    args = _parse_args()
    repo = args.repo.resolve()
    if not repo.is_dir():
        sys.exit(f"corpus: {repo} is not a directory")

    # An empty --subdir writes at the tree root, which is what a user's own run
    # looks like. The corpus repos cannot: picorv32 ships a colliding core.
    core_name = f"{repo.name}_auto.core"
    core_path = repo / args.subdir / core_name if args.subdir else repo / core_name

    init_stderr = _run_init(repo, core_path, args.top)
    vlnv, model = _diagnostics(repo, core_path, args.top)
    expected_top = model.tb_top if args.target == "sim" else model.top
    _validate_strict(core_path, expected_top, args.target)

    if args.expect_warning:
        _assert_warning(init_stderr, args.expect_warning)
    if args.verilate:
        _verilate(core_path, vlnv, args.target)

    print(f"corpus: PASS for {repo.name}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="root of the cloned corpus repo")
    parser.add_argument("--top", help="forwarded to autocore init --top")
    parser.add_argument(
        "--subdir",
        default="autocore_out",
        help="clean subdirectory of REPO to write the manifest into; empty "
        "writes it at the tree root",
    )
    parser.add_argument(
        "--expect-warning",
        metavar="CODE",
        help="assert this warning code appears on the CLI's stderr",
    )
    parser.add_argument(
        "--verilate",
        action="store_true",
        help="run fusesoc + verilator on the generated manifest",
    )
    parser.add_argument(
        "--target",
        choices=("default", "sim"),
        default="default",
        help="which generated target to validate and, with --verilate, run; "
        "'sim' simulates for real, 'default' lints",
    )
    return parser.parse_args()


def _tool(name: str) -> str:
    """A console script installed next to this interpreter, PATH as fallback."""
    beside = Path(sys.executable).parent / name
    if beside.is_file():
        return str(beside)
    found = shutil.which(name)
    if found is None:
        sys.exit(f"corpus: cannot find the '{name}' executable")
    return found


def _run_init(repo: Path, core_path: Path, top: str | None) -> str:
    """The CLI run. Returns its stderr, the full warning list."""
    command = [
        _tool("autocore"),
        "init",
        str(repo),
        "--yes",
        "--output",
        str(core_path),
    ]
    if top is not None:
        command += ["--top", top]

    print(f"== {repo.name}: {' '.join(command[1:])}")
    completed = subprocess.run(command, capture_output=True, text=True)
    print("-- autocore init stderr (the full warning list):")
    print(completed.stderr, end="")
    if completed.returncode != 0:
        sys.exit(f"corpus: autocore init exited {completed.returncode}")
    if not core_path.is_file():
        sys.exit(f"corpus: autocore init wrote nothing at {core_path}")
    return completed.stderr


def _diagnostics(
    repo: Path, core_path: Path, top: str | None
) -> tuple[str, ProjectModel]:
    """The per-repo diagnostics. Returns (vlnv, model)."""
    result = generate(repo, GenerateOptions(top=top, core_dir=core_path.parent))
    model = result.model
    scanned = scan(repo).files
    excluded = sorted(
        path.relative_to(repo).as_posix()
        for path in scanned
        if path not in model.rtl and path not in model.testbenches
    )

    print(f"-- {repo.name}: diagnostics")
    print(f"   detected top:   {model.top}")
    print(f"   sim toplevel:   {model.tb_top or '(none: no sim target)'}")
    print(f"   closure size:   {len(model.rtl)} file(s) in the rtl fileset")
    print(f"   scanned:        {len(scanned)} source file(s)")
    print(f"   testbenches:    {len(model.testbenches)}")
    print(f"   excluded:       {len(excluded)} file(s) outside the closure")
    for path in excluded:
        print(f"     - {path}")
    print(f"   warnings:       {len(model.warnings)}")

    if core_path.read_text(encoding="utf-8") != result.text:
        sys.exit(
            "corpus: the CLI's output differs from an in-process generate() "
            "with the same options: a determinism or plumbing bug"
        )
    return result.manifest.vlnv, model


def _validate_strict(core_path: Path, detected_top: str, target: str) -> None:
    """FuseSoC's own loader on the written file, strict mode."""
    flags = {"target": target, "is_toplevel": True}
    # Strict mode raises fusesoc's SyntaxError on any schema violation.
    core = Core(Core2Parser(allow_additional_properties=False), core_path)
    files = core.get_files(flags)
    if not files:
        sys.exit(f"corpus: strict loader resolved an empty {target} file list")
    missing = [
        entry["name"]
        for entry in files
        if not (core_path.parent / entry["name"]).resolve().is_file()
    ]
    if missing:
        sys.exit(f"corpus: resolved files do not exist on disk: {missing}")
    toplevel = core.get_toplevel(flags)
    if toplevel != detected_top:
        sys.exit(f"corpus: loader toplevel {toplevel!r} != {detected_top!r}")
    print(
        f"-- strict loader: OK, {len(files)} file(s) resolved for the "
        f"{target} target, toplevel {toplevel!r}"
    )


def _assert_warning(init_stderr: str, code: str) -> None:
    """The warning path itself, not mere success, is the assertion."""
    if f"[{code}]" not in init_stderr:
        sys.exit(f"corpus: expected warning code [{code}] on the CLI's stderr")
    print(f"-- warning path: OK, [{code}] appeared on stderr")


def _verilate(core_path: Path, vlnv: str, target: str) -> None:
    """The Verilator leg, from a scratch fusesoc workspace.

    ``--mode=lint-only`` applies to the ``default`` target only: the ``sim``
    target brings its own ``mode: binary`` and is meant to run.
    """
    command = [
        _tool("fusesoc"),
        "--cores-root",
        str(core_path.parent),
        "run",
        f"--target={target}",
        "--tool=verilator",
        vlnv,
    ]
    if target == "default":
        command.append("--mode=lint-only")
    # Flushed because the subprocess inherits stdout and writes to the fd
    # directly; without this a piped CI log interleaves the two wrongly.
    print(f"-- verilator leg: {' '.join(command[1:])}", flush=True)
    with tempfile.TemporaryDirectory(prefix="autocore-corpus-") as workspace:
        completed = subprocess.run(command, cwd=workspace)
    if completed.returncode != 0:
        sys.exit(f"corpus: fusesoc run exited {completed.returncode}")
    print("-- verilator leg: OK")


if __name__ == "__main__":
    main()
