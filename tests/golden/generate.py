"""Regenerate the golden fixtures under ``tests/golden/fixtures/``.

Run deliberately after an intentional, reviewed format change::

    PYTHONPATH=python/src python -m tests.golden.generate

The pure-Python writer is the reference implementation; its bytes are the
contract that both the byte-golden test and (eventually) the C implementation
are measured against. Never wire this into a test — fixtures change only on
human review of the resulting diff.
"""

from __future__ import annotations

import pathlib

from trlog._writer import TrlWriter

from . import corpus

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def main() -> None:
    FIXTURE_DIR.mkdir(exist_ok=True)
    for entry in corpus.entries():
        data = corpus.build_bytes(entry, TrlWriter)
        out = FIXTURE_DIR / f"{entry.name}.trl"
        out.write_bytes(data)
        print(f"wrote {out.relative_to(FIXTURE_DIR.parent.parent.parent)} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
