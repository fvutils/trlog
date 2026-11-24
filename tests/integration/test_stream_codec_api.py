"""Public stream-type codec API (issue #13).

* ``TrlWriter.set_stream_codec`` records a codec ID, version and parameters on
  a stream type (type registry stream entry v2).
* ``TrlReader.stream_codec`` returns them, with the codec ID resolved to its
  string; ``None`` for a stream type without a codec.

Blocks are decoded by the codec ID each block carries, not by the stream
type's codec.
"""

from __future__ import annotations

import pytest

from trlog._reader import TrlReader
from trlog._writer import TrlWriter


def test_stream_codec_roundtrip(tmp_path):
    path = tmp_path / "sc.trl"
    with TrlWriter(str(path)) as w:
        plain = w.add_stream_type("plain")
        coded = w.add_stream_type("coded", kind="bus")
        w.set_stream_codec(coded, "com.example.bus", version=3, params=b"\x01\x02")
        w.add_stream_metadata(coded, "width", 32)
    with TrlReader(str(path)) as r:
        assert r.stream_codec(plain) is None
        assert r.stream_codec(coded) == ("com.example.bus", 3, b"\x01\x02")
        assert r.stream_codec(999) is None
        # Other stream-type data is unaffected by the v2 entry.
        assert r.stream_metadata(coded) == {"width": 32}


def test_stream_codec_defaults(tmp_path):
    path = tmp_path / "scd.trl"
    with TrlWriter(str(path)) as w:
        st = w.add_stream_type("s")
        w.set_stream_codec(st, "com.example.s")
    with TrlReader(str(path)) as r:
        assert r.stream_codec(st) == ("com.example.s", 0, b"")


def test_stream_codec_errors(tmp_path):
    with TrlWriter(str(tmp_path / "e.trl")) as w:
        st = w.add_stream_type("s")
        with pytest.raises(KeyError):
            w.set_stream_codec(st + 100, "com.example.s")
        with pytest.raises(ValueError):
            w.set_stream_codec(st, "")
