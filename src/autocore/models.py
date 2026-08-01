"""Shared immutable dataclasses for all pipeline stages.

This module defines the common types passed between scan, parse, resolve, and
emit. Keeping these models frozen makes pipeline behavior easier to reason
about and helps preserve deterministic output.

The same input tree has to produce the same output bytes every time. Some
fields are stored as sets for convenience, but sets have no stable iteration
order, so any set that later becomes an ordered output must be sorted first.
Ordered fields here are tuples, which lets each stage make its ordering choice
once and pass it on explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "INCLUDE_SUFFIXES",
    "SOURCE_SUFFIXES",
    "Ambiguity",
    "CoreManifest",
    "FileEntry",
    "FileFacts",
    "Fileset",
    "Lang",
    "MixedLangFileType",
    "MultipleTops",
    "ParseResult",
    "ProjectModel",
    "ScanResult",
    "Target",
    "TbDirective",
    "TbEvidence",
    "ToolOption",
    "TopCandidate",
    "UnclearTbStatus",
    "Warning",
    "is_include_suffix",
    "lang_for_path",
]


class Lang(Enum):
    """Represent the source language of a file, based on its extension."""

    VERILOG = "verilog"
    SYSTEMVERILOG = "systemverilog"
    VHDL = "vhdl"


# Map each supported source-file extension to its language.
# Matching is case-insensitive in `lang_for_path`.
SOURCE_SUFFIXES: Mapping[str, Lang] = MappingProxyType(
    {
        ".v": Lang.VERILOG,
        ".vh": Lang.VERILOG,
        ".sv": Lang.SYSTEMVERILOG,
        ".svh": Lang.SYSTEMVERILOG,
        ".vhd": Lang.VHDL,
        ".vhdl": Lang.VHDL,
    }
)
# Header-like extensions that may be used as include files.
# Scan marks them as include candidates first, and Resolve may later demote
# one back into an ordinary source file if it actually declares modules.
INCLUDE_SUFFIXES: frozenset[str] = frozenset({".vh", ".svh"})


def lang_for_path(path: Path | str) -> Lang | None:
    """Return the language implied by a file extension, or `None`."""
    return SOURCE_SUFFIXES.get(Path(path).suffix.lower())


def is_include_suffix(path: Path | str) -> bool:
    """Return whether ``path`` has a header extension (``.vh`` / ``.svh``)."""
    return Path(path).suffix.lower() in INCLUDE_SUFFIXES


@dataclass(frozen=True)
class ScanResult:
    """Store the output of the scan stage.

    This stage only knows which files exist. It does not parse content.

    `files` is sorted by tree-relative POSIX path so the result does not depend
    on filesystem iteration order or checkout location.
    """

    root: Path
    files: tuple[Path, ...] = ()
    include_candidates: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class TbEvidence:
    """Collect testbench clues found during parsing.

    This is evidence only, not a final classification. Resolve combines these
    clues with filename rules and explicit directives to decide whether a file
    is a testbench.

    `strong` means the file matches the main automatic rule on its own.
    `partial` means there is some evidence, but not enough to decide without
    ambiguity.
    """

    has_finish_or_stop: bool = False
    empty_port_list: bool = False
    initial_heavy: bool = False

    @property
    def strong(self) -> bool:
        """Return whether the evidence is strong enough to classify as a testbench.
        ``$finish``/``$stop`` AND a module with an empty port list."""

        return self.has_finish_or_stop and self.empty_port_list

    @property
    def partial(self) -> bool:
        """Some evidence, but not enough to classify on its own."""
        return not self.strong and (
            self.has_finish_or_stop or self.empty_port_list or self.initial_heavy
        )


class TbDirective(Enum):
    """Represent an explicit autocore testbench directive in source comments.

    `TB` and `RTL` come directly from user-written comments.

    `CONFLICTING` is an internal marker used when a file contains both
    directives. Instead of picking one based on comment order, Parse records
    the conflict explicitly so later stages can handle it deterministically.
    """

    TB = "tb"
    RTL = "rtl"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class FileFacts:
    """Output of Parse, one per source file.
    Store everything Parse learned about one source file"""

    path: Path
    language: Lang
    declared: frozenset[str] = frozenset()
    instantiated: frozenset[str] = frozenset()
    imported_pkgs: frozenset[str] = frozenset()
    includes: frozenset[str] = frozenset()
    tb_evidence: TbEvidence = TbEvidence()
    tb_directive: TbDirective | None = None


@dataclass(frozen=True)
class Warning:
    """Represent a non-fatal diagnostic produced by the pipeline.

    Warnings never stop the run. `message` is the short human-facing summary.
    `details` carries any longer extra information that a caller may choose to
    show separately, such as a list of excluded files.

    Details are strings, not paths: producers build them already tree-relative
    and POSIX-separated, so a warning never embeds a checkout location.
    """

    code: str
    message: str
    path: Path | None = None
    details: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, str]:
        """Return the stable `(path, code)` key used to sort warnings."""
        return (self.path.as_posix() if self.path is not None else "", self.code)


@dataclass(frozen=True)
class ParseResult:
    """Store the output of the parse stage.

    `files` contains one entry for each file that produced parse facts.
    `warnings` contains diagnostics for files that did not.

    Together, these account for every file handed over by Scan.
    """

    files: tuple[FileFacts, ...] = ()
    warnings: tuple[Warning, ...] = ()


@dataclass(frozen=True)
class TopCandidate:
    """Describe one possible top module and the size of its dependency closure.

    `closure_size` is how many files the candidate drags behind it, the count
    the automatic fallback compares when the directory-name rule finds no
    match. It travels with the name because whoever is asked to choose needs
    to see what the automatic answer was based on.
    """

    name: str
    closure_size: int


@dataclass(frozen=True)
class MultipleTops:
    """Represent an ambiguity where several RTL top candidates exist.

    `candidates` is ordered exactly as `_detect_top` found them, sorted by
    name, so the prompt built from it is the same on every run.
    """

    candidates: tuple[TopCandidate, ...]

    @property
    def names(self) -> tuple[str, ...]:
        """Return just the candidate names, in their stored order."""
        return tuple(candidate.name for candidate in self.candidates)


@dataclass(frozen=True)
class UnclearTbStatus:
    """Represent a file with inconclusive testbench evidence."""

    path: Path


@dataclass(frozen=True)
class MixedLangFileType:
    """A file whose language disagrees with its fileset's dominant one."""

    path: Path


# The complete set of ambiguity types the pipeline may surface.
Ambiguity = MultipleTops | UnclearTbStatus | MixedLangFileType


@dataclass(frozen=True)
class ProjectModel:
    """Store the fully resolved view of the project.

    This is the main output of the resolve stage. It includes the chosen RTL
    top, compile order, testbench classification, include information,
    warnings, and any ambiguities that may need user input.

    `testbenches` is every file classified as one. `tb_compile_order` is the
    narrower thing Emit needs: the closure of `tb_top` with the rtl set
    subtracted, in dependency order. Without that subtraction a testbench
    instantiating the rtl top would drag the whole rtl closure into the tb
    fileset a second time. Both are empty when the tree has no testbench, which
    suppresses the ``sim`` target.

    `tb_top_alternatives` is the testbench tops that lost, sorted by name, and
    empty when there was nothing to choose between. Either way Emit produces
    one sim target with one toplevel; the alternatives let it note in the file
    that a guess was made.
    """

    files: tuple[FileFacts, ...]
    compile_order: tuple[Path, ...]
    top: str
    rtl: frozenset[Path] = frozenset()
    testbenches: frozenset[Path] = frozenset()
    tb_top: str = ""
    tb_top_alternatives: tuple[str, ...] = ()
    tb_compile_order: tuple[Path, ...] = ()
    include_files: frozenset[Path] = frozenset()
    include_dirs: tuple[Path, ...] = ()
    external_refs: frozenset[str] = frozenset()
    warnings: tuple[Warning, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()


@dataclass(frozen=True)
class FileEntry:
    """Represent one file entry inside a CAPI2 fileset.

    `path` is already relative to the `.core` file location and uses POSIX
    separators, so Emit does not need to do more path rewriting.

    `file_type` may be omitted when the file inherits the fileset's dominant
    type.
    """

    path: str
    file_type: str | None = None
    is_include_file: bool = False


@dataclass(frozen=True)
class Fileset:
    """Represent one CAPI2 fileset.

    Empty keys are omitted at Emit time. There is no include-dirs field
    because CAPI2 filesets have none. FuseSoC derives the include path from
    the directory of each file flagged ``is_include_file``, so include
    directories only appear here through `FileEntry.is_include_file`.
    """

    name: str
    files: tuple[FileEntry, ...] = ()
    file_type: str | None = None


@dataclass(frozen=True)
class ToolOption:
    """Represent one tool-specific option inside a target.

    Most tool configuration is out of scope for autocore. The few options that
    appear here are the ones the project can infer reliably from the source
    tree and needs in order to emit a usable target.

    The `sim` target says `verilator: {mode: binary}`. Edalize otherwise
    defaults Verilator to `cc` mode, and a self-contained SystemVerilog
    testbench with no C++ driver cannot link. Whether the tree has such a
    driver follows from what Parse already knows, so this stays an inference
    and not a config knob.

    Flat rather than nested, so the whole thing stays frozen and hashable.
    Emit groups entries back into `{tool: {key: value}}` in list order.
    """

    tool: str
    key: str
    value: str


@dataclass(frozen=True)
class Target:
    """Represent one CAPI2 target.

    `default` is always present. `sim` is added only when a usable testbench
    target exists.

    `toplevel_comment` is a rendered YAML comment placed above the emitted
    `toplevel` key when autocore had to make a simulation-top guess and wants
    the generated file itself to say so.
    """

    name: str
    filesets: tuple[str, ...] = ()
    toplevel: str | None = None
    default_tool: str | None = None
    tools: tuple[ToolOption, ...] = ()
    toplevel_comment: str | None = None


@dataclass(frozen=True)
class CoreManifest:
    """Store the structured CAPI2 manifest which is an input to Emit."""

    vlnv: str
    filesets: tuple[Fileset, ...] = ()
    targets: tuple[Target, ...] = ()
