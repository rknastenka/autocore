"""auto-core: scan an RTL tree and produce a single FuseSoC CAPI2 `.core` file.

This module is the main public entry point for the library.

`generate()` runs the full pipeline and returns everything a caller may need:
the structured manifest, the rendered `.core` text, and any warnings collected
along the way. The CLI and any future integration layers should stay thin and
build on top of this API instead of re-implementing pipeline logic.

`regenerate()` is the lightweight companion to `generate()`. It reuses an
existing scan/parse result and only re-runs the later stages when the caller
changes options that affect resolution or emission. This is mainly useful for
interactive flows, where a user answers a question and the project needs to be
re-resolved without re-reading the whole tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autocore.models import (
    CoreManifest,
    ParseResult,
    ProjectModel,
    ScanResult,
    TbDirective,
)

__all__ = [
    "GenerateError",
    "GenerateOptions",
    "GenerateResult",
    "__version__",
    "generate",
    "regenerate",
]

__version__ = "0.1.0"


class GenerateError(Exception):
    """Raised when a requested pipeline action cannot be completed at all.

    This should be rare. The general rule of the pipeline is to keep going and
    collect warnings instead of failing. This exception is reserved for cases
    where the request itself cannot be honored, such as forcing a top module
    that does not exist in the parsed tree.
    """


@dataclass(frozen=True)
class GenerateOptions:
    """Options that control one `generate()` or `regenerate()` run.

    These fields mirror the choices a caller or CLI user can make, such as the
    core name, library name, top module, preprocessor defines, and testbench
    classification overrides.

    `core_dir` is the directory where the `.core` file will live. Emitted file
    paths are made relative to that location.

    `tb_globs` replaces the built-in filename patterns used to recognize
    testbenches for this run.

    `tb_overrides` is mainly for interactive use. It lets a caller feed an
    explicit testbench/RTL decision back into the pipeline for specific files.
    """

    name: str | None = None
    library: str | None = None
    top: str | None = None
    defines: tuple[str, ...] = ()
    tb_globs: tuple[str, ...] = ()
    tb_overrides: tuple[tuple[Path, TbDirective], ...] = ()
    core_dir: Path | None = None


@dataclass(frozen=True)
class GenerateResult:
    """The full result of one pipeline run.

    This includes:
    - `manifest`: the structured CAPI2 representation
    - `text`: the rendered `.core` file contents
    - `model`: the resolved project model, including warnings

    It also keeps the original inputs to the later stages (`root`, `scanned`,
    and `parsed`) so the caller can re-run resolution and emission without
    scanning or parsing the tree again.
    """

    model: ProjectModel
    manifest: CoreManifest
    text: str
    root: Path
    scanned: ScanResult
    parsed: ParseResult


def generate(
    path: Path | str, options: GenerateOptions | None = None
) -> GenerateResult:
    """Run the full pipeline for the RTL tree at `path`.

    pipeline: Scan -> Parse -> Resolve -> Emit.

    This function scans the tree, parses the source files, resolves the project
    structure, and renders the final `.core` output.

    It does not write files or print warnings by itself. Those responsibilities
    belong to the caller, such as the CLI.

    Raises:
        GenerateError: If `options.top` names a module that is not declared by
            any parsed file.
        NotADirectoryError: If `path` is not a directory.
    """
    # Imported here rather than at module top for two reasons: `emit` reads
    # `__version__` back from this partially-initialised package, and the
    # pyslang import behind `parse` is too heavy a toll on `autocore --version`.
    from autocore.parse import parse_sources
    from autocore.scan import scan

    options = GenerateOptions() if options is None else options
    root = Path(path)

    tree = scan(root)
    parsed = parse_sources(tree, defines=options.defines)
    return _resolve_and_emit(root, tree, parsed, options)


def regenerate(
    previous: GenerateResult, options: GenerateOptions | None = None
) -> GenerateResult:
    """Re-run only the later pipeline stages(Resolve/Emit) using a previous result.

    This is useful when scan/parse data is still valid, but the caller wants to
    change resolution-time options such as top selection or testbench
    classification.

    It reuses the previous scan and parse outputs and only runs resolve + emit
    again. If the caller changes parse-time options such as `defines`, they
    should call `generate()` instead.
    """

    return _resolve_and_emit(
        previous.root,
        previous.scanned,
        previous.parsed,
        GenerateOptions() if options is None else options,
    )


def _resolve_and_emit(
    root: Path,
    tree: ScanResult,
    parsed: ParseResult,
    options: GenerateOptions,
) -> GenerateResult:
    """Run the shared resolve + emit part of the pipeline.

    Both `generate()` and `regenerate()` end up here. This helper validates any
    forced top selection, builds the resolved project model, converts it into a
    manifest, and renders the final `.core` text.
    """
    from autocore.emit import emit, to_manifest
    from autocore.resolve import resolve

    if options.top is not None and not any(
        options.top in facts.declared for facts in parsed.files
    ):
        raise GenerateError(
            f"top '{options.top}' is not declared by any parsed file in the tree"
        )

    model = resolve(
        tree,
        parsed,
        top=options.top,
        tb_globs=options.tb_globs,
        tb_overrides=dict(options.tb_overrides),
    )
    manifest = to_manifest(
        model,
        root,
        core_dir=options.core_dir,
        name=options.name,
        library=options.library,
    )
    return GenerateResult(
        model=model,
        manifest=manifest,
        text=emit(manifest),
        root=root,
        scanned=tree,
        parsed=parsed,
    )
