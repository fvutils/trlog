"""C reader robustness and C writer timezero (issue #17).

The C hierarchy scan read one field too few for each ``H_SCOPE`` and stopped
at the first ``H_ATTR2``, so it lost streams and variables declared after
them. The C type-registry reader stopped at enum and stream declarations. The
C writer always wrote ``timezero = 0``.
"""

from __future__ import annotations

import ctypes

import pytest

from trlog import _native
from trlog._reader import TrlReader
from trlog._writer import TrlWriter
from trlog._types import FieldDef, FieldType, HierKind, ScopeType, SignalEncoding

pytestmark = pytest.mark.skipif(
    _native._LIB is None,
    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH",
)

VC_CB = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint64,
                         ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32, ctypes.c_void_p)


def _write_py(path):
    """A Python-written file with an enum type, a stream decl, nested scopes
    and H_ATTR2 records before the variable and streams that follow."""
    with TrlWriter(str(path), compress=False) as w:
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        w.add_enum_type("color", [("RED", 0), ("GREEN", 1)])
        stype = w.add_stream_type("bus")
        w.add_txn_schema("item", [FieldDef(name_str_id=w.intern("c"), field_type=FieldType.FT_U32)])
        with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_typed_attr("depth", 3)
            h.begin_scope(ScopeType.ST_MODULE, "sub")
            h.add_typed_attr("ratio", 1.5)
            vid = h.add_var("v", sig_t)
            h.add_typed_attr("note", 7)
            s0 = h.add_stream(stype, "s0")
            h.add_typed_attr("k", 1)
            s1 = h.add_stream(stype, "s1")
            h.end_scope()
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            vc.add_change(vid, 5, 0x5A)
    return vid, (s0, s1)


def test_c_reader_reads_past_attr2_and_enum_types(tmp_path):
    p = tmp_path / "py.trl"
    vid, _ = _write_py(p)
    lib = _native._LIB
    lib.trl_reader_open.argtypes = [ctypes.c_char_p]
    lib.trl_reader_open.restype = ctypes.c_void_p
    lib.trl_iter_vc.argtypes = [ctypes.c_void_p, VC_CB, ctypes.c_void_p]
    lib.trl_iter_vc.restype = ctypes.c_int
    lib.trl_reader_close.argtypes = [ctypes.c_void_p]
    r = lib.trl_reader_open(str(p).encode())
    assert r, "C reader failed to open a file with enum types and H_ATTR2"
    seen = []
    cb = VC_CB(lambda var, t, val, n, u: seen.append((var, t, bytes(val[:n]))))
    try:
        assert lib.trl_iter_vc(r, cb, None) == 0
    finally:
        lib.trl_reader_close(r)
    assert [(v, t) for v, t, _ in seen] == [(vid, 5)]
    assert int.from_bytes(seen[0][2], "little") == 0x5A


def test_c_query_finds_streams_after_scopes_and_attr2(tmp_path):
    p = tmp_path / "py.trl"
    _write_py(p)
    lib = _native._LIB
    lib.trl_rdb_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.trl_rdb_open.restype = ctypes.c_void_p
    lib.trl_rdb_num_streams.argtypes = [ctypes.c_void_p]
    lib.trl_rdb_num_streams.restype = ctypes.c_uint32
    lib.trl_rdb_stream_find.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.trl_rdb_stream_find.restype = ctypes.c_void_p
    lib.trl_rdb_close.argtypes = [ctypes.c_void_p]
    db = lib.trl_rdb_open(b"robust", str(p).encode())
    assert db
    try:
        assert lib.trl_rdb_num_streams(db) == 2
        assert lib.trl_rdb_stream_find(db, b"s1")
    finally:
        lib.trl_rdb_close(db)


@pytest.mark.parametrize("tz", [0, 1234, -77])
def test_c_writer_timezero(tmp_path, tz):
    p = tmp_path / "tz.trl"
    with _native._CtypesWriter(str(p), timezero=tz, compress=False) as w:
        w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    with TrlReader(str(p)) as r:
        assert r.timezero == tz


def test_c_reader_skips_enum_and_stream_decls_before_schemas(tmp_path, monkeypatch):
    """Readers must not depend on entry order: put the enum and stream decls
    first so the C reader has to skip them to reach the schema."""
    from trlog import _type_registry as tr
    orig = tr.TypeRegistry._encode_payload

    def enums_first(self):
        saved = (self._signal_types, self._txn_schemas)
        self._signal_types, self._txn_schemas = {}, {}
        head = orig(self)
        self._signal_types, self._txn_schemas = saved
        saved = (self._enum_types, self._stream_decls)
        self._enum_types, self._stream_decls = {}, {}
        tail = orig(self)
        self._enum_types, self._stream_decls = saved
        n_head, off = tr.decode_uvarint(head, 0)
        n_tail, off2 = tr.decode_uvarint(tail, 0)
        return tr.encode_uvarint(n_head + n_tail) + head[off:] + tail[off2:]

    monkeypatch.setattr(tr.TypeRegistry, "_encode_payload", enums_first)
    p = tmp_path / "py.trl"
    _write_py(p)
    with TrlReader(str(p)) as r:   # the Python reader accepts the order
        assert r.type_registry.get_txn_schema(1) is not None
    lib = _native._LIB
    lib.trl_rdb_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.trl_rdb_open.restype = ctypes.c_void_p
    lib.trl_rdb_schema_find.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.trl_rdb_schema_find.restype = ctypes.c_void_p
    lib.trl_rdb_close.argtypes = [ctypes.c_void_p]
    db = lib.trl_rdb_open(b"robust_order", str(p).encode())
    assert db
    try:
        assert lib.trl_rdb_schema_find(db, b"item")
    finally:
        lib.trl_rdb_close(db)
