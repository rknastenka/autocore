"""Stage 1: Scan.

Collect ``.v .sv .vh .svh .vhd .vhdl`` under a target directory and hand Parse a
sorted path list. **Nothing here parses.** The only judgement Scan makes about a
file's contents is its extension.

Three rules carry the weight:

* ``.gitignore`` is honoured via pathspec, with git semantics rather than glob
  semantics: nested ignore files shadow shallower ones, ``!`` re-includes, and
  a file under an ignored directory can never be re-included because the walk
  never descends there.
* ``.git/``, ``build/``, ``sim_build/``, ``work/`` and hidden directories are
  skipped unconditionally, at any depth, ignore file or not.
* Symlinks are followed, guarded by a visited set keyed on ``(st_dev, st_ino)``
  so a directory loop terminates instead of recursing forever.

Output order is by POSIX path relative to the scan root, which makes it
independent of readdir order and of where the tree is checked out.
"""

from __future__ import annotations

import os
from pathlib import Path

from pathspec import GitIgnoreSpec

from autocore.models import SOURCE_SUFFIXES, ScanResult, is_include_suffix

__all__ = ["ALWAYS_SKIP_DIRS", "GITIGNORE_NAME", "scan"]

#: Skipped wherever they appear, regardless of ``.gitignore``. Matched as
#: whole directory names, so ``builds/`` and ``workspace/`` survive.
ALWAYS_SKIP_DIRS: frozenset[str] = frozenset({".git", "build", "sim_build", "work"})

GITIGNORE_NAME = ".gitignore"

# (directory relative to the scan root, spec compiled from its .gitignore).
# Innermost last, which is the order git resolves precedence in.
_IgnoreStack = tuple[tuple[str, GitIgnoreSpec], ...]


def scan(root: Path | str) -> ScanResult:
    """Collect every source file under ``root``.

    Raises ``NotADirectoryError`` if ``root`` is not a directory (symlinks to
    directories are fine). Unreadable directories and dangling symlinks are
    skipped silently: Scan never fails on a hostile tree, it returns less.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    found: list[tuple[str, Path]] = []
    _walk(root, root, "", (), found, visited=set())

    # Sort on the relative POSIX path, not the Path object: this is the one
    # place the output order is decided, and it must not depend on the absolute
    # location of the tree.
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
    """Recurse into ``directory``, appending ``(relative posix, path)`` pairs."""
    identity = _identity(directory)
    if identity is None or identity in visited:
        return  # unreadable, or a symlink back into a directory we already did
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
            # Symlinks are followed, so this also rejects the dangling ones:
            # a broken `foo.v` link must not reach Parse as a source file.
            is_file = not is_dir and entry.is_file(follow_symlinks=True)
        except OSError:
            continue  # symlink loop, or an entry we are not allowed to stat

        if is_dir:
            if entry.name in ALWAYS_SKIP_DIRS or entry.name.startswith("."):
                continue
            # A directory only matches a `dir/` pattern when it is offered with
            # a trailing slash; pathspec has no other way to tell the two apart.
            if _is_ignored(stack, f"{child_rel}/"):
                continue
            _walk(root / child_rel, root, child_rel, stack, found, visited)
        elif is_file and Path(entry.name).suffix.lower() in SOURCE_SUFFIXES:
            if _is_ignored(stack, child_rel):
                continue
            found.append((child_rel, root / child_rel))


def _identity(directory: Path) -> tuple[int, int] | None:
    """Filesystem identity of ``directory``, or ``None`` if it cannot be read.

    ``(st_dev, st_ino)`` rather than a resolved path: it is what actually
    distinguishes two directories, and it costs one stat we would pay anyway.
    """
    try:
        info = directory.stat()  # follows symlinks - that is the point
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _load_gitignore(directory: Path) -> GitIgnoreSpec | None:
    """Compile ``directory/.gitignore``, or ``None`` if there isn't a usable one.

    ``GitIgnoreSpec.from_lines`` takes the *lines* first in pathspec 1.x (0.x
    put the pattern factory first), and ``GitIgnoreSpec`` rather than
    ``PathSpec`` is what reproduces git's precedence rules.
    """
    try:
        text = (directory / GITIGNORE_NAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    return GitIgnoreSpec.from_lines(text.splitlines())


def _is_ignored(stack: _IgnoreStack, rel: str) -> bool:
    """Resolve ``rel`` against the stack of ``.gitignore`` files above it.

    Git gives the deepest ignore file the final say, so walk the stack inwards
    out and stop at the first file that has an opinion. pathspec reports that
    as ``CheckResult.include``: ``True`` matched an ignore pattern, ``False``
    matched a ``!`` negation, ``None`` no pattern matched at all.
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
