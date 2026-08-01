"""Stage 4: Emit.

Turn a resolved project into a `.core` file.

This module is the final step of the pipeline.

It does three related jobs:
1. Convert a `ProjectModel` into a structured `CoreManifest`.
2. Render that manifest into FuseSoC CAPI2 YAML text.
3. Optionally write the rendered text to disk.

The split between manifest-building and rendering is intentional. It keeps the
formatting layer separate from the project-structure logic, which makes both
parts easier to test and reason about.
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

#: Map each supported language to its emitted CAPI2 file type.
#: Header-like files such as `.vh` and `.svh` still resolve through
#: `lang_for_path`, so they use the same language-based mapping.
FILE_TYPES: dict[Lang, str] = {
    Lang.VERILOG: "verilogSource",
    Lang.SYSTEMVERILOG: "systemVerilogSource",
    Lang.VHDL: "vhdlSource-2008",
}

#: Default version used in generated VLNV strings.
#: Callers may override the name and library parts, but not this version here.
DEFAULT_CORE_VERSION = "0.1.0"

#: Tool options emitted for the generated `sim` target.
#:
#: Most tool configuration is intentionally outside autocore's scope. This one
#: exception exists because a generated SystemVerilog testbench is expected to
#: run as a standalone simulation target, and Verilator's `binary` mode is the
#: setting that matches that expectation.
SIM_TOOL_OPTIONS: tuple[ToolOption, ...] = (ToolOption("verilator", "mode", "binary"),)

#: Allowed character pattern for one VLNV part.
#: Any unsupported characters in a default name are folded to `_`.
_VLNV_PART_RE = re.compile(r"[^A-Za-z0-9_.\-]+")

#: Indentation column for comments attached to target-level keys.
#: This matches the fixed YAML structure used by `_dump`.
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

    rtl_files = tuple(
        _file_entry(path, model, core_dir) for path in model.compile_order
    )
    tb_files = tuple(
        _file_entry(path, model, core_dir) for path in model.tb_compile_order
    )

    # Only create filesets that actually contain files, and only point targets
    # at filesets that were created. This keeps the emitted manifest compact
    # and avoids empty structural keys.
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
        # The simulation target exists only when a usable testbench exists.
        # It includes both RTL and TB filesets, but Resolve has already removed
        # any overlap so files are not emitted twice.
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
    """Return a YAML warning comment for an auto-chosen simulation top.

    If the simulation top was chosen from several plausible candidates, the
    generated file should say so where the user will see it. If there was no
    ambiguity, no comment is needed.
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
    return min(counts, key=lambda file_type: (-counts[file_type], file_type))


def _lang_of(path: Path) -> Lang:
    """Return the language for a scanned source path.

    All paths reaching this function are expected to come from the scan stage.
    If a path has an unsupported extension here, that indicates invalid input
    to the emitter rather than a normal runtime condition.
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
    cleaned = _VLNV_PART_RE.sub("_", text).strip("_")
    return cleaned or "core"


# --------------------------------------------------------------------------
# rendering: CoreManifest -> str
# --------------------------------------------------------------------------


def emit(manifest: CoreManifest) -> str:
    """Render a structured manifest as the full text of a `.core` file.

    The result starts with the required `CAPI=2:` line, followed by a generated
    header comment and the YAML body.

    This function is pure: it does not read files, write files, or depend on
    global mutable state.
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
    """Render one file item for a CAPI2 files list.

    A plain path is used when the file needs no extra attributes. Otherwise,
    the file is emitted as a mapping with only the attributes that matter.
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
        # Targets always appear two mapping levels deep under `targets:`, so
        # ruamel needs the matching indentation column for the attached comment.
        rendered.yaml_set_comment_before_after_key(
            "toplevel", before=target.toplevel_comment, indent=_TARGET_KEY_INDENT
        )
    return rendered


def _tools_yaml(options: tuple[ToolOption, ...]) -> CommentedMap:
    """Group flat tool options into the nested CAPI2 `tools:` structure.

    The order is kept exactly as supplied so emitted output stays stable and so
    the written option order matches the order chosen by the code.
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
