"""Interactive layer for resolver ambiguities.

This module is the only part of autocore that may ask the user a question.

The pipeline itself stays non-interactive: Resolve produces ambiguities as
data, and this module decides whether they should be turned into prompts.
That keeps the pipeline deterministic and interactive behavior optional.

`decide()` only prompts when all three conditions are true:
1. stdin is a TTY,
2. non-interactive mode was not requested, and
3. the model contains at least one ambiguity.

In every other case, autocore keeps the default resolver decision. Any answers
collected here are returned as `Decisions`, which the caller can feed back into
`autocore.regenerate()` to re-run only the later pipeline stages.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from autocore.models import (
    MixedLangFileType,
    MultipleTops,
    ProjectModel,
    TbDirective,
    UnclearTbStatus,
)

__all__ = [
    "Asker",
    "Choice",
    "Decisions",
    "QuestionaryAsker",
    "decide",
]


@dataclass(frozen=True)
class Choice:
    """Represent one selectable answer in an interactive prompt."""

    value: str
    label: str


class Asker(Protocol):
    """How this module asks a question, and the seam tests replace.

    Every prompt autocore has takes the same shape, so one method covers them:
    pick one of several, with a default that reproduces the non-interactive
    answer. Implementations must return `default` when the user declines to
    choose (empty input, Ctrl-C, a closed terminal), never raise, and never
    write to stdout.
    """

    def select(
        self, question: str, choices: Sequence[Choice], *, default: str
    ) -> str: ...


class QuestionaryAsker:
    """The real asker: questionary, rendered to stderr.

    questionary is imported inside `select`, not at module scope, so a
    non-interactive run never pays for the prompt-toolkit import.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = sys.stderr if stream is None else stream

    def select(self, question: str, choices: Sequence[Choice], *, default: str) -> str:
        import questionary
        from prompt_toolkit.output import create_output

        items = [
            questionary.Choice(title=choice.label, value=choice.value)
            for choice in choices
        ]
        preselected = next(item for item in items if item.value == default)
        answer = questionary.select(
            question,
            choices=items,
            default=preselected,
            output=create_output(stdout=self._stream),
        ).ask()
        # `ask()` returns None when the user interrupts. Fall back to the
        # default, which is what `--yes` would have produced.
        return default if answer is None else answer


@dataclass(frozen=True)
class Decisions:
    """What the user chose, in the shape Resolve takes it back in.

    Empty when nothing was asked. The caller reads that as "the first pipeline
    run stands" and skips re-resolving.
    """

    top: str | None = None
    tb_overrides: tuple[tuple[Path, TbDirective], ...] = ()

    def __bool__(self) -> bool:
        return self.top is not None or bool(self.tb_overrides)


def decide(
    model: ProjectModel,
    root: Path,
    *,
    assume_yes: bool,
    asker: Asker | None = None,
    stdin: IO[str] | None = None,
) -> Decisions:
    """Ask about `model`'s ambiguities, or return the empty `Decisions`.

    `root` only shapes the wording of the questions; paths are shown relative
    to the scanned tree so a checkout location never appears in a prompt.

    The condition right below is the only place in autocore that decides
    whether anything prompts.
    """
    stream = sys.stdin if stdin is None else stdin
    if assume_yes or not model.ambiguities or not _is_a_tty(stream):
        return Decisions()

    asker = QuestionaryAsker() if asker is None else asker
    top: str | None = None
    overrides: list[tuple[Path, TbDirective]] = []

    for ambiguity in model.ambiguities:
        if isinstance(ambiguity, MultipleTops):
            top = _ask_top(asker, ambiguity, model.top)
        elif isinstance(ambiguity, UnclearTbStatus):
            overrides.append((ambiguity.path, _ask_tb_status(asker, ambiguity, root)))
        elif isinstance(ambiguity, MixedLangFileType):
            # Unreachable until a VHDL backend lands, and `resolve()` has no
            # file-type override for an answer to feed back into. Keep the
            # dominant-language default Emit picks; the `else` below is what
            # catches an ambiguity type nobody handles.
            continue
        else:
            raise AssertionError(f"unhandled ambiguity type: {type(ambiguity)!r}")

    return Decisions(top=top, tb_overrides=tuple(overrides))


def _is_a_tty(stream: IO[str] | None) -> bool:
    """Whether `stream` is a terminal a human could answer through.

    stdin can be `None` (a GUI-launched interpreter) or already closed. Such a
    run would apply the defaults anyway, so neither case should fail it.
    """
    if stream is None:
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_top(asker: Asker, ambiguity: MultipleTops, fallback: str) -> str:
    """Which module is the toplevel, defaulting to the automatic winner.

    `fallback` is `ProjectModel.top`, the answer the automatic fallback
    already applied, so pressing enter reproduces ``--yes`` exactly. Each
    candidate shows its closure size, which is the number the fallback weighed
    and usually the only thing distinguishing two bare module names.
    """
    choices = [
        Choice(
            value=c.name,
            label=f"{c.name} ({c.closure_size} file(s) in its closure)",
        )
        for c in ambiguity.candidates
    ]
    return asker.select(
        "Several modules are instantiated by nobody. Which is the toplevel?",
        choices,
        default=fallback,
    )


def _ask_tb_status(asker: Asker, ambiguity: UnclearTbStatus, root: Path) -> TbDirective:
    """RTL or testbench for one file, defaulting to the non-testbench reading.

    One file per prompt, since each answer is independent and a combined
    prompt would slow down the common case of accepting every default. The
    default matches Resolve's: partial evidence is not enough to move a file
    out of the design.
    """
    answer = asker.select(
        f"{_rel(ambiguity.path, root)} looks like it might be a testbench. What is it?",
        [
            Choice(TbDirective.RTL.value, "RTL: part of the design"),
            Choice(TbDirective.TB.value, "testbench: simulation only"),
        ],
        default=TbDirective.RTL.value,
    )
    return TbDirective.TB if answer == TbDirective.TB.value else TbDirective.RTL


def _rel(path: Path, root: Path) -> str:
    """Return `path` relative to the scanned tree using POSIX separators."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
