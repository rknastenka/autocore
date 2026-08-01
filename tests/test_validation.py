"""Real-consumer validation of the golden files.

Every golden is parsed by FuseSoC's own CAPI2 loader in strict mode, and the
resolved file list (what FuseSoC would actually hand a tool) is checked against
a hand-written expectation rather than anything the pipeline computed.
test_e2e.py pins the goldens byte for byte; this pins what they mean to the
consumer, so a fusesoc bump or a schema drift in our emitter fails loudly.

It uses fusesoc's internal API, which is why fusesoc is pinned in the dev
dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fusesoc.capi2.core import Core
from fusesoc.capi2.coreparser import Core2Parser

GOLDEN = Path(__file__).resolve().parent / "golden"

GOLDEN_NAMES = sorted(path.stem for path in GOLDEN.glob("*.core"))

#: Per golden: the toplevel and the resolved default-target file list as
#: ``(name, file_type, is_include_file)``, in order. Written by hand so a
#: pipeline regression cannot rewrite the expectation.
EXPECTED: dict[str, tuple[str, list[tuple[str, str, bool]]]] = {
    "broken_file": (
        "good",
        [("rtl/good.v", "verilogSource", False)],
    ),
    "external_prims": (
        "chip",
        [
            ("rtl/alu_core.v", "verilogSource", False),
            ("rtl/chip.v", "verilogSource", False),
        ],
    ),
    "ifdef_heavy": (
        "top",
        [
            ("include/config.svh", "systemVerilogSource", True),
            ("rtl/adder.v", "verilogSource", False),
            ("rtl/shifter.v", "verilogSource", False),
            ("rtl/top.sv", "systemVerilogSource", False),
        ],
    ),
    "multi_file_hierarchy": (
        "top",
        [
            ("include/defs.svh", "systemVerilogSource", True),
            ("rtl/core/alu.v", "verilogSource", False),
            ("rtl/core/regfile.sv", "systemVerilogSource", False),
            ("rtl/top.sv", "systemVerilogSource", False),
        ],
    ),
    "multiple_tops": (
        "soc_a",
        [
            ("rtl/common_alu.v", "verilogSource", False),
            ("rtl/soc_a.v", "verilogSource", False),
        ],
    ),
    "single_module": (
        "alu",
        [("alu.v", "verilogSource", False)],
    ),
    "with_testbench": (
        "chip",
        [
            ("rtl/alu.v", "verilogSource", False),
            ("rtl/self_test.v", "verilogSource", False),
            ("rtl/chip.v", "verilogSource", False),
        ],
    ),
}

#: The same for the ``sim`` target, plus the tool options FuseSoC hands the
#: tool. Filesets resolve in the order the target names them, so the list is
#: rtl followed by tb, with nothing listed twice.
#:
#: The options are asserted through the consumer rather than the emitted text
#: because that is the point of them: edalize defaults Verilator to ``cc``
#: mode, and anything but ``binary`` cannot link for want of a C++ ``main()``.
EXPECTED_SIM: dict[str, tuple[str, list[tuple[str, str, bool]], dict[str, str]]] = {
    "with_testbench": (
        "chip_tb",
        [
            ("rtl/alu.v", "verilogSource", False),
            ("rtl/self_test.v", "verilogSource", False),
            ("rtl/chip.v", "verilogSource", False),
            ("bench/clkgen.sv", "systemVerilogSource", False),
            ("bench/scoreboard.sv", "systemVerilogSource", False),
            ("bench/chip_tb.sv", "systemVerilogSource", False),
        ],
        {"mode": "binary"},
    ),
}


def test_every_golden_has_a_hand_written_expectation() -> None:
    # A new golden without an entry fails here, and so does a stale entry.
    assert GOLDEN_NAMES == sorted(EXPECTED)


def target_names(name: str) -> list[str]:
    """The targets fusesoc reads out of one golden."""
    core = Core(Core2Parser(allow_additional_properties=False), GOLDEN / f"{name}.core")
    return sorted(core._coredata.get_targets({}))


def test_exactly_the_expected_goldens_carry_a_sim_target() -> None:
    # An unintended sim target is as much a regression as a missing one.
    with_sim = sorted(name for name in GOLDEN_NAMES if "sim" in target_names(name))

    assert with_sim == sorted(EXPECTED_SIM)


@pytest.mark.parametrize("name", sorted(EXPECTED_SIM))
def test_strict_fusesoc_resolves_the_expected_sim_target(name: str) -> None:
    flags = {"target": "sim", "is_toplevel": True, "tool": "verilator"}

    core = Core(Core2Parser(allow_additional_properties=False), GOLDEN / f"{name}.core")

    actual = [
        (
            entry["name"],
            entry["file_type"],
            bool(entry.get("is_include_file", False)),
        )
        for entry in core.get_files(flags)
    ]
    toplevel, files, tool_options = EXPECTED_SIM[name]
    assert actual == files
    assert core.get_toplevel(flags) == toplevel
    assert core.get_tool_options(flags) == tool_options


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_strict_fusesoc_resolves_the_expected_default_target(name: str) -> None:
    flags = {"target": "default", "is_toplevel": True}

    # Strict mode raises fusesoc's SyntaxError on any schema violation.
    core = Core(Core2Parser(allow_additional_properties=False), GOLDEN / f"{name}.core")

    actual = [
        (
            entry["name"],
            entry["file_type"],
            bool(entry.get("is_include_file", False)),
        )
        for entry in core.get_files(flags)
    ]
    toplevel, files = EXPECTED[name]
    assert actual == files
    assert core.get_toplevel(flags) == toplevel
