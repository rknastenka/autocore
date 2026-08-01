"""Stage 4: Emit.

Turn a resolved project into a `.core` file.

This module is the final step of the pipeline.

It does three related jobs:
1. Convert a `ProjectModel` into a structured `CoreManifest`.
2. Render that manifest into FuseSoC CAPI2 YAML text.
3. Optionally write the rendered text to disk.

Manifest-building and rendering are separate steps so the YAML formatting can
be tested without going through the project-structure logic first.
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

# Each supported language and its emitted CAPI2 file type. Header-like files
# such as `.vh` and `.svh` resolve through `lang_for_path` too, so they use the
# same language-based mapping.
FILE_TYPES: dict[Lang, str] = {
    Lang.VERILOG: "verilogSource",
    Lang.SYSTEMVERILOG: "systemVerilogSource",
    Lang.VHDL: "vhdlSource-2008",
}

# Version used in generated VLNV strings. Callers may override the name and
# library parts, but not this.
DEFAULT_CORE_VERSION = "0.1.0"

# Tool options emitted for the generated `sim` target. Most tool configuration
# is out of scope for autocore; this one is here because a generated
# SystemVerilog testbench runs as a standalone simulation target, and
# Verilator's `binary` mode is what that needs.
SIM_TOOL_OPTIONS: tuple[ToolOption, ...] = (ToolOption("verilator", "mode", "binary"),)

# Characters FuseSoC does not accept in a VLNV part; runs of them fold to a
# single `_`. `cli._vlnv_part` checks user-supplied `--name`/`--library`
# against the same character set, but rejects instead of repairing.
_NOT_VLNV_RE = re.compile(r"[^A-Za-z0-9_.\-]+")

# Indentation column for comments attached to target-level keys, matching the
# fixed YAML structure `_dump` produces.
_TARGET_KEY_INDENT = 4


def to_manifest(
    model: ProjectModel,
    root: Path,
    core_dir: Path | None = None,
    *,
    name: str | None = None,
    library: str | None = None,
) -> CoreManifest:
    """Build the structured CAPI2 manifest for a resolved project.

    This function translates the resolved project model into the data structure
    that will later be rendered as a `.core` file.

    `core_dir` is the directory where the `.core` file will live. All emitted
    file paths are made relative to that location.

    `name` and `library` let the caller override the default VLNV parts. If no
    name is supplied, the project directory name is sanitized and used.
    """
    root = Path(root)
    core_dir = root if core_dir is None else Path(core_dir)

    core_name = name if name is not None else _sanitize_vlnv_part(root.name)
    vlnv = f":{library or ''}:{core_name}:{DEFAULT_CORE_VERSION}"

    rtl_files = tuple(_file_entry(p, model, core_dir) for p in model.compile_order)
    tb_files = tuple(_file_entry(p, model, core_dir) for p in model.tb_compile_order)

    # Skip filesets with no files, and only point targets at the ones that got
    # built, so the manifest carries no empty structural keys.
    filesets: list[Fileset] = []
    if rtl_files:
        filesets.append(Fileset("rtl", rtl_files, file_type=_dominant(rtl_files)))
    if tb_files:
        filesets.append(Fileset("tb", tb_files, file_type=_dominant(tb_files)))
    built = {fs.name for fs in filesets}

    targets = [
        Target(
            name="default",
            filesets=("rtl",) if "rtl" in built else (),
            toplevel=model.top or None,
        )
    ]
    if tb_files and model.tb_top:
        # The sim target needs a usable testbench. It pulls in both filesets;
        # Resolve has already subtracted the rtl set from the tb one, so no
        # file is emitted twice.
        targets.append(
            Target(
                name="sim",
                filesets=tuple(name for name in ("rtl", "tb") if name in built),
                toplevel=model.tb_top,
                default_tool="verilator",
                tools=SIM_TOOL_OPTIONS,
                toplevel_comment=_tb_top_comment(model.tb_top_alternatives),
            )
        )
    return CoreManifest(vlnv=vlnv, filesets=tuple(filesets), targets=tuple(targets))


def _tb_top_comment(alternatives: tuple[str, ...]) -> str | None:
    """Return a YAML warning comment for an auto-chosen simulation top.

    When several candidates were plausible, the generated file says so where
    the user will see it. With no ambiguity there is nothing to warn about.
    """
    if not alternatives:
        return None
    return (
        "autocore: several files could have been the sim toplevel "
        f"(also: {', '.join(alternatives)}).\n"
        "Check this is the testbench you meant."
    )


def _file_entry(path: Path, model: ProjectModel, core_dir: Path) -> FileEntry:
    """Build one emitted file entry relative to the `.core` file location."""
    return FileEntry(
        path=Path(os.path.relpath(path, core_dir)).as_posix(),
        file_type=FILE_TYPES[_lang_of(path)],
        is_include_file=path in model.include_files,
    )


def _dominant(files: tuple[FileEntry, ...]) -> str:
    """Return the most common file type in a fileset.

    This becomes the fileset-level `file_type`. Individual files only need
    their own `file_type` entry later if they differ from this dominant value.
    """
    counts = Counter(entry.file_type for entry in files)
    return min(counts, key=lambda ft: (-counts[ft], ft))


def _lang_of(path: Path) -> Lang:
    """Return the language for a scanned source path.

    Every path here comes from the scan stage, so an unsupported extension
    means the emitter was handed something invalid.
    """
    language = lang_for_path(path)
    if language is None:
        raise ValueError(f"not a scanned source extension: {path}")
    return language


def _sanitize_vlnv_part(text: str) -> str:
    """Normalize a default VLNV part to FuseSoC's accepted character set.

    Unsupported character runs collapse to `_`, and a completely unusable name
    falls back to `core` so the VLNV still remains valid.
    """
    cleaned = _NOT_VLNV_RE.sub("_", text).strip("_")
    return cleaned or "core"


def emit(manifest: CoreManifest) -> str:
    """Render a structured manifest as the full text of a `.core` file.

    The result starts with the required `CAPI=2:` line, followed by a generated
    header comment and the YAML body.

    Pure: no file access and no global mutable state.
    """
    document = CommentedMap()
    document["name"] = manifest.vlnv

    filesets = CommentedMap()
    for fs in manifest.filesets:
        rendered = _fileset_yaml(fs)
        if rendered:  # an empty fileset would render as an empty map
            filesets[fs.name] = rendered
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
    """Render one file item for a CAPI2 files list.

    Files needing no extra attributes emit as a plain path; the rest emit as a
    mapping carrying only the attributes that apply.
    """
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
        # Targets sit two mapping levels deep under `targets:`, and ruamel
        # needs that column spelled out for an attached comment.
        rendered.yaml_set_comment_before_after_key(
            "toplevel", before=target.toplevel_comment, indent=_TARGET_KEY_INDENT
        )
    return rendered


def _tools_yaml(options: tuple[ToolOption, ...]) -> CommentedMap:
    """Group flat tool options into the nested CAPI2 `tools:` structure.

    Supplied order is preserved, which keeps the emitted output stable.
    """
    rendered = CommentedMap()
    for opt in options:
        rendered.setdefault(opt.tool, CommentedMap())[opt.key] = opt.value
    return rendered


def _dump(document: CommentedMap) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    buffer = io.StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


def write_core(text: str, path: Path, *, force: bool = False) -> None:
    """Write `text` to `path` with fixed ``\\n`` newlines.

    The only write site in autocore. An existing file is never overwritten
    without `force`, and the refusal raises so a caller cannot miss it.
    """
    path = Path(path)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists; autocore will not overwrite it "
            "(use --force to allow it)"
        )
    path.write_text(text, encoding="utf-8", newline="\n")
