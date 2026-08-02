"""Frozen dataclasses shared by the four stages.

Every type here is frozen and hashable. No parser type may leak past Parse:
``sv_slang`` turns pyslang objects into :class:`FileFacts` and nothing further
down the pipeline ever sees a syntax node.

auto-core promises byte-identical output for an identical input tree. Several
fields here are ``frozenset``, whose iteration order varies with the process
hash seed, so a set that reached the output unsorted would make the same tree
emit different bytes from one run to the next. The rule that prevents it:
every set-to-sequence conversion downstream goes through ``sorted()``. The
ordered fields here (``files``, ``compile_order``, ``include_dirs``,
``warnings``, ``ambiguities``) are tuples precisely so that the ordering
decision is made once, by the producer, and then frozen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """Source language of a file, decided by extension."""

    VERILOG = "verilog"
    SYSTEMVERILOG = "systemverilog"
    VHDL = "vhdl"


#: The complete set of extensions Scan collects, mapped to their language.
#: Compared case-insensitively, in :func:`lang_for_path`.
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

#: Header extensions. These are include candidates: Scan flags them, and
#: Resolve demotes one back to an ordinary source file if it declares modules.
INCLUDE_SUFFIXES: frozenset[str] = frozenset({".vh", ".svh"})


def lang_for_path(path: Path | str) -> Lang | None:
    """Return the language implied by ``path``'s extension, or ``None``."""
    return SOURCE_SUFFIXES.get(Path(path).suffix.lower())


def is_include_suffix(path: Path | str) -> bool:
    """Return whether ``path`` has a header extension (``.vh`` / ``.svh``)."""
    return Path(path).suffix.lower() in INCLUDE_SUFFIXES


@dataclass(frozen=True)
class ScanResult:
    """Output of Scan. Nothing in here has been parsed.

    ``files`` is sorted by path relative to ``root``, POSIX-style, so the order
    is independent of both filesystem readdir order and where the tree happens
    to be checked out.
    """

    root: Path
    files: tuple[Path, ...] = ()
    include_candidates: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class TbEvidence:
    """Evidence for the testbench rule, as gathered by Parse.

    Evidence only: the parser classifies nothing. The two properties below are
    how Resolve reads the rule off this data:

    * `strong` classifies on its own: ``$finish``/``$stop`` *and* a module
      with an empty port list.
    * `partial` is some evidence but not enough, which Resolve turns into an
      `UnclearTbStatus`.

    The three fields are file-wide, because `FileFacts` is per file: a file
    counts as having an empty port list if *any* module in it does.
    """

    has_finish_or_stop: bool = False
    empty_port_list: bool = False
    initial_heavy: bool = False

    @property
    def strong(self) -> bool:
        """``$finish``/``$stop`` AND a module with an empty port list."""
        return self.has_finish_or_stop and self.empty_port_list

    @property
    def partial(self) -> bool:
        """Some evidence, but not enough to classify on its own."""
        return not self.strong and (
            self.has_finish_or_stop or self.empty_port_list or self.initial_heavy
        )


class TbDirective(Enum):
    """A magic comment: ``// autocore: tb`` or ``// autocore: rtl``.

    A user directive, not evidence, which is why it sits beside `TbEvidence`
    on `FileFacts` rather than inside it: evidence is weighed, a directive
    wins outright, in both directions.

    `CONFLICTING` is not something anyone writes. It is what Parse records
    when one file carries both directives. A first-wins rule would make the
    answer depend on which comment the walk reached first, and the walk is not
    something a user can see; recording the contradiction instead keeps the
    result the same however the file is laid out, and lets Resolve say so out
    loud before falling back to the ordinary rules.
    """

    TB = "tb"
    RTL = "rtl"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class FileFacts:
    """Output of Parse, one per source file."""

    path: Path
    language: Lang
    declared: frozenset[str] = frozenset()
    instantiated: frozenset[str] = frozenset()
    imported_pkgs: frozenset[str] = frozenset()
    includes: frozenset[str] = frozenset()
    tb_evidence: TbEvidence = TbEvidence()
    tb_directive: TbDirective | None = None


@dataclass(frozen=True)
class Warning:  # shadows the builtin deliberately
    """A non-fatal diagnostic. Never aborts the pipeline.

    `message` is one line, always: it is what a warning says when it is the
    only thing the user reads. `details` is the long tail some warnings carry,
    the 39 file names behind "39 files were excluded", kept out of the message
    so that an entry point can show the summary and hold the list back until
    ``-v`` asks for it. Nothing is lost either way: the count lives in the
    message, the names live here, and both are reachable from the CLI.

    Details are strings rather than paths because they are already
    tree-relative and POSIX-separated when a producer builds them. A warning
    never embeds the absolute location of a checkout.
    """

    code: str
    message: str
    path: Path | None = None
    details: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, str]:
        """The ``(path, code)`` key ``ProjectModel.warnings`` is sorted by."""
        return (self.path.as_posix() if self.path is not None else "", self.code)


@dataclass(frozen=True)
class ParseResult:
    """Output of Parse, the counterpart to `ScanResult`.

    `FileFacts` is per file and has nowhere to record a file that produced no
    facts at all, so the stage-level result carries both halves: `files` holds
    one entry per file that parsed, `warnings` one per file that did not. The
    two are disjoint, and together they account for every file Scan handed
    over.

    Both are ordered by their producer, once: `files` by POSIX path, `warnings`
    by `Warning.sort_key`. Resolve concatenates the warnings into
    `ProjectModel.warnings` without re-sorting anything.
    """

    files: tuple[FileFacts, ...] = ()
    warnings: tuple[Warning, ...] = ()


@dataclass(frozen=True)
class TopCandidate:
    """One contender for the toplevel, with the number that ranked it.

    `closure_size` is how many files the candidate drags behind it, the count
    the automatic fallback compares when the directory-name rule finds no
    match. It travels with the name because whoever is asked to choose needs
    to see what the automatic answer was based on.
    """

    name: str
    closure_size: int


@dataclass(frozen=True)
class MultipleTops:
    """Several RTL modules are instantiated by nobody.

    `candidates` is ordered exactly as `_detect_top` found them, sorted by
    name, so the prompt built from it is the same on every run.
    """

    candidates: tuple[TopCandidate, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(candidate.name for candidate in self.candidates)


@dataclass(frozen=True)
class UnclearTbStatus:
    """Partial testbench evidence that trips neither branch of the rule."""

    path: Path


@dataclass(frozen=True)
class MixedLangFileType:
    """A file whose language disagrees with its fileset's dominant one."""

    path: Path


#: The only three prompt triggers, encoded as data.
Ambiguity = MultipleTops | UnclearTbStatus | MixedLangFileType


@dataclass(frozen=True)
class ProjectModel:
    """Output of Resolve.

    `testbenches` is every file classified as one; `tb_compile_order` is the
    narrower thing Emit needs, the closure of `tb_top` *minus* the rtl set, in
    dependency order. The subtraction is why the two differ: a testbench
    instantiating the rtl top must not drag the whole rtl closure into the tb
    fileset a second time. Both are empty when the tree has no testbench, and
    that is what suppresses the ``sim`` target.

    `tb_top_alternatives` is the testbench tops that lost, sorted by name, and
    empty whenever the choice was not a choice at all. Exactly one sim target
    with exactly one toplevel is emitted either way; the alternatives exist so
    Emit can say in the file itself that a guess was made.
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
    """One file inside a fileset.

    ``path`` is already relative to the ``.core`` file and POSIX-separated, so
    Emit does no path arithmetic. ``file_type`` is ``None`` when the file
    inherits its fileset's dominant type; it is set only for the odd file out.
    """

    path: str
    file_type: str | None = None
    is_include_file: bool = False


@dataclass(frozen=True)
class Fileset:
    """A CAPI2 fileset.

    Empty keys are omitted at Emit time. There is no include-dirs field
    because CAPI2 filesets have none: FuseSoC derives the include path from
    the directory of each file flagged ``is_include_file``, so include
    directories appear here only implicitly, through
    `FileEntry.is_include_file`.
    """

    name: str
    files: tuple[FileEntry, ...] = ()
    file_type: str | None = None


@dataclass(frozen=True)
class ToolOption:
    """One entry of a target's CAPI2 ``tools:`` map.

    Tool options are out of scope for auto-core, with one exception: the
    ``sim`` target says ``verilator: {mode: binary}``, because edalize
    otherwise defaults Verilator to ``cc`` mode and a self-contained
    SystemVerilog testbench with no C++ driver cannot link. Whether the tree
    has such a driver follows from what Parse already knows, which is what
    keeps this an inference rather than a config knob.

    Flat rather than nested so the whole thing stays frozen and hashable;
    Emit groups entries back into ``{tool: {key: value}}`` in list order.
    """

    tool: str
    key: str
    value: str


@dataclass(frozen=True)
class Target:
    """A CAPI2 target: ``default`` always, ``sim`` only when a testbench exists.

    ``toplevel_comment`` is rendered as a YAML comment directly above the
    ``toplevel`` key. It exists for one case: the sim toplevel was picked out
    of several testbench candidates, and the generated file has to say so
    where the reader will see it. The stderr warning is long gone by the time
    anyone opens the ``.core``.
    """

    name: str
    filesets: tuple[str, ...] = ()
    toplevel: str | None = None
    default_tool: str | None = None
    tools: tuple[ToolOption, ...] = ()
    toplevel_comment: str | None = None


@dataclass(frozen=True)
class CoreManifest:
    """Input to Emit; mirrors CAPI2."""

    vlnv: str
    filesets: tuple[Fileset, ...] = field(default_factory=tuple)
    targets: tuple[Target, ...] = field(default_factory=tuple)
