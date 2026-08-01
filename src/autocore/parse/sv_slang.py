"""Verilog and SystemVerilog backend, on pyslang 11.

Single-file and syntax-level: one `SyntaxTree` per source file, no elaboration,
no compilation unit shared between files. A module whose name comes from a macro
defined in another file is therefore invisible. That is a stated limitation of
autocore, and `--define` is the escape hatch.

Three details of the pyslang API are worth knowing before touching the code
below:

* `SyntaxTree.fromFile` takes its arguments positionally; keyword arguments
  are rejected.
* Children are reached by index, `len(node)` then `node[i]`, and that order is
  source order, the same in every process.
* `fileName.valueText` on an include directive keeps its delimiters, so
  `` `include "defs.svh" `` reads back as `'"defs.svh"'`. The model wants the
  target as written with quotes stripped, which is `_strip_delimiters`.

Includes are collected along two paths. Resolved ones come from
`tree.getIncludeDirectives()`; unresolved ones never appear there, but their
`IncludeDirective` syntax survives in the trivia attached to the next real
token, so the trivia scan recovers those. The union is what a file "includes",
whether or not the header was found. Both paths report transitive includes: a
header that itself includes another header contributes both. That is slang's
behaviour, and it suits us, because Resolve turns these strings into
`include_dirs` and every directory in the chain has to be listed for the
emitted core to compile.

Testbench evidence and magic comments both come out of the same walk. The
three `TbEvidence` bits are structural: a ``$finish``/``$stop`` system call
anywhere, a module declared with an empty port list, and a body whose `initial`
blocks are at least as numerous as its `always` blocks and continuous
assignments. Evidence only; classification happens in Resolve.
`` // autocore: tb `` and `` // autocore: rtl `` are user directives rather
than evidence, and they ride the same trivia scan the unresolved includes do.
One wrinkle: slang folds a comment written directly above a preprocessor
directive into that directive's trivia, where it never reaches a real token,
so the scan recurses through directive syntax. Testbenches commonly open with
a magic comment above `` `timescale ``.

Include search paths are a backend property, not a per-file argument.
`SvSlangBackend.for_tree` seeds them from the header directories Scan already
found. Without that, a tree that keeps its headers in `include/` and its sources
in `rtl/` would fail to resolve every `` `include ``, every macro from those
headers would become an `UnknownDirective` error, and every file in the tree
would be classified as unparseable. Reading a header a file explicitly asked
for does not amount to sharing a compilation unit, so the single-file rule
still holds.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pyslang
from pyslang.parsing import PreprocessorOptions, Token, TokenKind, TriviaKind
from pyslang.syntax import SyntaxKind, SyntaxNode, SyntaxTree

from autocore.models import (
    FileFacts,
    Lang,
    ScanResult,
    TbDirective,
    TbEvidence,
    lang_for_path,
)
from autocore.parse.base import ParseError

__all__ = [
    "MAGIC_COMMENT_RE",
    "MAX_REPORTED_DIAGNOSTICS",
    "SUPPORTED_LANGUAGES",
    "TOLERATED_DIAGNOSTICS",
    "SvSlangBackend",
]

# What this backend reads. `.vh` is Verilog and `.svh` is SystemVerilog by
# extension; slang parses both the same way.
SUPPORTED_LANGUAGES: frozenset[Lang] = frozenset({Lang.VERILOG, Lang.SYSTEMVERILOG})

# Error-severity diagnostics that do not mean "this file failed to parse".
#
# `CouldNotOpenIncludeFile` is the only one. pyslang reports it at error
# severity, but it is recoverable: parsing continues past a missing header,
# and Resolve gets a second chance at the target by matching basenames against
# the scanned tree. Treating it like the other errors would classify every
# file with an out-of-tree header as unparseable, and severity alone cannot
# tell the two apart, so the exception is by name.
TOLERATED_DIAGNOSTICS: frozenset[Any] = frozenset(
    {pyslang.Diags.CouldNotOpenIncludeFile}
)

# Diagnostics quoted in a `ParseFailed` warning before it says "and N more".
MAX_REPORTED_DIAGNOSTICS = 3

_FATAL_SEVERITIES = frozenset(
    {pyslang.DiagnosticSeverity.Error, pyslang.DiagnosticSeverity.Fatal}
)

# The three kinds that carry a name other files can refer to.
_DECLARATION_KINDS = frozenset(
    {
        SyntaxKind.ModuleDeclaration,
        SyntaxKind.InterfaceDeclaration,
        SyntaxKind.PackageDeclaration,
    }
)

# `` `include "x" `` versus `` `include <x> ``.
_DELIMITERS = (('"', '"'), ("<", ">"))

# The magic comments. Forgiving about spacing, case, and which comment syntax
# carries it, so `` // autocore: tb ``, `` //autocore:TB `` and
# `` /* autocore : rtl */ `` all count. The keyword and the verdict have to be
# adjacent, so prose mentioning autocore never reclassifies a file by accident.
MAGIC_COMMENT_RE = re.compile(r"\bautocore\s*:\s*(tb|rtl)\b", re.IGNORECASE)

# Trivia that can carry a magic comment.
_COMMENT_TRIVIA = frozenset({TriviaKind.LineComment, TriviaKind.BlockComment})

# The task-control calls that make up half of the evidence branch.
_FINISH_TASKS = frozenset({"$finish", "$stop"})

# `initial` blocks weigh against these. Instantiations are not in the list: a
# testbench instantiates its DUT, so counting instances would penalise exactly
# the files this evidence is meant to notice.
_STRUCTURAL_KINDS = frozenset(
    {
        SyntaxKind.AlwaysBlock,
        SyntaxKind.AlwaysCombBlock,
        SyntaxKind.AlwaysFFBlock,
        SyntaxKind.AlwaysLatchBlock,
        SyntaxKind.ContinuousAssign,
    }
)


@dataclass(frozen=True)
class SvSlangBackend:
    """The Verilog and SystemVerilog `ParserBackend`.

    Frozen and holding nothing but strings and paths, because `parse_all` ships
    it to worker processes.
    """

    # Directories searched for `` `include `` targets, in order. Sorted by
    # `for_tree`, so two headers with the same basename resolve the same way on
    # every run.
    include_dirs: tuple[Path, ...] = ()

    languages: ClassVar[frozenset[Lang]] = SUPPORTED_LANGUAGES

    @classmethod
    def for_tree(cls, scan: ScanResult) -> SvSlangBackend:
        """A backend that can find the headers `scan` turned up.

        Every directory holding a `.vh`/`.svh` becomes a search path. This is
        the same set of directories Resolve arrives at as `include_dirs`, from
        the same evidence.
        """
        directories = {path.parent for path in scan.include_candidates}
        return cls(include_dirs=tuple(sorted(directories, key=Path.as_posix)))

    def parse(self, path: Path, defines: Sequence[str] = ()) -> FileFacts:
        """Extract `FileFacts` from one Verilog or SystemVerilog file."""
        path = Path(path)
        language = lang_for_path(path)
        if language is None or language not in self.languages:
            raise ParseError(
                path,
                f"{path.suffix or 'this file'} is not Verilog or SystemVerilog",
                code="UnsupportedLanguage",
            )

        tree = self._syntax_tree(path, defines)
        self._reject_broken(path, tree)

        found = _walk(tree.root)
        for directive in tree.getIncludeDirectives():
            _add(found.includes, _strip_delimiters(directive.syntax.fileName.valueText))

        return FileFacts(
            path=path,
            language=language,
            declared=frozenset(found.declared),
            instantiated=frozenset(found.instantiated),
            imported_pkgs=frozenset(found.imported),
            includes=frozenset(found.includes),
            tb_evidence=found.evidence(),
            tb_directive=found.directive(),
        )

    def _syntax_tree(self, path: Path, defines: Sequence[str]) -> SyntaxTree:
        """One parsed file. The argument shape is what pyslang 11 accepts."""
        source_manager = pyslang.SourceManager()
        bag = pyslang.Bag()
        options = PreprocessorOptions()
        options.predefines = list(defines)  # `--define NAME[=VAL]`, verbatim
        if self.include_dirs:
            options.additionalIncludePaths = [str(d) for d in self.include_dirs]
        bag.preprocessorOptions = options
        return SyntaxTree.fromFile(str(path), source_manager, bag)  # positional

    @staticmethod
    def _reject_broken(path: Path, tree: SyntaxTree) -> None:
        """Raise `ParseError` if the file has real errors, not just diagnostics.

        pyslang always hands back a tree, so "failed to parse" has to be decided
        from `tree.diagnostics`. The rule: any error-severity diagnostic that is
        not in `TOLERATED_DIAGNOSTICS` means the file is excluded from the graph.
        Warnings and notes are ignored; plenty of clean RTL produces them.
        """
        engine = pyslang.DiagnosticEngine(tree.sourceManager)
        messages = [
            engine.formatMessage(diagnostic)
            for diagnostic in tree.diagnostics
            if diagnostic.code not in TOLERATED_DIAGNOSTICS
            and engine.getSeverity(diagnostic.code, diagnostic.location)
            in _FATAL_SEVERITIES
        ]
        if messages:
            raise ParseError(path, _summarise(messages))


@dataclass
class _Found:
    """Everything one pass over a syntax tree produces.

    The counters and the directive set are tallies. `evidence` and `directive`
    are the only places a judgement is made, and neither depends on the order
    the walk visited anything in.
    """

    declared: set[str] = field(default_factory=set)
    instantiated: set[str] = field(default_factory=set)
    imported: set[str] = field(default_factory=set)
    includes: set[str] = field(default_factory=set)
    directives: set[TbDirective] = field(default_factory=set)
    has_finish_or_stop: bool = False
    empty_port_list: bool = False
    initial_blocks: int = 0
    structural_blocks: int = 0

    def evidence(self) -> TbEvidence:
        """The three evidence bits, read off the tallies."""
        return TbEvidence(
            has_finish_or_stop=self.has_finish_or_stop,
            empty_port_list=self.empty_port_list,
            # "initial-heavy": at least one `initial`, and at least as many of
            # them as everything a synthesisable body is made of. A testbench
            # with one stimulus block and one clock generator passes; RTL with
            # a single power-on `initial` among several `always` does not.
            initial_heavy=(
                self.initial_blocks > 0
                and self.initial_blocks >= self.structural_blocks
            ),
        )

    def directive(self) -> TbDirective | None:
        """The magic comment, or `CONFLICTING` when the file carries both."""
        if len(self.directives) > 1:
            return TbDirective.CONFLICTING
        return next(iter(self.directives), None)


def _walk(root: SyntaxNode) -> _Found:
    """Collect every fact the pipeline needs, in one pass.

    Iterative rather than recursive: nesting depth follows expression depth in
    the source, and real RTL gets deep enough to be worth not betting the
    interpreter's stack on.
    """
    found = _Found()

    stack: list[SyntaxNode | Token | None] = [root]
    while stack:
        node = stack.pop()
        if node is None:  # optional child slots come back as None
            continue

        if isinstance(node, Token):
            if node.kind == TokenKind.SystemIdentifier:
                if node.valueText in _FINISH_TASKS:
                    found.has_finish_or_stop = True
            _scan_trivia(node, found)
            continue
        if not isinstance(node, SyntaxNode):
            continue

        kind = node.kind
        if kind in _DECLARATION_KINDS:
            _add(found.declared, node.header.name.valueText)
            if kind == SyntaxKind.ModuleDeclaration and _ports_are_empty(node.header):
                found.empty_port_list = True
        elif kind == SyntaxKind.HierarchyInstantiation:
            # Pre-elaboration, so this fires inside `generate` blocks too, and
            # an `ifdef`-guarded instantiation appears only when the define is
            # active.
            _add(found.instantiated, node.type.valueText)
        elif kind == SyntaxKind.PackageImportDeclaration:
            for item in node.items:
                # `items` is a separated list: the commas are in there too.
                if getattr(item, "kind", None) == SyntaxKind.PackageImportItem:
                    _add(found.imported, item.package.valueText)
        elif kind == SyntaxKind.InitialBlock:
            found.initial_blocks += 1
        elif kind in _STRUCTURAL_KINDS:
            found.structural_blocks += 1

        for index in range(len(node)):
            stack.append(node[index])

    return found


def _ports_are_empty(header: Any) -> bool:
    """Whether a module header declares no ports at all.

    Both spellings count: `` module tb; `` leaves `ports` unset, and
    `` module tb (); `` builds a port list holding nothing but its two
    parentheses. Anything with a real port in it is a child node rather than
    a token, which is the distinction tested here.
    """
    ports = header.ports
    if ports is None:
        return True
    return not any(isinstance(ports[index], SyntaxNode) for index in range(len(ports)))


def _scan_trivia(token: Token, found: _Found) -> None:
    """Read one token's trivia for include targets and magic comments.

    Two things hide here. Unresolved includes: when slang cannot open the file
    there is nothing to splice in, so the directive stays behind as trivia on
    the following token. And magic comments, including the ones that never
    reach a real token at all, because slang folds a comment written directly
    above a preprocessor directive into that directive's trivia. Recursing
    through directive syntax is the only way to reach those, which matters
    because testbenches commonly put their header comment above `` `timescale ``.
    """
    for trivia in token.trivia:
        if trivia.kind in _COMMENT_TRIVIA:
            match = MAGIC_COMMENT_RE.search(trivia.getRawText())
            if match:
                found.directives.add(TbDirective(match.group(1).lower()))
        elif trivia.kind == TriviaKind.Directive:
            syntax = trivia.syntax()
            if syntax is None:
                continue
            if syntax.kind == SyntaxKind.IncludeDirective:
                _add(found.includes, _strip_delimiters(syntax.fileName.valueText))
            for nested in _tokens_of(syntax):
                _scan_trivia(nested, found)


def _tokens_of(root: SyntaxNode) -> Iterator[Token]:
    """Every token under `root`, in no particular order: callers only tally."""
    stack: list[SyntaxNode | Token | None] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Token):
            yield node
        elif isinstance(node, SyntaxNode):
            for index in range(len(node)):
                stack.append(node[index])


def _strip_delimiters(text: str) -> str:
    """`'"defs.svh"'` and `'<defs.svh>'` both become `'defs.svh'`."""
    text = text.strip()
    for opener, closer in _DELIMITERS:
        if len(text) >= 2 and text.startswith(opener) and text.endswith(closer):
            return text[1:-1]
    return text


def _add(target: set[str], name: str) -> None:
    """Add `name` unless error recovery produced an empty identifier."""
    if name:
        target.add(name)


def _summarise(messages: Sequence[str]) -> str:
    """A stable one-line digest of a file's error diagnostics.

    Source order, capped, and free of file paths and line numbers. The message
    ends up in a `Warning`, which must read the same from any checkout.
    """
    head = "; ".join(messages[:MAX_REPORTED_DIAGNOSTICS])
    extra = len(messages) - MAX_REPORTED_DIAGNOSTICS
    return f"{head} (and {extra} more)" if extra > 0 else head
