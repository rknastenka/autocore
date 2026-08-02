"""Stage 3: Resolve.

Turns per-file facts into one `ProjectModel`: a symbol table, a dependency
graph, a detected top, the transitive closure of that top as the rtl set, a
compile order, and resolved includes. Hand-rolled adjacency dicts; only a topo
sort and root-finding are needed, so there is no graph library. Nothing here
prompts and nothing here is fatal: every problem becomes a `Warning`, and the
one prompt trigger this stage can produce, `MultipleTops`, is returned as data
with the automatic fallback already applied.

The order of work is dictated by two dependencies between the steps. Include
matching has to happen before top detection and compile order, because the rtl
closure follows include edges and the topo sort runs over them; and the
closure has to exist before `include_files` can be classified, because only
headers inside it are part of the project. So: symbols, reference edges,
testbench classification, include matching, top detection, the rtl closure,
the testbench top and its closure, compile order, and finally the include
classification.

The rtl set is the transitive closure of the detected top, following
instantiation, package-import and include edges. Files that were scanned but
fall outside it are excluded from `ProjectModel.rtl` and reported in one
warning that counts them, with their names on `Warning.details`. A manifest
listing everything scanned would be valid CAPI2 and unbuildable; real trees
keep whole subdirectories of tool-specific stubs that no top reaches, and
there can be dozens, which is why the names are details rather than message.

The tb set is the same idea run a second time, from the testbench top, with
two differences: it does not stop at testbench files (that is the point), and
whatever the rtl set already holds is subtracted from it afterwards. A
testbench instantiating the rtl top reaches the entire rtl closure, and
emitting that twice would be both wrong and loud.

Every set-to-sequence conversion in this module goes through `sorted()`.
Frozenset iteration order varies with the process hash seed, so an unsorted
one reaching anything ordered would make the same tree emit different bytes
from run to run. Edges are built from sorted name sets, the topo sort draws
from a sorted ready list, and the cycle breaker picks its edge from a sorted
edge list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from autocore.models import (
    Ambiguity,
    FileFacts,
    MultipleTops,
    ParseResult,
    ProjectModel,
    ScanResult,
    TbDirective,
    TbEvidence,
    TopCandidate,
    UnclearTbStatus,
    Warning,
)

__all__ = ["TB_FILENAME_PATTERNS", "resolve"]

#: The filename half of the testbench rule. Matched against the lowercased
#: basename, consistent with Scan's case-insensitive extension handling.
#: ``--tb-glob`` *replaces* this tuple rather than extending it. ``testbench.*``
#: earns its place because tool-specific benches are often named exactly that,
#: and the evidence half cannot reach them: they lean on cross-file macros and
#: never parse, so a filename is all there is to go on.
TB_FILENAME_PATTERNS: tuple[str, ...] = (
    "*_tb.*",
    "tb_*.*",
    "*_test.*",
    "testbench.*",
)


def resolve(
    scan: ScanResult,
    parsed: ParseResult,
    *,
    top: str | None = None,
    tb_globs: Sequence[str] | None = None,
    tb_overrides: Mapping[Path, TbDirective] | None = None,
) -> ProjectModel:
    """Build the `ProjectModel` for one scanned-and-parsed tree.

    Pure: nothing here touches the filesystem, so hand-built `ScanResult` and
    `ParseResult` pairs work without any files existing. `parsed.warnings`
    are carried through into `ProjectModel.warnings`.

    `top` is the ``--top`` escape hatch. A caller that has already decided the
    toplevel skips detection entirely: no `MultipleTops` ambiguity, no
    fallback, no detection warnings. The name is trusted to be declared
    somewhere in the tree (`autocore.generate` checks before calling); an
    undeclared name gives an empty closure rather than an error.

    `tb_overrides` is the same escape hatch for testbench classification, and
    the shape `interact.py` feeds an answered `UnclearTbStatus` back through.
    A caller that has already decided whether a file is a testbench maps its
    path to `TbDirective.TB` or `TbDirective.RTL`, and that wins outright: no
    ambiguity for it, no warning about it. Paths not in the mapping are
    classified by the ordinary rules.

    `tb_globs` is ``--tb-glob``: filename patterns that **replace**
    `TB_FILENAME_PATTERNS` for this run. The evidence half and the magic
    comments are unaffected, so a tree can turn the filename rule right off
    with a glob matching nothing and still classify by evidence.
    """
    root = scan.root
    warnings: list[Warning] = list(parsed.warnings)

    symbols, duplicate_warnings = _symbol_table(parsed.files, root)
    warnings += duplicate_warnings

    deps, external_refs, external_warnings = _reference_edges(parsed.files, symbols)
    warnings += external_warnings

    testbenches, tb_warnings, tb_ambiguities = _classify_testbenches(
        scan, parsed.files, tb_globs, tb_overrides or {}
    )
    warnings += tb_warnings

    candidates = _effective_include_candidates(scan, parsed.files)
    include_edges, include_warnings = _resolve_includes(parsed.files, candidates)
    warnings += include_warnings

    ambiguities: tuple[Ambiguity, ...] = tuple(tb_ambiguities)
    if top is None:
        top, top_warnings, top_ambiguities = _detect_top(
            parsed.files, symbols, testbenches, root, deps, include_edges
        )
        warnings += top_warnings
        ambiguities += top_ambiguities

    rtl = _closure(symbols.get(top), deps, include_edges, testbenches)

    tb_top, tb_top_warnings, tb_top_alternatives = _detect_tb_top(
        parsed.files, symbols, testbenches, root, deps, include_edges
    )
    warnings += tb_top_warnings
    # No testbench exclusion here: the tb closure is allowed to walk through
    # testbenches, and through rtl too. The subtraction below is what keeps
    # the rtl closure from being emitted a second time.
    tb_files = _closure(symbols.get(tb_top), deps, include_edges, frozenset()) - rtl

    excluded = sorted(
        _rel(path, root)
        for path in scan.files
        if path not in rtl and path not in testbenches and path not in tb_files
    )
    if excluded:
        warnings.append(
            Warning(
                "ExcludedFromRtl",
                f"{len(excluded)} file(s) not reachable from top '{top}', "
                "excluded from the rtl fileset",
                details=tuple(excluded),
            )
        )
    warnings += _dropped_testbench_warnings(testbenches, tb_files, tb_top, root)

    compile_order, cycle_warnings = _compile_order(rtl, deps, include_edges, root)
    warnings += cycle_warnings
    tb_compile_order, tb_cycle_warnings = _compile_order(
        tb_files, deps, include_edges, root
    )
    warnings += tb_cycle_warnings

    matched: set[Path] = set()
    for targets in include_edges.values():
        matched |= targets
    include_files = frozenset(matched & (rtl | tb_files))
    include_dirs = tuple(
        sorted({path.parent for path in include_files}, key=Path.as_posix)
    )

    return ProjectModel(
        files=parsed.files,
        compile_order=compile_order,
        top=top,
        rtl=frozenset(rtl),
        testbenches=testbenches,
        tb_top=tb_top,
        tb_top_alternatives=tb_top_alternatives,
        tb_compile_order=tb_compile_order,
        include_files=include_files,
        include_dirs=include_dirs,
        external_refs=external_refs,
        warnings=tuple(sorted(warnings, key=lambda warning: warning.sort_key)),
        ambiguities=ambiguities,
    )


# --------------------------------------------------------------------------
# symbol table
# --------------------------------------------------------------------------


def _symbol_table(
    files: tuple[FileFacts, ...], root: Path
) -> tuple[dict[str, Path], list[Warning]]:
    """Declared name -> declaring file; first declaration by sorted path wins.

    `files` arrives sorted by path (the `ParseResult` contract), so plain
    iteration order *is* sorted-path order and the first writer wins.
    """
    symbols: dict[str, Path] = {}
    warnings: list[Warning] = []
    for facts in files:
        for name in sorted(facts.declared):
            owner = symbols.get(name)
            if owner is None:
                symbols[name] = facts.path
            elif owner != facts.path:
                warnings.append(
                    Warning(
                        "DuplicateDeclaration",
                        f"'{name}' is also declared in {_rel(owner, root)}, "
                        "which wins by sorted path",
                        facts.path,
                    )
                )
    return symbols, warnings


# --------------------------------------------------------------------------
# reference edges and external refs
# --------------------------------------------------------------------------


def _reference_edges(
    files: tuple[FileFacts, ...], symbols: dict[str, Path]
) -> tuple[dict[Path, set[Path]], frozenset[str], list[Warning]]:
    """A depends on B iff A instantiates or imports something B declares.

    A name declared nowhere becomes an external ref plus one warning, never a
    failure: vendor primitives and encrypted IP are the normal case here.
    Self-edges are dropped, because a file cannot depend on itself, and a
    module instantiating a sibling declared in the same file is an intra-file
    matter the graph does not need to see.
    """
    deps: dict[Path, set[Path]] = {}
    users_of: dict[str, list[Path]] = {}
    for facts in files:
        targets: set[Path] = set()
        for name in sorted(facts.instantiated | facts.imported_pkgs):
            owner = symbols.get(name)
            if owner is None:
                users_of.setdefault(name, []).append(facts.path)
            elif owner != facts.path:
                targets.add(owner)
        deps[facts.path] = targets

    warnings = [
        Warning(
            "ExternalReference",
            f"'{name}' is instantiated or imported but declared nowhere in "
            "the tree; treated as an external reference",
            users[0],
        )
        for name, users in sorted(users_of.items())
    ]
    return deps, frozenset(users_of), warnings


# --------------------------------------------------------------------------
# testbench classification
# --------------------------------------------------------------------------


def _classify_testbenches(
    scan: ScanResult,
    files: tuple[FileFacts, ...],
    tb_globs: Sequence[str] | None,
    overrides: Mapping[Path, TbDirective],
) -> tuple[frozenset[Path], list[Warning], list[Ambiguity]]:
    """Split the scanned tree into testbenches and everything else.

    The rule in precedence order: a caller's `overrides` entry decides, then a
    magic comment decides, in either direction; otherwise the filename
    patterns or strong evidence (``$finish``/``$stop`` **and** an empty port
    list) make it a testbench; otherwise it is RTL. Evidence that is real but
    not strong enough to classify becomes an `UnclearTbStatus` *and* a warning
    saying which way the file fell: the ambiguity is what `interact.py` may
    turn into a question, the warning is what every non-prompting path owes
    the user instead. Nothing here asks anybody anything; that gate lives in
    `interact.py` and nowhere else.

    An override outranks a magic comment because it is the newer statement of
    intent. The two cannot actually collide, since a file carrying a comment
    is never unclear and so is never asked about.

    Iteration is over `scan.files`, which is sorted and complete: a file Parse
    produced no facts for still gets classified, on its filename alone, which
    is the only rule that can reach an unparseable testbench.
    """
    # Both sides are lowercased, so `--tb-glob '*_TB.*'` behaves like the
    # built-in patterns do rather than silently matching nothing.
    patterns = (
        tuple(glob.lower() for glob in tb_globs) if tb_globs else TB_FILENAME_PATTERNS
    )
    facts_by_path = {facts.path: facts for facts in files}

    testbenches: set[Path] = set()
    warnings: list[Warning] = []
    ambiguities: list[Ambiguity] = []

    for path in scan.files:
        facts = facts_by_path.get(path)
        evidence = facts.tb_evidence if facts is not None else TbEvidence()
        directive = facts.tb_directive if facts is not None else None

        forced = overrides.get(path)
        if forced is TbDirective.TB:
            testbenches.add(path)
            continue
        if forced is TbDirective.RTL:
            continue

        if directive is TbDirective.CONFLICTING:
            warnings.append(
                Warning(
                    "ConflictingTbDirective",
                    "carries both '// autocore: tb' and '// autocore: rtl'; "
                    "neither is applied and the ordinary rules decide",
                    path,
                )
            )
            directive = None

        if directive is TbDirective.TB:
            testbenches.add(path)
        elif directive is TbDirective.RTL:
            pass  # beats the filename patterns and the evidence alike
        elif _is_testbench_filename(path.name, patterns) or evidence.strong:
            testbenches.add(path)
        elif evidence.partial:
            ambiguities.append(UnclearTbStatus(path))
            warnings.append(
                Warning(
                    "UnclearTbStatus",
                    "partial testbench evidence, which trips neither branch "
                    "of the rule; treated as RTL",
                    path,
                )
            )

    return frozenset(testbenches), warnings, ambiguities


def _is_testbench_filename(name: str, patterns: Sequence[str]) -> bool:
    """The filename branch, against whichever patterns are in force."""
    lowered = name.lower()
    return any(fnmatchcase(lowered, pattern) for pattern in patterns)


# --------------------------------------------------------------------------
# include resolution: matching here, classification back in `resolve`
# --------------------------------------------------------------------------


def _effective_include_candidates(
    scan: ScanResult, files: tuple[FileFacts, ...]
) -> frozenset[Path]:
    """Scan's candidates minus the demoted ones.

    A `.vh`/`.svh` that declares modules stops being an include candidate and
    becomes an ordinary source file. It reaches the rtl set through
    instantiation edges like any other source, not through include matching.
    A candidate that failed to parse stays a candidate: demotion needs to see
    its declarations, and headers are the files most likely to be unparseable
    standalone.
    """
    demoted = {
        facts.path
        for facts in files
        if facts.declared and facts.path in scan.include_candidates
    }
    return scan.include_candidates - demoted


def _resolve_includes(
    files: tuple[FileFacts, ...], candidates: frozenset[Path]
) -> tuple[dict[Path, set[Path]], list[Warning]]:
    """Match written include strings against candidates by basename.

    Every candidate sharing the basename matches. With only the written string
    to go on there is no way to prefer one `defs.svh` over another, and
    over-including keeps the emitted core buildable. An include that matches
    nothing produces a warning; it must not vanish silently.
    """
    by_basename: dict[str, list[Path]] = {}
    for path in sorted(candidates, key=Path.as_posix):
        by_basename.setdefault(path.name, []).append(path)

    edges: dict[Path, set[Path]] = {}
    warnings: list[Warning] = []
    for facts in files:
        targets: set[Path] = set()
        for written in sorted(facts.includes):
            matches = by_basename.get(_basename(written))
            if matches:
                targets.update(match for match in matches if match != facts.path)
            else:
                warnings.append(
                    Warning(
                        "UnresolvedInclude",
                        f"'{written}' does not match any scanned include candidate",
                        facts.path,
                    )
                )
        edges[facts.path] = targets
    return edges, warnings


def _basename(written: str) -> str:
    """The last component of an include target as written in the source."""
    return written.replace("\\", "/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# top detection
# --------------------------------------------------------------------------


def _detect_top(
    files: tuple[FileFacts, ...],
    symbols: dict[str, Path],
    testbenches: frozenset[Path],
    root: Path,
    deps: dict[Path, set[Path]],
    include_edges: dict[Path, set[Path]],
) -> tuple[str, list[Warning], tuple[Ambiguity, ...]]:
    """Candidates are names no *other* RTL file references.

    File granularity is deliberate: a wrapper and the core it instantiates
    inside the same file are both candidates. That is what keeps a core on the
    candidate list beside the bus wrappers around it when a single file
    declares all of them, which is a common shape. It also keeps a
    self-recursive module a candidate for free.
    """
    rtl_facts = [facts for facts in files if facts.path not in testbenches]
    declared = sorted(
        {
            name
            for facts in rtl_facts
            for name in facts.declared
            if symbols[name] == facts.path
        }
    )
    referenced: set[str] = set()
    for facts in rtl_facts:
        for name in sorted(facts.instantiated | facts.imported_pkgs):
            owner = symbols.get(name)
            if owner is not None and owner != facts.path:
                referenced.add(name)
    candidates = [name for name in declared if name not in referenced]

    if len(candidates) == 1:
        return candidates[0], [], ()

    if not candidates:
        if declared:
            top = declared[0]
            message = (
                "every declared name is referenced by another file; using "
                f"'{top}' (first alphabetically) as the top"
            )
        else:
            top = ""
            message = "nothing is declared anywhere in the tree; no top detected"
        return top, [Warning("NoTopCandidate", message)], ()

    sizes = _closure_sizes(candidates, symbols, deps, include_edges, testbenches)
    top, reason = _fallback_top(candidates, root, sizes)
    warning = Warning(
        "MultipleTops",
        f"multiple top candidates: {', '.join(candidates)}; using "
        f"'{top}' because {reason}",
    )
    ambiguity = MultipleTops(
        candidates=tuple(TopCandidate(name, sizes[name]) for name in candidates)
    )
    return top, [warning], (ambiguity,)


def _detect_tb_top(
    files: tuple[FileFacts, ...],
    symbols: dict[str, Path],
    testbenches: frozenset[Path],
    root: Path,
    deps: dict[Path, set[Path]],
    include_edges: dict[Path, set[Path]],
) -> tuple[str, list[Warning], tuple[str, ...]]:
    """The testbench no *other* testbench instantiates.

    The mirror image of `_detect_top`, over the other half of the tree, with
    one difference that carries the whole idea: only testbench-to-testbench
    references disqualify a candidate. A bench instantiating the rtl top is
    the normal case and must stay a candidate, because that is what a bench
    is for.

    Returns the chosen toplevel, its warnings, and the names it beat. Several
    candidates reuse the same fallback as the rtl top (directory name, then
    largest closure, then alphabetical) with a warning, but produce no
    ambiguity and never a prompt: exactly one sim target with exactly one
    toplevel is emitted whatever happens, and the losers come back as
    alternatives so Emit can flag the guess in the file itself. No testbenches
    at all means no sim target, which is an empty string here rather than a
    warning about it.
    """
    tb_facts = [facts for facts in files if facts.path in testbenches]
    declared = sorted(
        {
            name
            for facts in tb_facts
            for name in facts.declared
            if symbols[name] == facts.path
        }
    )
    referenced: set[str] = set()
    for facts in tb_facts:
        for name in sorted(facts.instantiated | facts.imported_pkgs):
            owner = symbols.get(name)
            if owner is not None and owner != facts.path and owner in testbenches:
                referenced.add(name)
    candidates = [name for name in declared if name not in referenced]

    if not declared:
        # Testbenches that declare nothing (or nothing they own) cannot host a
        # sim target; the classification still stands, keeping them out of rtl.
        return "", [], ()

    if len(candidates) == 1:
        return candidates[0], [], ()

    if not candidates:
        # Mutually-instantiating benches: nobody is free, so the choice is made
        # from every declared name and is a guess in the same way several
        # candidates would be, hence alternatives here too.
        top = declared[0]
        warning = Warning(
            "NoTestbenchTop",
            "every testbench module is instantiated by another testbench; "
            f"using '{top}' (first alphabetically) as the sim toplevel",
        )
        return top, [warning], tuple(name for name in declared if name != top)

    # An empty testbench set means "nothing is off limits", which is what the
    # tb closure wants: sizes here are measured the way tb_files is built.
    sizes = _closure_sizes(candidates, symbols, deps, include_edges, frozenset())
    top, reason = _fallback_top(candidates, root, sizes)
    warning = Warning(
        "MultipleTestbenchTops",
        f"multiple testbench top candidates: {', '.join(candidates)}; "
        f"using '{top}' as the sim toplevel because {reason}",
    )
    return top, [warning], tuple(name for name in candidates if name != top)


def _closure_sizes(
    candidates: list[str],
    symbols: dict[str, Path],
    deps: dict[Path, set[Path]],
    include_edges: dict[Path, set[Path]],
    testbenches: frozenset[Path],
) -> dict[str, int]:
    """How many files each candidate drags behind it: the tiebreak, and the
    number a prompt shows so the automatic answer is legible."""
    return {
        name: len(_closure(symbols.get(name), deps, include_edges, testbenches))
        for name in candidates
    }


def _fallback_top(
    candidates: list[str], root: Path, sizes: dict[str, int]
) -> tuple[str, str]:
    """Pick a top without asking: the candidate matching the project directory
    name, else the one with the largest transitive closure, with alphabetical
    order breaking genuine ties.

    Closure size is the better guess at "the real top". A leaf that only fell
    off the graph because an ``ifdef`` switched it off has a closure of one,
    while the module that instantiates most of the tree drags the whole tree
    behind it. `candidates` is already sorted, so the first strictly-largest
    closure wins and equal closures fall back to name order.
    """
    directory = _sanitize(root.name)
    for name in candidates:
        if _sanitize(name) == directory:
            return name, "it matches the project directory name"

    largest = max(sizes.values())
    leaders = [name for name in candidates if sizes[name] == largest]
    plural = "file" if largest == 1 else "files"
    if len(leaders) == 1:
        return leaders[0], (
            f"it has the largest transitive closure ({largest} {plural})"
        )
    return leaders[0], (
        f"it is first alphabetically among {len(leaders)} candidates whose "
        f"closures tie at {largest} {plural}"
    )


def _sanitize(name: str) -> str:
    """Fold a directory or module name so the two can be compared.

    `my-chip/` should claim a module named `my_chip`: directory names use
    separators identifiers cannot, so both sides are lowercased and squeezed
    to `[a-z0-9_]` before comparing.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --------------------------------------------------------------------------
# the rtl closure
# --------------------------------------------------------------------------


def _closure(
    start: Path | None,
    deps: dict[Path, set[Path]],
    include_edges: dict[Path, set[Path]],
    testbenches: frozenset[Path],
) -> set[Path]:
    """Every file reachable from `start` over instantiation, import and
    include edges, never entering a testbench."""
    if start is None or start in testbenches:
        return set()
    reached = {start}
    stack = [start]
    while stack:
        path = stack.pop()
        neighbours = deps.get(path, set()) | include_edges.get(path, set())
        for target in sorted(neighbours, key=Path.as_posix):
            if target not in reached and target not in testbenches:
                reached.add(target)
                stack.append(target)
    return reached


# --------------------------------------------------------------------------
# testbenches the sim target has no room for
# --------------------------------------------------------------------------


def _dropped_testbench_warnings(
    testbenches: frozenset[Path],
    tb_files: set[Path],
    tb_top: str,
    root: Path,
) -> list[Warning]:
    """Name the testbenches that were classified and then left out anyway.

    A sim target is built from one toplevel, so a bench outside that
    toplevel's closure has nowhere to go, and a tree whose benches declare
    nothing at all gets no sim target, so all of them do. Either way the file
    is in neither fileset: classified out of rtl, dropped from tb. That is a
    defensible outcome but not a silent one, because nothing the user wrote
    may vanish without a word. The mirror of `ExcludedFromRtl`, down to
    putting the names on `Warning.details` and the count in the message.
    """
    dropped = sorted(_rel(path, root) for path in testbenches if path not in tb_files)
    if not dropped:
        return []
    reason = (
        f"not reachable from the sim toplevel '{tb_top}'"
        if tb_top
        else "no sim toplevel could be detected among them"
    )
    return [
        Warning(
            "ExcludedFromTb",
            f"{len(dropped)} file(s) classified as testbenches but {reason}, "
            "excluded from the tb fileset",
            details=tuple(dropped),
        )
    ]


# --------------------------------------------------------------------------
# compile order
# --------------------------------------------------------------------------


def _compile_order(
    nodes: set[Path],
    deps: dict[Path, set[Path]],
    include_edges: dict[Path, set[Path]],
    root: Path,
) -> tuple[tuple[Path, ...], list[Warning]]:
    """Stable topological sort of the rtl closure, dependencies first.

    Ties break alphabetically: of everything currently free of unmet
    dependencies, the first by POSIX path is emitted, one node at a time.
    Emitting a whole ready batch at once would let a later name jump ahead of
    a file it unblocks. A cycle breaks at the alphabetically-last edge that
    actually closes one, with a warning, and the sort carries on. "Closes
    one" matters: when the sort is stuck, edges from innocent bystanders that
    merely depend on a cycle are also still unmet, and cutting one of those
    would move the bystander ahead of its own dependencies.

    A node with no entry in either edge dict (a matched header that never
    parsed) simply has no dependencies and sorts early, which is where a
    header belongs.
    """
    remaining: dict[Path, set[Path]] = {
        node: {
            target
            for target in deps.get(node, set()) | include_edges.get(node, set())
            if target in nodes and target != node
        }
        for node in sorted(nodes, key=Path.as_posix)
    }
    order: list[Path] = []
    warnings: list[Warning] = []
    pending = set(nodes)
    while pending:
        ready = sorted(
            (node for node in pending if not remaining[node]), key=Path.as_posix
        )
        if ready:
            node = ready[0]
            order.append(node)
            pending.remove(node)
            for other in pending:
                remaining[other].discard(node)
        else:
            edges = sorted(
                ((node, target) for node in pending for target in remaining[node]),
                key=lambda edge: (edge[0].as_posix(), edge[1].as_posix()),
            )
            # A stuck subgraph always contains a cycle, so at least one edge
            # closes one; take the alphabetically last of those.
            dependent, dependency = next(
                edge
                for edge in reversed(edges)
                if _reaches(edge[1], edge[0], remaining)
            )
            remaining[dependent].discard(dependency)
            warnings.append(
                Warning(
                    "CircularDependency",
                    "dependency cycle broken by ignoring the edge to "
                    f"{_rel(dependency, root)}",
                    dependent,
                )
            )
    return tuple(order), warnings


def _reaches(source: Path, target: Path, remaining: dict[Path, set[Path]]) -> bool:
    """Whether `target` is reachable from `source` over the unmet edges.

    The unsorted set walk inside is safe because the answer is a bool: no set
    order reaches anything the output is built from.
    """
    seen = {source}
    stack = [source]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        for step in remaining.get(node, ()):
            if step not in seen:
                seen.add(step)
                stack.append(step)
    return False


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _rel(path: Path, root: Path) -> str:
    """`path` as the tree-relative POSIX string a warning is allowed to show.

    Warning messages must never embed the absolute checkout location: the same
    tree resolved from two checkouts must produce identical models up to
    `root`.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name
