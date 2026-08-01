"""Stage 2: Parse.

`base` holds the `ParserBackend` seam and the driver; `sv_slang` is the pyslang
backend. This module is where the two meet: `parse_sources` is the one call the
rest of the pipeline makes, and the only place that names a backend.

    facts = parse_sources(scan(root), defines=["USE_MUL", "WIDTH=32"])

VHDL files that Scan collected come back as `UnsupportedLanguage` warnings;
there is no VHDL backend yet.
"""

from __future__ import annotations

from collections.abc import Sequence

from autocore.models import ParseResult, ScanResult
from autocore.parse.base import PARALLEL_THRESHOLD, ParseError, ParserBackend, parse_all
from autocore.parse.sv_slang import SvSlangBackend

__all__ = [
    "PARALLEL_THRESHOLD",
    "ParseError",
    "ParserBackend",
    "SvSlangBackend",
    "parse_all",
    "parse_sources",
]


def parse_sources(
    scan: ScanResult,
    *,
    defines: Sequence[str] = (),
    max_workers: int | None = None,
) -> ParseResult:
    """Parse all scanned files using the default source-language backend.

At the moment, this means the pyslang-based Verilog/SystemVerilog backend.
Unsupported scanned languages are reported as warnings by the shared parse
driver.
"""
    return parse_all(
        scan,
        SvSlangBackend.for_tree(scan),
        defines=defines,
        max_workers=max_workers,
    )
