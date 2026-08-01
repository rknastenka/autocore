"""Interactive layer for resolver ambiguities.

This module is the only part of autocore that may ask the user a question.

The pipeline itself stays non-interactive: Resolve produces ambiguities as
data, and this module decides whether they should be turned into prompts.
That separation keeps the core pipeline deterministic and makes interactive
behavior optional.

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

#: The two possible answers for an unclear testbench classification.
#: `_RTL` is also the documented default.
_RTL = "rtl"
_TB = "tb"


@dataclass(frozen=True)
class Choice:
    """Represent one selectable answer in an interactive prompt."""

    value: str
    label: str


class Asker(Protocol):
    """How this module asks a question, and the seam tests replace.

    One method, because every prompt auto-core has is the same shape: pick one
    of several, with a default that reproduces the non-interactive answer.
    Implementations must return `default` when the user declines to choose
    (empty input, Ctrl-C, a closed terminal), never raise, and never write to
    stdout.
    """

    def select(
        self, question: str, choices: Sequence[Choice], *, default: str
    ) -> str: ...


class QuestionaryAsker:
    """The real asker: questionary, rendered to stderr.

    questionary is imported inside `select` rather than at module scope so the
    overwhelmingly common path — a non-interactive run — never pays for a
    prompt-toolkit import it will not use.
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
        # `ask()` returns None when the user interrupts. Declining to choose is
        # not an error; it is a request for the default, which is the same
        # answer `--yes` would have produced.
        return default if answer is None else answer


@dataclass(frozen=True)
class Decisions:
    """What the user chose, in the shape Resolve takes it back in.

    Empty when nothing was asked, which is the signal to the caller that the
    first pipeline run already stands and Resolve need not run again.
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

    `root` only shapes the wording of the questions — paths are shown relative
    to the scanned tree, never as the absolute location of a checkout.

    The gate is spelled out once, right below, and is the only place in
    auto-core that decides whether anything prompts.
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
            # Implemented, unreachable, and deliberately inert: Resolve cannot
            # build a `MixedLangFileType` until a VHDL backend lands, and there
            # is no file-type override on `resolve()` for an answer to feed
            # back into yet. Asking a question whose answer goes nowhere would
            # be worse than not asking, so this branch applies the
            # dominant-language default Emit already picks. What it must not
            # be is *missing*: an ambiguity type nobody handles is one the
            # user is silently denied a say in, which is what the `else`
            # below turns into a crash rather than a shrug.
            continue
        else:
            raise AssertionError(f"unhandled ambiguity type: {type(ambiguity)!r}")

    return Decisions(top=top, tb_overrides=tuple(overrides))


def _is_a_tty(stream: IO[str] | None) -> bool:
    """Whether `stream` is a terminal a human could answer through.

    Defensive on purpose: stdin can be `None` (a GUI-launched interpreter) or
    already closed, and neither is a reason to fail a run that was going to
    apply the defaults anyway.
    """
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):
        return False


def _ask_top(asker: Asker, ambiguity: MultipleTops, fallback: str) -> str:
    """Which module is the toplevel, defaulting to the automatic winner.

    `fallback` is `ProjectModel.top`, the answer the automatic fallback
    already applied, so pressing enter reproduces ``--yes`` exactly. Each
    candidate shows its closure size because that is the number the fallback
    weighed: seeing that one candidate drags 40 files behind it and another
    drags one is what makes the choice obvious rather than a coin toss between
    two names.
    """
    choices = [
        Choice(
            value=candidate.name,
            label=f"{candidate.name} ({candidate.closure_size} file(s) in its closure)",
        )
        for candidate in ambiguity.candidates
    ]
    return asker.select(
        "Several modules are instantiated by nobody. Which is the toplevel?",
        choices,
        default=fallback,
    )


def _ask_tb_status(asker: Asker, ambiguity: UnclearTbStatus, root: Path) -> TbDirective:
    """RTL or testbench for one file, defaulting to the non-testbench reading.

    One file at a time, deliberately: each answer is independent, and a single
    combined prompt would make the common case (accept every default) harder
    rather than easier. The default matches Resolve's: partial evidence is
    not enough to move a file out of the design.
    """
    answer = asker.select(
        f"{_rel(ambiguity.path, root)} looks like it might be a testbench. What is it?",
        [
            Choice(_RTL, "RTL — part of the design"),
            Choice(_TB, "testbench — simulation only"),
        ],
        default=_RTL,
    )
    return TbDirective.TB if answer == _TB else TbDirective.RTL


def _rel(path: Path, root: Path) -> str:
    """Return `path` relative to the scanned tree using POSIX separators."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
