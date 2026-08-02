"""Emit every fixture's ``.core``: ``python tests/emit_fixtures.py OUTDIR``.

Two callers, one code path:

* Regenerating the goldens after a deliberate output change:
  ``python tests/emit_fixtures.py tests/golden`` — then review the diff.
* The CI determinism job runs this twice under two different
  ``PYTHONHASHSEED`` values and byte-compares the two output directories
  with ``diff -r``. Comparing run against run is the point: it catches an
  unsorted set leaking into the output even if both runs happened to
  satisfy some weaker check.

Not a test module — pytest never collects it (no ``test_`` prefix).
"""

from __future__ import annotations

import sys
from pathlib import Path

from autocore.emit import emit, to_manifest, write_core
from autocore.parse import parse_sources
from autocore.resolve import resolve
from autocore.scan import scan

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def emit_fixture(root: Path) -> str:
    """The full pipeline for one fixture tree, defaults only (``--yes`` mode)."""
    tree = scan(root)
    model = resolve(tree, parse_sources(tree))
    return emit(to_manifest(model, root))


def main(outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    for fixture in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        write_core(emit_fixture(fixture), outdir / f"{fixture.name}.core", force=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} OUTDIR")
    main(Path(sys.argv[1]))
