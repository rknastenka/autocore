"""Stage 4: Emit.

Three functions, in pipeline order:

* `to_manifest` turns a `ProjectModel` into a `CoreManifest`. Fileset
  structure, file types and the VLNV are CAPI2 concerns, so they are decided
  here rather than in Resolve, and separately from the rendering below so the
  pure string half stays independently testable. The ``tb`` fileset and the
  ``sim`` target appear if and only if Resolve found a testbench top and files
  to put under it.
* `emit` is the pure `CoreManifest -> str` half: ruamel.yaml under a fixed
  header, first line exactly ``CAPI=2:``. Empty structural keys are omitted
  entirely: no ``depend: []``, no empty maps.
* `write_core` is the one write site: fixed ``\\n`` newlines, and it refuses
  to overwrite an existing ``.core`` unless forced. That refusal is the
  one-shot promise.

Include directories are not emitted as a fileset-level ``include_dirs`` key,
because CAPI2 has no such key: fusesoc 2.4.6's strict loader rejects it.
FuseSoC derives the include path itself, from the directory containing each
file flagged ``is_include_file: true``, and those are exactly the directories
`ProjectModel.include_dirs` holds, so nothing is lost.

Output is byte-identical for equal input. Everything ordered here comes from
fields the producer already ordered (`compile_order`) or goes through
`sorted()`, and `emit` builds a fresh ruamel instance per call.
"""

from __future__ import annotations

import io
import os
import re
from collections import Counter
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from autocore import __version__
from autocore.models import (
    CoreManifest,
    FileEntry,
    Fileset,
    Lang,
    ProjectModel,
    Target,
    ToolOption,
    lang_for_path,
)

__all__ = [
    "DEFAULT_CORE_VERSION",
    "FILE_TYPES",
    "SIM_TOOL_OPTIONS",
    "emit",
    "to_manifest",
    "write_core",
]

#: Language (by extension) to CAPI2 file type. `.vh` and `.svh` reach this
#: through `lang_for_path`, as Verilog and SystemVerilog respectively.
FILE_TYPES: dict[Lang, str] = {
    Lang.VERILOG: "verilogSource",
    Lang.SYSTEMVERILOG: "systemVerilogSource",
    Lang.VHDL: "vhdlSource-2008",
}

#: The version part of the default VLNV. `--name`/`--library` override the
#: middle parts; nothing overrides this one.
DEFAULT_CORE_VERSION = "0.1.0"

#: The one tool option auto-core emits, on the ``sim`` target only. Tool
#: options are otherwise out of scope, but without this one edalize defaults
#: Verilator to ``cc`` mode, which verilates the design into a C++ library and
#: then fails at link for want of a ``main()`` that only a hand-written driver
#: could supply. A sim target generated from an RTL tree with no such driver
#: in it could never run. ``binary`` is Verilator's own answer: it builds a
#: self-contained executable from the SystemVerilog testbench, which is
#: exactly the kind of testbench the classification rule picks out.
SIM_TOOL_OPTIONS: tuple[ToolOption, ...] = (ToolOption("verilator", "mode", "binary"),)

#: What fusesoc's `Vlnv` accepts per part. Anything else in a directory name
#: is folded to ``_`` by `_sanitize_vlnv_part`.
_VLNV_PART_RE = re.compile(r"[^A-Za-z0-9_.\-]+")

#: Column a target's own keys sit at: ``targets:`` then the target name, both
#: at the two-space mapping indent `_dump` fixes.
_TARGET_KEY_INDENT = 4


# --------------------------------------------------------------------------
# manifest assembly: ProjectModel -> CoreManifest
# --------------------------------------------------------------------------


def to_manifest(
    model: ProjectModel,
    root: Path,
    core_dir: Path | None = None,
    *,
    name: str | None = None,
    library: str | None = None,
) -> CoreManifest:
    """Assemble the CAPI2 manifest for `model`, scanned from `root`.

    `core_dir` is the directory the ``.core`` file will live in. Every file
    path is emitted relative to it, with POSIX separators. It defaults to
    `root`, where the default output lands.

    `name` and `library` are the ``--name``/``--library`` overrides and are
    trusted verbatim. Only the default, the directory name, is sanitized to
    fusesoc's allowed charset, because a directory can be called anything.
    """
    root = Path(root)
    core_dir = root if core_dir is None else Path(core_dir)

    core_name = name if name is not None else _sanitize_vlnv_part(root.name)
    vlnv = f":{library or ''}:{core_name}:{DEFAULT_CORE_VERSION}"

    rtl_files = tuple(
        _file_entry(path, model, core_dir) for path in model.compile_order
    )
    tb_files = tuple(
        _file_entry(path, model, core_dir) for path in model.tb_compile_order
    )

    # No empty keys, twice over: a fileset with no files is never built, and a
    # target never names one that was not.
    filesets: tuple[Fileset, ...] = ()
    if rtl_files:
        filesets += (Fileset("rtl", files=rtl_files, file_type=_dominant(rtl_files)),)
    if tb_files:
        filesets += (Fileset("tb", files=tb_files, file_type=_dominant(tb_files)),)
    built = {fileset.name for fileset in filesets}

    targets = (
        Target(
            name="default",
            filesets=tuple(name for name in ("rtl",) if name in built),
            toplevel=model.top or None,
        ),
    )
    if tb_files and model.tb_top:
        # The sim target exists only when a testbench does, and it compiles the
        # rtl fileset alongside the tb one. Resolve already subtracted the
        # overlap, so nothing is listed twice. Exactly one sim target with
        # exactly one toplevel, always: when the toplevel was a guess between
        # several benches, the file says so in a comment rather than the
        # manifest offering a choice CAPI2 cannot express.
        targets += (
            Target(
                name="sim",
                filesets=tuple(name for name in ("rtl", "tb") if name in built),
                toplevel=model.tb_top,
                default_tool="verilator",
                tools=SIM_TOOL_OPTIONS,
                toplevel_comment=_tb_top_comment(model.tb_top_alternatives),
            ),
        )
    return CoreManifest(vlnv=vlnv, filesets=filesets, targets=targets)


def _tb_top_comment(alternatives: tuple[str, ...]) -> str | None:
    """The warning comment above ``toplevel``, or `None` when nothing was
    ambiguous.

    `alternatives` arrives sorted from Resolve, so the comment is the same on
    every run. One obvious testbench produces no alternatives and so no
    comment: a tree that was never in doubt must not be told to go and check.
    """
    if not alternatives:
        return None
    return (
        "autocore: several files could have been the sim toplevel "
        f"(also: {', '.join(alternatives)}).\n"
        "Check this is the testbench you meant."
    )


def _file_entry(path: Path, model: ProjectModel, core_dir: Path) -> FileEntry:
    """One compile-order entry, relative to where the ``.core`` will sit."""
    return FileEntry(
        path=Path(os.path.relpath(path, core_dir)).as_posix(),
        file_type=FILE_TYPES[_lang_of(path)],
        is_include_file=path in model.include_files,
    )


def _dominant(files: tuple[FileEntry, ...]) -> str:
    """The fileset-level file type: the most common one across `files`.

    A tie goes to the alphabetically-first type name: arbitrary, but the same
    on every run. `to_manifest` fills `FileEntry.file_type` on every entry;
    `emit` reads dominance back out of the fileset and drops the per-file
    value wherever it matches, so only the odd file out carries one.
    """
    counts = Counter(entry.file_type for entry in files)
    return min(counts, key=lambda file_type: (-counts[file_type], file_type))


def _lang_of(path: Path) -> Lang:
    """The language of a path Scan accepted; unknown extensions cannot occur.

    Scan only collects the extensions in `SOURCE_SUFFIXES`, so failing here
    means a caller fed `to_manifest` paths that never went through the
    pipeline. That is worth a loud failure rather than a guess.
    """
    language = lang_for_path(path)
    if language is None:
        raise ValueError(f"not a scanned source extension: {path}")
    return language


def _sanitize_vlnv_part(text: str) -> str:
    """Fold a directory name into fusesoc's ``[A-Za-z0-9_.-]`` charset.

    Runs of disallowed characters become one ``_``; sanitization artefacts at
    the ends are stripped. A name with nothing salvageable falls back to
    ``core``, because an empty VLNV name part fails fusesoc's parser.
    """
    cleaned = _VLNV_PART_RE.sub("_", text).strip("_")
    return cleaned or "core"


# --------------------------------------------------------------------------
# rendering: CoreManifest -> str
# --------------------------------------------------------------------------


def emit(manifest: CoreManifest) -> str:
    """Render `manifest` as the full text of a ``.core`` file.

    First line exactly ``CAPI=2:``, then the generated-by comment, then the
    YAML body. Pure: no filesystem, no global state, byte-identical output
    for equal manifests.
    """
    document = CommentedMap()
    document["name"] = manifest.vlnv

    filesets = CommentedMap()
    for fileset in manifest.filesets:
        rendered = _fileset_yaml(fileset)
        if rendered:  # an empty fileset would render as an empty map
            filesets[fileset.name] = rendered
    if filesets:
        document["filesets"] = filesets

    targets = CommentedMap()
    for target in manifest.targets:
        rendered = _target_yaml(target)
        if rendered:
            targets[target.name] = rendered
    if targets:
        document["targets"] = targets

    header = (
        "CAPI=2:\n"
        f"# Generated by autocore v{__version__}. Edit freely; "
        "autocore will not touch this file again.\n"
        "\n"
    )
    return header + _dump(document)


def _fileset_yaml(fileset: Fileset) -> CommentedMap:
    rendered = CommentedMap()
    if fileset.files:
        rendered["files"] = [
            _file_yaml(entry, fileset.file_type) for entry in fileset.files
        ]
    if fileset.file_type:
        rendered["file_type"] = fileset.file_type
    return rendered


def _file_yaml(entry: FileEntry, dominant: str | None) -> str | CommentedMap:
    """One files-list item: a bare path unless an attribute earns a map."""
    attributes = CommentedMap()
    if entry.file_type is not None and entry.file_type != dominant:
        attributes["file_type"] = entry.file_type
    if entry.is_include_file:
        attributes["is_include_file"] = True
    if not attributes:
        return entry.path
    item = CommentedMap()
    item[entry.path] = attributes
    return item


def _target_yaml(target: Target) -> CommentedMap:
    rendered = CommentedMap()
    if target.filesets:
        rendered["filesets"] = list(target.filesets)
    if target.toplevel:
        rendered["toplevel"] = target.toplevel
    if target.default_tool:
        rendered["default_tool"] = target.default_tool
    if target.tools:
        rendered["tools"] = _tools_yaml(target.tools)
    if target.toplevel_comment and "toplevel" in rendered:
        # Targets always sit two levels down (``targets: <name>: ...``), so
        # their keys are indented by four, and ruamel has to be told the column.
        rendered.yaml_set_comment_before_after_key(
            "toplevel", before=target.toplevel_comment, indent=_TARGET_KEY_INDENT
        )
    return rendered


def _tools_yaml(options: tuple[ToolOption, ...]) -> CommentedMap:
    """Group flat `ToolOption` entries back into ``{tool: {key: value}}``.

    Insertion order is the entry order, which is fixed in source, so no
    sorting is needed to keep the output stable, and none is wanted: the order
    a tool's options are written in is the order they were decided in.
    """
    rendered = CommentedMap()
    for option in options:
        rendered.setdefault(option.tool, CommentedMap())[option.key] = option.value
    return rendered


def _dump(document: CommentedMap) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    buffer = io.StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# the one write site
# --------------------------------------------------------------------------


def write_core(text: str, path: Path, *, force: bool = False) -> None:
    """Write `text` to `path` with fixed ``\\n`` newlines.

    An existing file is never overwritten unless `force`. Refusing is the
    one-shot promise, so it raises rather than warning.
    """
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists; autocore will not overwrite it "
            "(use --force to allow it)"
        )
    path.write_text(text, encoding="utf-8", newline="\n")
