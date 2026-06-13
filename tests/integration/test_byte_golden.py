"""Byte-for-byte golden test — the no-regression contract (Phase 0).

The pure-Python writer is the reference implementation. For every entry in the
golden corpus we re-generate the trace in memory and assert it is byte-identical
to the fixture checked in under ``tests/golden/fixtures/``. Any change to the
on-disk layout therefore shows up here as a failing test with a precise diff,
and the fixtures are only refreshed deliberately via
``python -m tests.golden.generate``.

See ``docs/design/pluggable-codecs-implementation-plan.md`` §1 (no-regression /
byte-compat) and §4 Phase 0.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog._writer import TrlWriter
from tests.golden import corpus

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[1] / "golden" / "fixtures"


def _first_diff(a: bytes, b: bytes) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - 4)
            return (f"first diff at offset {i}: "
                    f"fixture={a[i]:#04x} actual={b[i]:#04x}; "
                    f"fixture[{lo}:{i + 4}]={a[lo:i + 4].hex()} "
                    f"actual[{lo}:{i + 4}]={b[lo:i + 4].hex()}")
    return f"common prefix identical; lengths differ fixture={len(a)} actual={len(b)}"


@pytest.mark.parametrize("entry", corpus.entries(), ids=corpus.names())
def test_pure_python_matches_fixture(entry):
    fixture_path = FIXTURE_DIR / f"{entry.name}.trl"
    assert fixture_path.exists(), (
        f"missing golden fixture {fixture_path}; run "
        f"`python -m tests.golden.generate`"
    )
    expected = fixture_path.read_bytes()
    actual = corpus.build_bytes(entry, TrlWriter)
    assert actual == expected, (
        f"{entry.name}: pure-Python output diverged from golden fixture. "
        f"{_first_diff(expected, actual)}. If this change is intentional, "
        f"regenerate with `python -m tests.golden.generate` and review the diff."
    )


def test_every_fixture_has_a_corpus_entry():
    """Guard against orphaned fixtures left behind after a corpus rename."""
    on_disk = {p.stem for p in FIXTURE_DIR.glob("*.trl")}
    declared = set(corpus.names())
    assert on_disk == declared, (
        f"fixtures on disk and corpus entries disagree: "
        f"only on disk={on_disk - declared}, only in corpus={declared - on_disk}"
    )
