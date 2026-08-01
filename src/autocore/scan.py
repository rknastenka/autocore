"""Stage 1: Scan.

Scan a project tree and collect source files.

This module is responsible for finding HDL source files under a root directory
and returning them in a stable order for later pipeline stages.

Scan does not parse file contents. Its only content-level decision is based on
file extension. It also applies `.gitignore` rules, skips selected directories,
and follows symlinks safely so the walk stays practical on real project trees.

A key design goal here is deterministic output: the same tree should produce
the same ordered file list regardless of filesystem iteration order or checkout
location.
"""

from __future__ import annotations

import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from autocore.models import SOURCE_SUFFIXES, ScanResult, is_include_suffix

__all__ = ["ALWAYS_SKIP_DIRS", "GITIGNORE_NAME", "scan"]

#: Directory names that are always skipped during scanning.
#: Matching is by whole directory name, so names like `builds/` or
#: `workspace/` are not affected.
ALWAYS_SKIP_DIRS: frozenset[str] = frozenset({".git", "build", "sim_build", "work"})

GITIGNORE_NAME = ".gitignore"

# (directory relative to the scan root, spec compiled from its .gitignore).
# Innermost last, which is the order git resolves precedence in.
_IgnoreStack = tuple[tuple[str, GitIgnoreSpec], ...]


def scan(root: Path | str) -> ScanResult:
    """Scan `root` and collect all supported source files.

    The result is a stable, sorted list of files that later stages can parse.

    Raises:
        NotADirectoryError: If `root` is not a directory.

    Unreadable directories and broken symlinks are skipped quietly. The scanner
    prefers returning a partial result over failing on a messy tree.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    found: list[tuple[str, Path]] = []
    _walk(root, root, "", (), found, visited=set())

    # Sort by the tree-relative POSIX path so output order stays stable and
    # does not depend on the absolute checkout location.
    found.sort(key=lambda item: item[0])
    files = tuple(path for _, path in found)

    return ScanResult(
        root=root,
        files=files,
        include_candidates=frozenset(p for p in files if is_include_suffix(p)),
    )


def _walk(
    directory: Path,
    root: Path,
    rel: str,
    stack: _IgnoreStack,
    found: list[tuple[str, Path]],
    visited: set[tuple[int, int]],
) -> None:
    """Walk one directory and append matching files to `found`.

    Collected entries are stored as `(relative_posix_path, absolute_path)`
    pairs so the caller can sort deterministically by the portable relative
    path while still returning real `Path` objects.
    """
    identity = _identity(directory)
    if identity is None or identity in visited:
        return  # unreadable, or a symlink back to a directory already seen
    visited.add(identity)

    spec = _load_gitignore(directory)
    if spec is not None:
        stack = (*stack, (rel, spec))

    try:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda entry: entry.name)
    except OSError:
        return

    for entry in entries:
        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
            # Symlinks are followed here, which also filters out broken ones:
            # a dangling `foo.v` link should not reach the parse stage.
            is_file = not is_dir and entry.is_file(follow_symlinks=True)
        except OSError:
            continue  # symlink loop, or an entry we are not allowed to stat

        if is_dir:
            if entry.name in ALWAYS_SKIP_DIRS or entry.name.startswith("."):
                continue
            # `pathspec` only treats this as a directory match if the path has
            # a trailing slash, so add one before checking ignore rules.
            if _is_ignored(stack, f"{child_rel}/"):
                continue
            _walk(root / child_rel, root, child_rel, stack, found, visited)
        elif is_file and Path(entry.name).suffix.lower() in SOURCE_SUFFIXES:
            if _is_ignored(stack, child_rel):
                continue
            found.append((child_rel, root / child_rel))


def _identity(directory: Path) -> tuple[int, int] | None:
    """Return a filesystem identity for `directory`, or `None` if unreadable.

    The identity is `(st_dev, st_ino)`, which lets the scanner detect directory
    loops even when symlinks or multiple paths point to the same place.
    """
    try:
        info = directory.stat()  # follows symlinks - that is the point
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _load_gitignore(directory: Path) -> GitIgnoreSpec | None:
    """Load and compile `directory/.gitignore`, if present and readable.

    This uses `GitIgnoreSpec` so ignore behavior follows Git-style precedence
    rules rather than plain glob matching.
    """
    try:
        text = (directory / GITIGNORE_NAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    return GitIgnoreSpec.from_lines(text.splitlines())


def _is_ignored(stack: _IgnoreStack, rel: str) -> bool:
    """Return whether `rel` should be ignored by the active `.gitignore` stack.

    The deepest matching `.gitignore` file wins, which matches Git's own
    precedence rules. A `!` pattern re-includes a path only if the scanner was
    still able to reach that path in the first place.
    """
    for base, spec in reversed(stack):
        candidate = rel
        if base:
            prefix = f"{base}/"
            if not rel.startswith(prefix):
                continue
            candidate = rel[len(prefix) :]
        verdict = spec.check_file(candidate).include
        if verdict is not None:
            return verdict
    return False
