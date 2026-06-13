"""Cross-implementation parity — pure-Python vs ctypes-native (Phase 0).

The long-term contract (implementation plan §0, §1.3) is that any codec shipped
in *both* languages produces byte-identical output. The strategy is "pure-Python
reference first, freeze with golden fixtures, then make C match the fixtures", so
on ``main`` today the two implementations are expected to *diverge* for most of
the corpus — closing that gap is the work of Phases 1–5.

This test therefore measures the gap rather than demanding it be zero:

* If ``libtrl.so`` is not present, the whole module is skipped.
* Entries the limited native binding cannot yet express are skipped per-entry.
* For the rest, byte divergence is recorded as an expected failure (xfail); a
  surprise *pass* (xpass) flags an entry where C already matches the reference.

As later phases bring the C writer onto the golden bytes, entries here flip from
xfail to pass and the xfail handling can be removed entry-by-entry.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog._writer import TrlWriter
from trlog import _native
from tests.golden import corpus

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[1] / "golden" / "fixtures"

pytestmark = pytest.mark.skipif(
    _native._LIB is None,
    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH",
)


def _native_build(entry, path) -> bytes:
    """Build an entry with the ctypes-native writer, or raise to signal that the
    native binding cannot express it."""
    entry.build(_native._CtypesWriter, str(path))
    return pathlib.Path(path).read_bytes()


@pytest.mark.parametrize("entry", corpus.entries(), ids=corpus.names())
def test_native_matches_pure(entry, tmp_path):
    pure = corpus.build_bytes(entry, TrlWriter)

    try:
        native = _native_build(entry, tmp_path / f"{entry.name}.trl")
    except (TypeError, AttributeError, ValueError, NotImplementedError, OSError) as exc:
        pytest.skip(f"native binding cannot express '{entry.name}': "
                    f"{type(exc).__name__}: {exc}")
        return

    if native != pure:
        pytest.xfail(
            f"native output for '{entry.name}' diverges from the pure-Python "
            f"reference (pure={len(pure)}B, native={len(native)}B) — C-matches-"
            f"fixtures is later-phase work"
        )

    # Reaching here means parity already holds for this entry — assert it stays
    # tied to the frozen golden bytes too.
    expected = (FIXTURE_DIR / f"{entry.name}.trl").read_bytes()
    assert native == expected == pure
