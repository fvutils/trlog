"""Unit tests for HierarchyBlock."""

import pytest
from trlog._hierarchy import HierarchyBlock
from trlog._types import HierKind, ScopeType, VarDir, SignalEncoding
from trlog._exceptions import ZstFormatError


def make_simple_block():
    """Single root scope + two vars + one stream."""
    hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=10)
    hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
    hb.add_var(name_str_id=2, sig_type_id=1)
    hb.add_var(name_str_id=3, sig_type_id=2, var_dir=VarDir.VD_OUT)
    hb.add_stream(stream_type_id=1, name_str_id=4)
    hb.end_scope()
    return hb


def roundtrip(hb: HierarchyBlock, compress=True, use_zstd=False) -> HierarchyBlock:
    block = hb.encode_block(compress=compress, use_zstd=use_zstd)
    flags = block[1]
    hb2 = HierarchyBlock(hier_id=0, kind=HierKind.HK_DESIGN, name_str_id=0)
    hb2.read_block(block[10:], flags=flags)
    return hb2


class TestSimpleTree:
    def test_encode_decode_single_scope(self):
        hb2 = roundtrip(make_simple_block())
        assert hb2.header.hier_id == 1
        assert hb2.header.kind == HierKind.HK_DESIGN
        assert hb2.header.name_str_id == 10

    def test_vars_decoded(self):
        hb2 = roundtrip(make_simple_block())
        assert 1 in hb2.vars
        assert 2 in hb2.vars
        v2 = hb2.vars[2]
        assert v2.var_dir == VarDir.VD_OUT

    def test_stream_decoded(self):
        hb2 = roundtrip(make_simple_block())
        assert 1 in hb2.streams
        assert hb2.streams[1].stream_type_id == 1

    def test_root_children_structure(self):
        hb2 = roundtrip(make_simple_block())
        # root should have one child (the scope)
        from trlog._types import HScope
        assert len(hb2._root_children) == 1
        assert isinstance(hb2._root_children[0], HScope)
        scope = hb2._root_children[0]
        assert scope.name_str_id == 1
        assert len(scope.children) == 3  # 2 vars + 1 stream


class TestNestedScopes:
    def test_three_level_nesting(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=2)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=3)
        hb.add_var(name_str_id=4, sig_type_id=1)
        hb.end_scope()
        hb.end_scope()
        hb.end_scope()

        hb2 = roundtrip(hb)
        from trlog._types import HScope
        lvl1 = hb2._root_children[0]
        assert isinstance(lvl1, HScope) and lvl1.name_str_id == 1
        lvl2 = lvl1.children[0]
        assert isinstance(lvl2, HScope) and lvl2.name_str_id == 2
        lvl3 = lvl2.children[0]
        assert isinstance(lvl3, HScope) and lvl3.name_str_id == 3
        assert len(lvl3.children) == 1  # var


class TestAttrs:
    def test_attr_on_scope(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_attr(key_str_id=10, value_str_id=20)
        hb.end_scope()

        hb2 = roundtrip(hb)
        scope = hb2._root_children[0]
        assert len(scope.attrs) == 1
        assert scope.attrs[0].key_str_id == 10
        assert scope.attrs[0].value_str_id == 20

    def test_attr_on_var(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_var(name_str_id=2, sig_type_id=1)
        hb.add_attr(key_str_id=11, value_str_id=21)
        hb.end_scope()

        hb2 = roundtrip(hb)
        var = hb2._root_children[0].children[0]
        assert var.attrs[0].key_str_id == 11

    def test_attr_on_stream(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_stream(stream_type_id=1, name_str_id=5)
        hb.add_attr(key_str_id=12, value_str_id=22)
        hb.end_scope()

        hb2 = roundtrip(hb)
        stream = hb2._root_children[0].children[0]
        assert stream.attrs[0].value_str_id == 22


class TestSrcLocation:
    def test_scope_src_location_roundtrip(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1, src_file_str_id=50, src_line=42)
        hb.end_scope()

        hb2 = roundtrip(hb)
        scope = hb2._root_children[0]
        assert scope.src_file_str_id == 50
        assert scope.src_line == 42

    def test_var_src_location_roundtrip(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_var(name_str_id=2, sig_type_id=1, src_file_str_id=51, src_line=99)
        hb.end_scope()

        hb2 = roundtrip(hb)
        var = hb2.vars[1]
        assert var.src_file_str_id == 51
        assert var.src_line == 99

    def test_var_src_location_absent(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_var(name_str_id=2, sig_type_id=1)
        hb.end_scope()

        hb2 = roundtrip(hb)
        var = hb2.vars[1]
        assert var.src_file_str_id == 0
        assert var.src_line == 0

    def test_var_driver_attrs_roundtrip(self):
        """Driver info stored as H_ATTR with well-known keys."""
        from trlog._types import WellKnownAttr
        driver_file_key = 60
        driver_line_key = 61
        driver_file_val = 70
        driver_line_val = 71  # string ID for "17"

        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_var(name_str_id=2, sig_type_id=1)
        hb.add_attr(key_str_id=driver_file_key, value_str_id=driver_file_val)
        hb.add_attr(key_str_id=driver_line_key, value_str_id=driver_line_val)
        hb.end_scope()

        hb2 = roundtrip(hb)
        var = hb2.vars[1]
        assert len(var.attrs) == 2
        assert var.attrs[0].key_str_id == driver_file_key
        assert var.attrs[0].value_str_id == driver_file_val
        assert var.attrs[1].key_str_id == driver_line_key
        assert var.attrs[1].value_str_id == driver_line_val

    def test_stream_src_attrs_roundtrip(self):
        """Stream source location stored as H_ATTR."""
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        hb.add_stream(stream_type_id=1, name_str_id=5)
        hb.add_attr(key_str_id=80, value_str_id=90)   # src.file
        hb.add_attr(key_str_id=81, value_str_id=91)   # src.line
        hb.end_scope()

        hb2 = roundtrip(hb)
        stream = hb2.streams[1]
        assert len(stream.attrs) == 2
        assert stream.attrs[0].key_str_id == 80
        assert stream.attrs[1].key_str_id == 81


class TestErrors:
    def test_upscope_without_scope_raises(self):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        with pytest.raises(ZstFormatError):
            hb.end_scope()

    def test_upscope_in_encoded_stream_raises(self):
        """Manually craft a payload with H_UPSCOPE at the start."""
        from trlog._types import HierTag
        from trlog._codec import encode_uvarint
        # Hierarchy header
        inner = encode_uvarint(1) + bytes([int(HierKind.HK_DESIGN)]) + encode_uvarint(0)
        inner += bytes([HierTag.H_UPSCOPE])
        import zlib, struct
        uncompressed_len = len(inner)
        compressed = zlib.compress(inner)
        payload = struct.pack('<Q', uncompressed_len) + compressed
        hb2 = HierarchyBlock(hier_id=0, kind=HierKind.HK_DESIGN, name_str_id=0)
        with pytest.raises(ZstFormatError):
            hb2.read_block(payload, flags=0x01)


class TestCompression:
    def test_zlib_roundtrip(self):
        hb2 = roundtrip(make_simple_block(), compress=True, use_zstd=False)
        assert 1 in hb2.vars and 2 in hb2.vars

    def test_uncompressed_roundtrip(self):
        hb2 = roundtrip(make_simple_block(), compress=False)
        assert 1 in hb2.vars

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("zstandard"),
        reason="zstandard not installed",
    )
    def test_zstd_roundtrip(self):
        hb2 = roundtrip(make_simple_block(), compress=True, use_zstd=True)
        assert 1 in hb2.vars


class TestScopeTypes:
    @pytest.mark.parametrize("st", list(ScopeType))
    def test_all_scope_types_encode_correctly(self, st):
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        hb.begin_scope(st, name_str_id=1)
        hb.end_scope()
        hb2 = roundtrip(hb)
        scope = hb2._root_children[0]
        assert scope.scope_type == st
