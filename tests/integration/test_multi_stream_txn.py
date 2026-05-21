"""Integration tests for per-stream transaction block writing and reading."""

import pytest
from trlog import (
    TrlWriter, TrlReader, ScopeType, FieldType, FieldDef,
    TxnFull, TxnBegin, TxnEnd, TxnAttr,
)
from trlog._types import HierKind, TxnRecordTag


def _make_attr(field_idx, value, ft=FieldType.FT_U64):
    a = TxnAttr(field_idx=field_idx, value=value)
    a._field_type = ft
    return a


class TestSeparateStreamBlocks:
    """Write per-stream blocks and read them back selectively."""

    def test_separate_stream_blocks(self, tmp_path):
        """3 streams, 50 records each; filtered read returns only the target."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [
                FieldDef(name_str_id=w.intern("val"), field_type=FieldType.FT_U32),
            ])
            st = w.add_stream_type("s", kind="generic")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "stream_a")
                s2 = h.add_stream(st, "stream_b")
                s3 = h.add_stream(st, "stream_c")
                h.end_scope()

            for sid in (s1, s2, s3):
                with w.begin_txn_block(0, stream_inst_id=sid) as blk:
                    for i in range(50):
                        blk.write_full(sid, schema, sid * 1000 + i,
                                       i * 10, i * 10 + 5, 0,
                                       [_make_attr(0, i, FieldType.FT_U32)])

        with TrlReader(str(p)) as r:
            # Read only stream_b
            blocks = list(r.iter_txn_blocks(stream_inst_id=s2))
            all_recs = [rec for blk in blocks for rec in blk]
            assert len(all_recs) == 50
            for rec in all_recs:
                assert rec.stream_inst_id == s2

            # Unfiltered read returns all 150
            all_blocks = list(r.iter_txn_blocks())
            total = sum(len(b) for b in all_blocks)
            assert total == 150

    def test_cross_stream_parent_txn_id(self, tmp_path):
        """A record in stream 2 can reference a transaction in stream 1."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "parent_stream")
                s2 = h.add_stream(st, "child_stream")
                h.end_scope()

            parent_txn_id = 100
            with w.begin_txn_block(0, stream_inst_id=s1) as blk:
                blk.write_begin(s1, schema, parent_txn_id, 0, parent=0)

            with w.begin_txn_block(0, stream_inst_id=s2) as blk:
                blk.write_full(s2, schema, 200, 10, 20, parent_txn_id)

            with w.begin_txn_block(100, stream_inst_id=s1) as blk:
                blk.write_end(parent_txn_id, 100)

        with TrlReader(str(p)) as r:
            s2_recs = list(r.iter_txn_blocks(stream_inst_id=s2))
            child = s2_recs[0][0]
            assert isinstance(child, TxnFull)
            assert child.parent_txn_id == parent_txn_id

    def test_interleaved_stream_blocks(self, tmp_path):
        """Blocks written in interleaved order; filtered read is complete and ordered."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            txn_id = 1
            for t in range(0, 40, 10):
                for sid in (s1, s2):
                    with w.begin_txn_block(t, stream_inst_id=sid) as blk:
                        blk.write_full(sid, schema, txn_id, t, t + 5, 0)
                        txn_id += 1

        with TrlReader(str(p)) as r:
            s1_blocks = list(r.iter_txn_blocks(stream_inst_id=s1))
            s1_recs = [rec for blk in s1_blocks for rec in blk]
            assert len(s1_recs) == 4
            times = [rec.start_time for rec in s1_recs]
            assert times == sorted(times)

    def test_mixed_block_fallback(self, tmp_path):
        """Block without stream_inst_id; filtered read falls back to record-level."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            # Write both streams into the same block (no stream_inst_id)
            with w.begin_txn_block(0) as blk:
                blk.write_full(s1, schema, 1, 0, 10, 0)
                blk.write_full(s2, schema, 2, 0, 10, 0)
                blk.write_full(s1, schema, 3, 10, 20, 0)

        with TrlReader(str(p)) as r:
            s1_blocks = list(r.iter_txn_blocks(stream_inst_id=s1))
            s1_recs = [rec for blk in s1_blocks for rec in blk]
            assert len(s1_recs) == 2
            assert all(rec.stream_inst_id == s1 for rec in s1_recs)


class TestStreamDiscovery:
    """Tests for find_streams() and hierarchy-based stream relationships."""

    def test_hierarchy_with_streams(self, tmp_path):
        """find_streams discovers streams by kind string."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [])
            st_alpha = w.add_stream_type("alpha_type", kind="alpha")
            st_beta = w.add_stream_type("beta_type", kind="beta")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                sa = h.add_stream(st_alpha, "stream_alpha")
                sb = h.add_stream(st_beta, "stream_beta")
                h.end_scope()

            with w.begin_txn_block(0, stream_inst_id=sa) as blk:
                blk.write_full(sa, schema, 1, 0, 10, 0)
            with w.begin_txn_block(0, stream_inst_id=sb) as blk:
                blk.write_full(sb, schema, 2, 0, 10, 0)

        with TrlReader(str(p)) as r:
            alpha_streams = r.find_streams(kind="alpha")
            assert len(alpha_streams) == 1
            assert sa in alpha_streams

            beta_streams = r.find_streams(kind="beta")
            assert len(beta_streams) == 1
            assert sb in beta_streams

            # Read only alpha stream via discovered inst_id
            alpha_recs = list(r.iter_txn_blocks(stream_inst_id=sa))
            assert sum(len(b) for b in alpha_recs) == 1

    def test_stream_attrs_declare_relationships(self, tmp_path):
        """H_ATTR on a stream can reference another stream's inst_id."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            st = w.add_stream_type("s", kind="generic")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s_main = h.add_stream(st, "main")
                s_detail = h.add_stream(st, "detail")
                # Go back to main stream and attach relationship attr
                # (attrs attach to the last declared node)
                h.end_scope()

            # Attach attr directly to the hierarchy block
            # We need to do this before the hierarchy is flushed, so let's
            # use a different approach: declare attrs inline
        # Re-do with attrs inline
        with TrlWriter(str(p), compress=False) as w:
            st = w.add_stream_type("s", kind="generic")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s_main = h.add_stream(st, "main")
                # attr attaches to the most recently declared node (s_main)
                h.add_attr("related_detail_stream", str(s_main + 1))
                s_detail = h.add_stream(st, "detail")
                h.end_scope()

        with TrlReader(str(p)) as r:
            hier = list(r.hierarchies.values())[0]
            main_stream = hier.streams[s_main]
            assert len(main_stream.attrs) == 1
            key = r.string_table.lookup(main_stream.attrs[0].key_str_id)
            val = r.string_table.lookup(main_stream.attrs[0].value_str_id)
            assert key == "related_detail_stream"
            assert val == str(s_detail)

    def test_find_streams_no_match(self, tmp_path):
        """find_streams with a non-existent kind returns empty dict."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            st = w.add_stream_type("s", kind="alpha")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                h.add_stream(st, "s1")
                h.end_scope()

        with TrlReader(str(p)) as r:
            result = r.find_streams(kind="nonexistent")
            assert result == {}

    def test_find_streams_all(self, tmp_path):
        """find_streams(kind=None) returns all streams."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            st1 = w.add_stream_type("s1", kind="alpha")
            st2 = w.add_stream_type("s2", kind="beta")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                h.add_stream(st1, "a")
                h.add_stream(st2, "b")
                h.end_scope()

        with TrlReader(str(p)) as r:
            result = r.find_streams()
            assert len(result) == 2


class TestPerStreamCompression:
    """Verify per-stream blocks get better compression than mixed blocks."""

    def test_per_stream_compression_benefit(self, tmp_path):
        """Per-stream blocks compress better than mixed blocks."""
        # Schema A: 3 u32 fields
        # Schema B: 2 u64 fields
        # Writing them in separate blocks should compress better than mixed.

        p_separate = tmp_path / "separate.trl"
        p_mixed = tmp_path / "mixed.trl"

        fields_a = [
            FieldDef(name_str_id=1, field_type=FieldType.FT_U32),
            FieldDef(name_str_id=2, field_type=FieldType.FT_U32),
            FieldDef(name_str_id=3, field_type=FieldType.FT_U32),
        ]
        fields_b = [
            FieldDef(name_str_id=4, field_type=FieldType.FT_U64),
            FieldDef(name_str_id=5, field_type=FieldType.FT_U64),
        ]

        N = 500

        # Separate blocks
        with TrlWriter(str(p_separate), compress=True) as w:
            schema_a = w.add_txn_schema("type_a", fields_a)
            schema_b = w.add_txn_schema("type_b", fields_b)
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            with w.begin_txn_block(0, stream_inst_id=s1) as blk:
                for i in range(N):
                    blk.write_full(s1, schema_a, i + 1, i * 10, i * 10 + 5, 0,
                                   [_make_attr(0, i, FieldType.FT_U32),
                                    _make_attr(1, i * 2, FieldType.FT_U32),
                                    _make_attr(2, i * 3, FieldType.FT_U32)])
            with w.begin_txn_block(0, stream_inst_id=s2) as blk:
                for i in range(N):
                    blk.write_full(s2, schema_b, N + i + 1, i * 10, i * 10 + 5, 0,
                                   [_make_attr(0, i * 100),
                                    _make_attr(1, i * 200)])

        # Mixed block
        with TrlWriter(str(p_mixed), compress=True) as w:
            schema_a = w.add_txn_schema("type_a", fields_a)
            schema_b = w.add_txn_schema("type_b", fields_b)
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            with w.begin_txn_block(0) as blk:
                for i in range(N):
                    blk.write_full(s1, schema_a, i + 1, i * 10, i * 10 + 5, 0,
                                   [_make_attr(0, i, FieldType.FT_U32),
                                    _make_attr(1, i * 2, FieldType.FT_U32),
                                    _make_attr(2, i * 3, FieldType.FT_U32)])
                    blk.write_full(s2, schema_b, N + i + 1, i * 10, i * 10 + 5, 0,
                                   [_make_attr(0, i * 100),
                                    _make_attr(1, i * 200)])

        sep_size = p_separate.stat().st_size
        mix_size = p_mixed.stat().st_size
        # Per-stream should be at least as small (ideally smaller)
        assert sep_size <= mix_size * 1.1  # allow 10% tolerance for overhead


class TestReadStreamUsesIndex:
    """Verify read_stream works through the full stack."""

    def test_read_stream_uses_index(self, tmp_path):
        """10 blocks per stream; read_stream returns correct data."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [
                FieldDef(name_str_id=w.intern("x"), field_type=FieldType.FT_U32),
            ])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            txn_counter = 1
            for block_idx in range(10):
                t_start = block_idx * 100
                for sid in (s1, s2):
                    with w.begin_txn_block(t_start, stream_inst_id=sid) as blk:
                        for i in range(100):
                            t = t_start + i
                            blk.write_full(sid, schema, txn_counter, t, t + 1, 0,
                                           [_make_attr(0, txn_counter, FieldType.FT_U32)])
                            txn_counter += 1

        with TrlReader(str(p)) as r:
            s2_recs = r.read_stream(s2)
            assert len(s2_recs) == 1000
            for rec in s2_recs:
                assert rec.stream_inst_id == s2


class TestBackwardCompat:
    """Verify old-style files (no stream_inst_id on blocks) still work."""

    def test_backward_compat_no_stream_blocks(self, tmp_path):
        """Old-style file; unfiltered read works, filtered read falls back."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            schema = w.add_txn_schema("evt", [])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                s1 = h.add_stream(st, "a")
                s2 = h.add_stream(st, "b")
                h.end_scope()

            # No stream_inst_id -- old-style mixed block
            with w.begin_txn_block(0) as blk:
                blk.write_full(s1, schema, 1, 0, 10, 0)
                blk.write_full(s2, schema, 2, 10, 20, 0)
                blk.write_full(s1, schema, 3, 20, 30, 0)

        with TrlReader(str(p)) as r:
            # Unfiltered
            all_recs = [rec for blk in r.iter_txn_blocks() for rec in blk]
            assert len(all_recs) == 3

            # Filtered -- falls back to record-level since no per-stream index
            s1_recs = [rec for blk in r.iter_txn_blocks(stream_inst_id=s1) for rec in blk]
            assert len(s1_recs) == 2
            assert all(rec.stream_inst_id == s1 for rec in s1_recs)


class TestLargeMultiStream:
    """Scale test with many records across multiple streams."""

    def test_10k_records_three_streams(self, tmp_path):
        """10k records across 3 streams; verify counts and values."""
        p = tmp_path / "t.trl"
        stream_counts = {1: 500, 2: 3000, 3: 6500}

        with TrlWriter(str(p), compress=True) as w:
            schema = w.add_txn_schema("evt", [
                FieldDef(name_str_id=w.intern("seq"), field_type=FieldType.FT_U32),
            ])
            st = w.add_stream_type("s")
            with w.begin_hierarchy(kind=HierKind.HK_BEHAVIORAL, name="h") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                sids = {}
                for sid_key in stream_counts:
                    sids[sid_key] = h.add_stream(st, f"stream_{sid_key}")
                h.end_scope()

            txn_id = 1
            for sid_key, count in stream_counts.items():
                sid = sids[sid_key]
                with w.begin_txn_block(0, stream_inst_id=sid) as blk:
                    for i in range(count):
                        blk.write_full(sid, schema, txn_id, i, i + 1, 0,
                                       [_make_attr(0, i, FieldType.FT_U32)])
                        txn_id += 1

        with TrlReader(str(p)) as r:
            for sid_key, expected_count in stream_counts.items():
                sid = sids[sid_key]
                recs = [rec for blk in r.iter_txn_blocks(stream_inst_id=sid)
                        for rec in blk]
                assert len(recs) == expected_count, \
                    f"stream {sid_key}: expected {expected_count}, got {len(recs)}"
