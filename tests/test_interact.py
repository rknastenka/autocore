"""Tests for the interactive layer.

Three things are under test and they are worth naming apart:

* **the gate** — prompt if and only if stdin is a TTY, ``--yes`` was not
  passed, and an `Ambiguity` exists. Eight combinations, one rule, one place.
* **the questions** — for each member of the ambiguity union: what is asked,
  in what order, and which answer is the default.
* **the answers** — that they come back in the shape Resolve takes, and that
  accepting every default reproduces the ``--yes`` result exactly.

No test drives a real TTY: `FakeStdin` decides what `isatty()` says and
`FakeAsker` replaces questionary through the asker seam. CI has no terminal, and
a test suite that needed one would simply be untestable there.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pytest

from autocore import GenerateOptions, generate, regenerate
from autocore.interact import Asker, Choice, Decisions, QuestionaryAsker, decide
from autocore.models import (
    MixedLangFileType,
    MultipleTops,
    ProjectModel,
    TbDirective,
    TopCandidate,
    UnclearTbStatus,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ROOT = Path("/proj")


# --------------------------------------------------------------------------
# the seam: a scripted asker and a stdin that says what it is told to
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """One prompt as the user would have seen it."""

    question: str
    choices: tuple[Choice, ...]
    default: str

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(choice.label for choice in self.choices)

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(choice.value for choice in self.choices)


class FakeAsker:
    """A scripted `Asker`: records every prompt, answers from a queue.

    An exhausted queue means "pressed enter" — the default — which is both
    what the real asker returns for an interrupted prompt and the case that
    has to end up identical to ``--yes``.
    """

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[Ask] = []

    def select(self, question: str, choices, *, default: str) -> str:
        self.asked.append(Ask(question, tuple(choices), default))
        return self.answers.pop(0) if self.answers else default


class FakeStdin(io.StringIO):
    """A stdin whose `isatty()` answers whatever the test needs."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def tty() -> FakeStdin:
    return FakeStdin(True)


def pipe() -> FakeStdin:
    return FakeStdin(False)


def model_with(*ambiguities, top: str = "chip") -> ProjectModel:
    """The least model `decide` reads: a top and some ambiguities."""
    return ProjectModel(
        files=(), compile_order=(), top=top, ambiguities=tuple(ambiguities)
    )


TWO_TOPS = MultipleTops(candidates=(TopCandidate("soc_a", 2), TopCandidate("soc_b", 7)))
UNCLEAR = UnclearTbStatus(path=ROOT / "bench" / "monitor.sv")


def test_the_fake_asker_satisfies_the_protocol() -> None:
    # Cheap, but it is the seam's whole contract: if the fake drifts from
    # `Asker`, every test below is testing something auto-core never calls.
    asker: Asker = FakeAsker()

    assert asker.select("q", [Choice("a", "A")], default="a") == "a"


# --------------------------------------------------------------------------
# the gate: three conditions, one place
# --------------------------------------------------------------------------


def test_a_tty_without_yes_and_with_an_ambiguity_prompts() -> None:
    asker = FakeAsker("soc_b")

    decisions = decide(
        model_with(TWO_TOPS), ROOT, assume_yes=False, asker=asker, stdin=tty()
    )

    assert len(asker.asked) == 1
    assert decisions == Decisions(top="soc_b")


#: Every ambiguity type that can reach a prompt today. The two gate tests
#: below run over all of them, because "prompt iff three conditions" is a
#: property of the gate, not of any one question.
PROMPTABLE = {"MultipleTops": TWO_TOPS, "UnclearTbStatus": UNCLEAR}


@pytest.mark.parametrize("ambiguity", list(PROMPTABLE.values()), ids=list(PROMPTABLE))
def test_yes_never_prompts_even_on_a_tty(ambiguity) -> None:
    """The condition that stopped being a no-op in this session."""
    asker = FakeAsker("soc_b", "tb")

    decisions = decide(
        model_with(ambiguity), ROOT, assume_yes=True, asker=asker, stdin=tty()
    )

    assert asker.asked == []
    assert not decisions


@pytest.mark.parametrize("ambiguity", list(PROMPTABLE.values()), ids=list(PROMPTABLE))
def test_a_pipe_never_prompts_even_without_yes(ambiguity) -> None:
    """No terminal, no question — which is what makes a CI run behave the
    same way with or without ``--yes``."""
    asker = FakeAsker("soc_b", "tb")

    decisions = decide(
        model_with(ambiguity), ROOT, assume_yes=False, asker=asker, stdin=pipe()
    )

    assert asker.asked == []
    assert not decisions


def test_an_unambiguous_tree_never_prompts_on_a_tty_either() -> None:
    asker = FakeAsker()

    decisions = decide(model_with(), ROOT, assume_yes=False, asker=asker, stdin=tty())

    assert asker.asked == []
    assert not decisions


@pytest.mark.parametrize("stream", [None, io.StringIO()], ids=["none", "closed"])
def test_a_missing_or_unusable_stdin_falls_back_to_the_defaults(stream) -> None:
    """A GUI-launched interpreter has no stdin at all. That is a reason to
    apply the documented default, never to fail a run."""
    if isinstance(stream, io.StringIO):
        stream.close()
    asker = FakeAsker("soc_b")

    decisions = decide(
        model_with(TWO_TOPS), ROOT, assume_yes=False, asker=asker, stdin=stream
    )

    assert asker.asked == []
    assert not decisions


# --------------------------------------------------------------------------
# MultipleTops
# --------------------------------------------------------------------------


def ask_about(*ambiguities, answers: tuple[str, ...] = ()) -> tuple[FakeAsker, ...]:
    asker = FakeAsker(*answers)
    decisions = decide(
        model_with(*ambiguities, top="soc_a"),
        ROOT,
        assume_yes=False,
        asker=asker,
        stdin=tty(),
    )
    return asker, decisions


def test_the_top_prompt_defaults_to_the_d15_winner() -> None:
    """`ProjectModel.top` is what the fallback already applied, so pressing
    enter is the same answer ``--yes`` would have produced."""
    asker, decisions = ask_about(TWO_TOPS)

    (ask,) = asker.asked
    assert ask.default == "soc_a"
    assert decisions == Decisions(top="soc_a")


def test_the_top_prompt_shows_every_candidate_with_its_closure_size() -> None:
    """The closure size is what the tiebreak used, so it is what makes the
    choice obvious rather than a coin toss between two names."""
    asker, _ = ask_about(TWO_TOPS)

    (ask,) = asker.asked
    assert ask.values == ("soc_a", "soc_b")
    assert ask.labels == (
        "soc_a (2 file(s) in its closure)",
        "soc_b (7 file(s) in its closure)",
    )


def test_choosing_a_different_top_is_what_comes_back() -> None:
    _, decisions = ask_about(TWO_TOPS, answers=("soc_b",))

    assert decisions == Decisions(top="soc_b")


# --------------------------------------------------------------------------
# UnclearTbStatus
# --------------------------------------------------------------------------


def test_the_tb_prompt_asks_one_file_at_a_time_defaulting_to_rtl() -> None:
    other = UnclearTbStatus(path=ROOT / "bench" / "probe.sv")
    asker, decisions = ask_about(UNCLEAR, other)

    assert len(asker.asked) == 2
    first, second = asker.asked
    assert "bench/monitor.sv" in first.question
    assert "bench/probe.sv" in second.question
    assert first.default == "rtl" and second.default == "rtl"
    # The documented default is the non-testbench reading, in both directions.
    assert decisions.tb_overrides == (
        (ROOT / "bench" / "monitor.sv", TbDirective.RTL),
        (ROOT / "bench" / "probe.sv", TbDirective.RTL),
    )


def test_the_tb_prompt_names_the_file_relative_to_the_scanned_tree() -> None:
    """A question must never read out the absolute location of a checkout."""
    asker, _ = ask_about(UNCLEAR)

    (ask,) = asker.asked
    assert str(ROOT) not in ask.question
    assert ask.values == ("rtl", "tb")


def test_answering_testbench_comes_back_as_a_tb_override() -> None:
    _, decisions = ask_about(UNCLEAR, answers=("tb",))

    assert decisions.tb_overrides == ((ROOT / "bench" / "monitor.sv", TbDirective.TB),)


def test_both_ambiguity_types_are_answered_in_one_pass() -> None:
    asker, decisions = ask_about(UNCLEAR, TWO_TOPS, answers=("tb", "soc_b"))

    assert len(asker.asked) == 2
    assert decisions == Decisions(
        top="soc_b", tb_overrides=((ROOT / "bench" / "monitor.sv", TbDirective.TB),)
    )


# --------------------------------------------------------------------------
# MixedLangFileType, and the gap it must not leave
# --------------------------------------------------------------------------


def test_a_mixed_language_file_type_is_handled_without_being_asked_about() -> None:
    """Resolve cannot build one until VHDL lands, and there is no file-type
    override for an answer to feed back into, so the branch is inert by design
    rather than by omission."""
    asker, decisions = ask_about(MixedLangFileType(path=ROOT / "a.vhd"))

    assert asker.asked == []
    assert not decisions


def test_an_unknown_ambiguity_type_is_a_crash_not_a_shrug() -> None:
    """The guard behind the branch above: a fourth member of the union that
    nobody handles is a user silently denied a say. It must not pass quietly."""

    @dataclass(frozen=True)
    class Invented:
        path: Path

    with pytest.raises(AssertionError, match="unhandled ambiguity"):
        ask_about(Invented(path=ROOT / "x.sv"))


# --------------------------------------------------------------------------
# the real asker, at the seam only
# --------------------------------------------------------------------------


def test_the_real_asker_renders_to_stderr_by_default() -> None:
    """So that ``--dry-run`` piped to a file receives nothing but YAML."""
    import sys

    assert QuestionaryAsker()._stream is sys.stderr


# --------------------------------------------------------------------------
# the answers, fed back through the pipeline (B5)
# --------------------------------------------------------------------------


def run(fixture: str, **options) -> object:
    return generate(FIXTURES / fixture, GenerateOptions(**options))


def answered(fixture: str, *answers: str):
    """One real pipeline run, prompted through the seam, then re-resolved."""
    first = run(fixture)
    asker = FakeAsker(*answers)
    decisions = decide(
        first.model,
        FIXTURES / fixture,
        assume_yes=False,
        asker=asker,
        stdin=tty(),
    )
    if not decisions:
        return first
    return regenerate(
        first,
        GenerateOptions(top=decisions.top, tb_overrides=decisions.tb_overrides),
    )


def test_accepting_every_default_reproduces_the_yes_output_byte_for_byte() -> None:
    """The determinism promise for the interactive path: prompting happens
    outside the pipeline, so an all-defaults run is the ``--yes`` run."""
    assert answered("multiple_tops").text == run("multiple_tops").text


def test_a_chosen_top_reaches_the_manifest_and_silences_its_warning() -> None:
    result = answered("multiple_tops", "soc_b")

    assert result.model.top == "soc_b"
    assert "toplevel: soc_b" in result.text
    assert "rtl/soc_b.v" in result.text
    # Asked and answered: warning the user about a choice they just made
    # would be noise, and the ambiguity is spent.
    assert [w.code for w in result.model.warnings if w.code == "MultipleTops"] == []
    assert result.model.ambiguities == ()


def unclear_tree(tmp_path: Path) -> Path:
    """A tree with exactly one file the classifier cannot make its mind up
    about.

    `monitor` calls `$finish` but has a port, so it trips neither branch of
    the rule: half the evidence, no filename match, no magic comment.
    """
    (tmp_path / "chip.v").write_text(
        "module chip (input wire clk);\n  monitor u_monitor (.clk(clk));\nendmodule\n"
    )
    (tmp_path / "monitor.v").write_text(
        "module monitor (input wire clk);\n"
        "  initial begin\n    #10;\n    $finish;\n  end\n"
        "endmodule\n"
    )
    return tmp_path


def test_an_unanswered_unclear_file_stays_rtl_and_says_so(tmp_path: Path) -> None:
    """The documented default, and the warning every non-prompting path owes
    the user in place of the question it did not ask."""
    root = unclear_tree(tmp_path)

    result = generate(root)

    assert result.model.ambiguities == (UnclearTbStatus(path=root / "monitor.v"),)
    assert root / "monitor.v" in result.model.rtl
    assert result.model.testbenches == frozenset()
    assert [w.code for w in result.model.warnings] == ["UnclearTbStatus"]


def test_answering_testbench_moves_the_file_and_builds_a_sim_target(
    tmp_path: Path,
) -> None:
    root = unclear_tree(tmp_path)
    first = generate(root)
    asker = FakeAsker("tb")

    decisions = decide(first.model, root, assume_yes=False, asker=asker, stdin=tty())
    result = regenerate(first, GenerateOptions(tb_overrides=decisions.tb_overrides))

    assert result.model.testbenches == frozenset({root / "monitor.v"})
    assert result.model.tb_top == "monitor"
    assert "toplevel: monitor\n" in result.text
    assert "mode: binary" in result.text
    # Answered, so nothing is left to warn or ask about.
    assert result.model.ambiguities == ()
    assert [w.code for w in result.model.warnings if w.code == "UnclearTbStatus"] == []


def test_re_resolving_does_not_re_scan_or_re_parse() -> None:
    """B5's actual claim: Scan and Parse cannot change based on an answer, so
    the second run reuses their results object-for-object."""
    first = run("multiple_tops")

    second = regenerate(first, GenerateOptions(top="soc_b"))

    assert second.scanned is first.scanned
    assert second.parsed is first.parsed
    assert second.model is not first.model
