"""Per-bit 4-state and 9-state values round-trip (issues #3 #4).

A multi-bit 4-state value given as a string or list such as ``"01xz"`` used to
be stored as all X, and the characters ``'0'``/``'1'`` in a 9-state value
were stored as 0.
"""

from __future__ import annotations

import pytest

from trlog import _native
from trlog._reader import TrlReader
from trlog._writer import TrlWriter
from trlog._types import ScopeType, SignalEncoding


@pytest.fixture(params=["py", "c"])
def writer(request):
    if request.param == "c":
        if _native._LIB is None:
            pytest.skip("libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH")
        return _native._CtypesWriter
    return TrlWriter


def _roundtrip(W, tmp_path, enc, width, values):
    path = str(tmp_path / "ms.trl")
    with W(path, compress=False) as w:
        st = w.add_signal_type(enc, width)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            vid = h.add_var("s", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i, v in enumerate(values):
                vc.add_change(vid, i * 10, v)
    return [c.value for c in TrlReader(path).read_signal(vid)]


@pytest.mark.parametrize("value,want", [
    ("01xz", [0, 1, "X", "Z"]),
    ("10XZ", [1, 0, "X", "Z"]),
    (["0", 1, "x", "Z"], [0, 1, "X", "Z"]),
    ("X", ["X"] * 4),          # one symbol fills every bit
    ("z", ["Z"] * 4),
    ("0110", 0b0110),          # all known: read back as an int
    ("1x", [0, 0, 1, "X"]),    # shorter: zero-extended on the left
])
def test_4state_per_bit(writer, tmp_path, value, want):
    assert _roundtrip(writer, tmp_path, SignalEncoding.SE_4STATE, 4, [value]) == [want]


@pytest.mark.parametrize("value,want", [
    ("01XZ", [0, 1, "X", "Z"]),
    (["1", "0", "h", "L"], [1, 0, "H", "L"]),
    ("UW-1", ["U", "W", "-", 1]),
    ("1010", 0b1010),
    ("X", ["X"] * 4),
])
def test_9state_per_bit(writer, tmp_path, value, want):
    assert _roundtrip(writer, tmp_path, SignalEncoding.SE_9STATE, 4, [value]) == [want]


@pytest.mark.parametrize("enc", [SignalEncoding.SE_4STATE, SignalEncoding.SE_9STATE])
def test_unknown_bit_state_rejected(writer, tmp_path, enc):
    with pytest.raises(ValueError):
        _roundtrip(writer, tmp_path, enc, 4, ["01q1"])
