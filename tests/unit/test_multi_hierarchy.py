"""Unit tests for multiple hierarchy trees in one file (multi-hierarchy)."""

import struct
import pytest
from trlog._hierarchy import HierarchyBlock
from trlog._types import HierKind, ScopeType, VarDir
from trlog._codec import encode_uvarint


class TestMultiHierarchyUnit:
    def test_two_hierarchies_independent(self):
        """Each HierarchyBlock carries independent data."""
        design = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=1)
        design.begin_scope(ScopeType.ST_MODULE, name_str_id=10)
        design.add_var(name_str_id=11, sig_type_id=1)
        design.end_scope()

        sw = HierarchyBlock(hier_id=2, kind=HierKind.HK_SW, name_str_id=2)
        sw.begin_scope(ScopeType.ST_TASK, name_str_id=20)
        sw.add_var(name_str_id=21, sig_type_id=2)
        sw.end_scope()

        # encode + decode both blocks
        def rt(hb: HierarchyBlock) -> HierarchyBlock:
            block = hb.encode_block()
            flags = block[1]
            hb2 = HierarchyBlock(0, HierKind.HK_DESIGN, 0)
            hb2.read_block(block[10:], flags=flags)
            return hb2

        d2 = rt(design)
        s2 = rt(sw)

        assert d2.header.hier_id == 1
        assert d2.header.kind == HierKind.HK_DESIGN
        assert s2.header.hier_id == 2
        assert s2.header.kind == HierKind.HK_SW

    def test_hier_ids_unique(self):
        d = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=1)
        s = HierarchyBlock(hier_id=2, kind=HierKind.HK_SW,     name_str_id=2)
        assert d.header.hier_id != s.header.hier_id

    def test_var_ids_independent(self):
        """var_id 1 in hierarchy 1 is unrelated to var_id 1 in hierarchy 2."""
        h1 = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        h1.begin_scope(ScopeType.ST_MODULE, name_str_id=1)
        vid1 = h1.add_var(name_str_id=2, sig_type_id=1)
        h1.end_scope()

        h2 = HierarchyBlock(hier_id=2, kind=HierKind.HK_SW, name_str_id=0)
        h2.begin_scope(ScopeType.ST_TASK, name_str_id=3)
        vid2 = h2.add_var(name_str_id=4, sig_type_id=2)
        h2.end_scope()

        # Both start at var_id=1 — that's expected and correct
        assert vid1 == 1
        assert vid2 == 1


class TestV1BackwardCompat:
    """v1 files have no hierarchy header in BLK_HIERARCHY.
    
    A v1 file is identified by version_major=1.  Readers encountering a v1 file
    may treat the single hierarchy block as hier_id=1, kind=HK_DESIGN.
    We synthesise a raw v1 hierarchy payload (no hierarchy header) and verify
    the reader decodes it with the correct defaults when using read_block_v1().
    """

    def _make_v1_payload(self):
        """Craft a minimal v1 hierarchy payload without a hierarchy header."""
        import zlib
        # A v1 block has NO hierarchy header (no hier_id/kind/name_str_id).
        # It starts directly with the tag sequence.
        from trlog._types import HierTag
        inner = bytearray()
        # H_SCOPE
        inner.append(HierTag.H_SCOPE)
        inner.append(0)                     # ST_MODULE
        inner += encode_uvarint(5)          # name_str_id
        inner += encode_uvarint(0)          # component_str_id
        inner += encode_uvarint(0)          # src_file_str_id
        inner += encode_uvarint(0)          # src_line
        # H_UPSCOPE
        inner.append(HierTag.H_UPSCOPE)
        inner = bytes(inner)
        uncompressed_len = len(inner)
        compressed = zlib.compress(inner)
        return struct.pack('<Q', uncompressed_len) + compressed

    def test_v1_read_as_design_hier1(self):
        payload = self._make_v1_payload()
        hb = HierarchyBlock(hier_id=1, kind=HierKind.HK_DESIGN, name_str_id=0)
        # read_block_v1 skips the hierarchy header
        hb.read_block_v1(payload, flags=0x01)
        assert hb.header.hier_id == 1
        assert hb.header.kind == HierKind.HK_DESIGN
        from trlog._types import HScope
        assert len(hb._root_children) == 1
        assert hb._root_children[0].name_str_id == 5
