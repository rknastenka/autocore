"""auto-core: scan an RTL tree, emit one FuseSoC CAPI2 ``.core`` file.

``generate()`` is the library API and the single integration point. The CLI
and any future FuseSoC generator are thin entry points over this one function.

It returns a `GenerateResult` rather than a bare manifest because every entry
point needs more than CAPI2 data: the warnings that go to stderr and the
rendered text that goes to the output file both travel with the manifest.
It does no I/O beyond reading the tree. Where the text lands and where the
warnings print is each entry point's own business.

`regenerate()` is the interactive-layer counterpart. An answered ambiguity
changes what Resolve decides, never what Scan found or what Parse read, so
re-running those two stages would be waste. It takes a finished
`GenerateResult` and new options and re-runs Resolve and Emit alone, producing
a result indistinguishable from one generated with those options from the
start.
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
    """A fatal pipeline-level problem: the run produced nothing usable.

    Deliberately rare, since the pipeline's rule is "warn, never fail". It is
    reserved for requests that cannot be honoured at all, like a forced top
    that no parsed file declares. The CLI maps it to exit code 1.
    """


@dataclass(frozen=True)
class GenerateOptions:
    """The knobs of `generate`, mirroring the CLI flags they back.

    ``core_dir`` is the directory the ``.core`` file will live in, and every
    emitted file path is relative to it. Defaults to the scanned root, which
    is where the default output lands.

    ``tb_globs`` is ``--tb-glob``: when non-empty it *replaces* the built-in
    testbench filename patterns for the run, leaving the evidence half and
    the magic comments untouched.

    ``tb_overrides`` is how an answered `UnclearTbStatus` comes back into
    the pipeline, as ``(path, TbDirective)`` pairs rather than a mapping so
    that these options stay frozen and comparable like every other field.
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
    """Everything one pipeline run produced, and everything it read.

    ``text`` is the complete rendered ``.core`` file. ``model.warnings`` is
    what an entry point prints to stderr. ``manifest`` is the structured
    CAPI2 view for callers that want data rather than text.

    ``root``, ``scanned`` and ``parsed`` are the run's inputs, carried so
    `regenerate` can re-decide without re-reading the tree. They are why an
    interactive entry point costs one filesystem walk and one parse however
    many questions it asks.
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
    """Run Scan -> Parse -> Resolve -> Emit over the tree at ``path``.

    Reads the tree, writes nothing, prints nothing. Raises `GenerateError`
    when ``options.top`` names something no parsed file declares, and
    ``NotADirectoryError`` when ``path`` is not a directory; everything else
    the pipeline can dislike about a tree becomes a `Warning` on the model.
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
    """Re-run Resolve and Emit over `previous`'s tree with new `options`.

    Answering an ambiguity moves nothing upstream of Resolve: which module
    is the toplevel and whether a file is a testbench change what the graph
    *means*, not which files exist or what they declare. So this reuses
    `previous.scanned` and `previous.parsed` verbatim and produces exactly
    what `generate` would have produced with these options from the start.

    ``options.defines`` is the one field it cannot honour, since defines
    are consumed by Parse; a caller changing them must call `generate`
    again.
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
    """The back half of the pipeline, shared by `generate` and `regenerate`."""
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
