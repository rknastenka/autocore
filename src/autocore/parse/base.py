"""Parser backend seam.

Two things live here and nothing else: the `ParserBackend` protocol that every
language backend implements, and `parse_all`, the driver that runs one backend
over a `ScanResult`.

The driver exists to hold two invariants:

* **Never fatal.** A file that will not parse produces a `Warning` and is left
  out of `ParseResult.files`; the pipeline keeps going with the files that did
  parse. The only exceptions that escape `parse_all` are the ones that mean the
  process itself is broken, not the source tree.
* **Same order every run.** Work is fanned out over a `ProcessPoolExecutor`,
  so results arrive in completion order, which depends on scheduling and
  varies run to run. They are gathered in full and then sorted by path before
  anything downstream can observe them, which is what keeps the emitted
  manifest byte-identical for an identical tree.

The backend is a parameter rather than an import, so `base` need not know that
`sv_slang` exists and a future VHDL backend can be passed rather than wired in
here. `autocore.parse.parse_sources` supplies the default.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from autocore.models import (
    FileFacts,
    Lang,
    ParseResult,
    ScanResult,
    Warning,
    lang_for_path,
)

__all__ = [
    "PARALLEL_THRESHOLD",
    "ParseError",
    "ParserBackend",
    "parse_all",
]

#: Below this many files, parsing stays in the current process.
#: Spinning up worker processes costs more than it saves for very small inputs.
PARALLEL_THRESHOLD = 4


class ParseError(Exception):
    """One file could not be turned into `FileFacts`.

    Raised by a backend, caught by `parse_all`, and turned into exactly one
    `Warning`. It never reaches a caller of `parse_all`, which is what "never
    fatal" means in practice.
    """

    def __init__(self, path: Path, message: str, code: str = "ParseFailed") -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message
        self.code = code

    def as_warning(self) -> Warning:
        """Convert this parse failure into the warning stored in `ParseResult`."""
        return Warning(code=self.code, message=self.message, path=self.path)


class ParserBackend(Protocol):
    """What Parse needs from a language backend.

    `languages` is what lets `parse_all` route. Scan collects `.vhd` alongside
    `.sv`, so something has to notice that the SystemVerilog backend cannot
    take a VHDL file and say so as a warning rather than a crash. The same
    field is how a caller would pick between two backends once a second one
    exists.
    """

    #: Languages this backend accepts, based on file extension routing.
    languages: frozenset[Lang]

    def parse(self, path: Path, defines: Sequence[str] = ()) -> FileFacts:
        """Return the facts about `path`, or raise `ParseError`.

        `defines` are `NAME` or `NAME=VALUE` strings straight from `--define`.
        Backends parse one file at a time and never look at another one.
        """
        ...


def parse_all(
    scan: ScanResult,
    backend: ParserBackend,
    *,
    defines: Sequence[str] = (),
    max_workers: int | None = None,
) -> ParseResult:
    """Run `backend` over every file in `scan` and return sorted facts.

    Files the backend does not handle, and files it cannot parse, are dropped
    from `ParseResult.files` and reported in `ParseResult.warnings`. Every file
    in `scan.files` ends up in exactly one of the two.
    """
    defines = tuple(defines)

    accepted: list[Path] = []
    warnings: list[Warning] = []
    for path in scan.files:
        language = lang_for_path(path)
        if language is None or language not in backend.languages:
            warnings.append(_unsupported(path, language))
        else:
            accepted.append(path)

    facts: list[FileFacts] = []
    for parsed, file_warnings in _run(backend, accepted, defines, max_workers):
        if parsed is not None:
            facts.append(parsed)
        warnings.extend(file_warnings)

    # The pool hands results back in completion order, so this is where that
    # order is thrown away. Sorting on the POSIX string rather than on `Path`
    # keeps it identical to Scan's, which sorts on the path relative to the
    # tree root; the two agree because every path shares that root as a prefix.
    return ParseResult(
        files=tuple(sorted(facts, key=lambda item: item.path.as_posix())),
        warnings=tuple(sorted(warnings, key=lambda item: item.sort_key)),
    )


def _run(
    backend: ParserBackend,
    paths: Sequence[Path],
    defines: tuple[str, ...],
    max_workers: int | None,
) -> list[tuple[FileFacts | None, tuple[Warning, ...]]]:
    """Parse the given paths either serially or in parallel.

    The returned list reflects completion order when workers are used. Callers are
    responsible for sorting if they need a stable visible order.
    """
    workers = _worker_count(len(paths), max_workers)
    if workers <= 1:
        return [_parse_one(backend, path, defines) for path in paths]

    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(_parse_one, backend, path, defines) for path in paths]
        return [future.result() for future in as_completed(pending)]


def _worker_count(files: int, max_workers: int | None) -> int:
    """Choose how many worker processes to use for this parse run."""
    if files == 0:
        return 1
    if max_workers is not None:
        return max(1, min(max_workers, files))
    if files < PARALLEL_THRESHOLD:
        return 1
    return max(1, min(files, os.cpu_count() or 1))


def _parse_one(
    backend: ParserBackend, path: Path, defines: tuple[str, ...]
) -> tuple[FileFacts | None, tuple[Warning, ...]]:
    """Parse one file, converting any failure into a warning.

    Runs in a worker process, so it must stay picklable and must not raise:
    an escaping exception would take down the pool and with it the whole run.
    """
    try:
        return backend.parse(path, defines), ()
    except ParseError as exc:
        return None, (exc.as_warning(),)
    except OSError as exc:
        # `strerror` rather than `str(exc)`: the latter embeds the absolute
        # path, which would make the warning depend on where the tree sits.
        # A warning must read the same from any checkout.
        detail = exc.strerror or type(exc).__name__
        return None, (Warning("FileUnreadable", detail, path),)
    except Exception as exc:  # a backend bug must not be fatal either
        return None, (Warning("ParseFailed", f"{type(exc).__name__}: {exc}", path),)


def _unsupported(path: Path, language: Lang | None) -> Warning:
    """Build the warning for a file unsupported by the active backend set."""
    named = language.value if language is not None else "unknown"
    return Warning(
        "UnsupportedLanguage",
        f"no parser backend for {named} sources in this run",
        path,
    )
