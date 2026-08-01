"""Stage 3: Resolve.

Resolve the scanned-and-parsed project into one final model.

This module takes per-file facts and turns them into a resolved project view:
a symbol table, dependency edges, testbench classification, include matching,
a chosen RTL top, compile order, and the warnings/ambiguities needed by later
stages.

The resolver is intentionally pure. It does not touch the filesystem, prompt
the user, or write output. Its job is to make the best deterministic decision
it can, surface anything unclear as data, and let higher layers decide how to
present that information.

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
#: Default filename patterns used by the testbench classifier.
#: These are matched against the lowercased basename only.
#:
#: `--tb-glob` replaces this tuple for a given run rather than extending it.
#: That keeps the rule easy to reason about: either the built-in patterns
#: are in effect, or the caller has supplied a full replacement set.
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
    """Build the resolved project model for one scanned-and-parsed tree.

    This function is the heart of Stage 3. It combines scan results and parse
    facts into one `ProjectModel` that later stages can emit as a `.core` file.

    The resolver is pure: it does not read files, ask questions, or write
    anything. It only transforms inputs into a deterministic result.

    `top` lets a caller force the RTL top and skip automatic top detection.
    `tb_overrides` lets a caller force specific files to be treated as testbench
    or RTL. `tb_globs` replaces the built-in filename patterns used by the
    testbench classifier for this run.
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

    If the same name is declared in more than one file, the first declaration
    by sorted path wins and later ones become warnings. This keeps the outcome
    deterministic and makes the rule easy to explain.
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
    """Build dependency edges from instantiations and package imports.

    A file depends on another file when it instantiates or imports a name that
    the other file declares.

    Names declared nowhere in the tree are treated as external references
    rather than hard failures. That keeps the resolver usable for real-world
    trees that rely on vendor primitives, encrypted IP, or sources outside the
    scanned project.
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
    """Classify scanned files as testbench or non-testbench.

    The decision order is intentional and stable:

    1. A caller override wins.
    2. An explicit autocore directive wins.
    3. Built-in or caller-supplied filename patterns may classify the file.
    4. Strong parsed evidence may classify the file.
    5. Partial evidence becomes an ambiguity and falls back to RTL.

    This function never prompts the user. It only records warnings and
    ambiguities so a higher layer can decide whether to ask for help.
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
            """Return whether a basename matches the active testbench patterns."""
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
    """Match written include strings to scanned include candidates.

    Matching is done by basename. If several candidates share the same
    basename, they all match. That is conservative, but it avoids silently
    dropping a possible dependency when the source text does not give enough
    information to prefer one file over another.
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
    """Choose the RTL top from the non-testbench part of the tree.

    A top candidate is a declared name that no other RTL file references.

    If there is exactly one candidate, it wins directly. If there are several,
    the resolver applies a deterministic fallback and records the ambiguity so
    interactive layers may present that choice to the user later.
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
    """Choose the simulation top from the files classified as testbenches.

    This mirrors `_detect_top`, but only testbench-to-testbench references can
    disqualify a candidate. A testbench that instantiates the RTL top should
    still remain a valid testbench-top candidate.

    The function always picks one deterministic answer when testbench modules
    exist, and returns any losing alternatives so later stages can annotate the
    emitted file if the choice was a guess.
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
    """Measure how many files each candidate pulls into its transitive closure."""
    return {
        name: len(_closure(symbols.get(name), deps, include_edges, testbenches))
        for name in candidates
    }


def _fallback_top(
    candidates: list[str], root: Path, sizes: dict[str, int]
) -> tuple[str, str]:
    """Pick a deterministic top when several candidates are possible.

    The preference order is:
    1. A candidate whose name matches the project directory name.
    2. The candidate with the largest transitive closure.
    3. Alphabetical order to break a genuine tie.

    The returned string explains why the winner was chosen so callers can turn
    the decision into a human-readable warning.
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
    """Normalize a directory or module name for loose comparison.

    This helps project directory names and HDL identifiers compare sensibly
    even when they differ in case or separator style.
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
    """Return every file reachable from `start` through dependency edges.

    The closure follows instantiation, package-import, and include edges.
    Testbench files are treated as off-limits when building the RTL closure so
    they do not leak into the emitted RTL fileset.
    """
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
    """Warn about files classified as testbenches but left out of the TB fileset.

    This happens when a file is recognized as a testbench, but it is not part
    of the chosen simulation-top closure, or when no usable simulation top can
    be detected at all.

    The file should not disappear silently, so the warning names the dropped
    files and explains why they were excluded.
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
    """Produce a stable dependency-first compile order.

    This is a deterministic topological sort. When several files are currently
    ready, alphabetical path order decides which one comes next.

    If a cycle prevents progress, the resolver breaks one specific edge,
    records a warning, and continues. The goal is to keep producing a usable,
    explainable result rather than fail outright.

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
    """Return a tree-relative POSIX path suitable for warnings.

    Warnings should never depend on the absolute checkout location, so this
    helper converts paths into stable project-relative strings whenever
    possible.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name
