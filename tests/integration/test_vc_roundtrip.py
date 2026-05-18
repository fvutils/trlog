"""Integration tests for VcDataBlock encode/decode roundtrip."""

import math
import pytest
from trlog._vc_data import VcDataBlock
from trlog._types import SignalEncoding, VcChange


def make_block(start, changes_by_var, sig_types, compress=False):
    """Helper: build and encode, then decode VcDataBlock."""
    blk = VcDataBlock(start_time=start, sig_types=sig_types, compress=compress)
    for var_id, changes in changes_by_var.items():
        for time, value in changes:
            blk.add_change(var_id, time, value)
    encoded = blk.encode_block()
    flags = encoded[1]
    blk2 = VcDataBlock(start_time=0, sig_types=sig_types, compress=False)
    return blk2.read_block(encoded[10:], flags=flags)


class TestOneBit2State:
    def test_clock_toggles(self):
        changes = [(i * 10, i % 2) for i in range(100)]
        sig_types = {1: (SignalEncoding.SE_2STATE, 1)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert actual == changes

    def test_1000_toggles(self):
        changes = [(i * 5, i % 2) for i in range(1000)]
        sig_types = {1: (SignalEncoding.SE_2STATE, 1)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert actual == changes


class TestNBit2State:
    def test_32bit_bus(self):
        changes = [(i * 10, i * 0x1000) for i in range(100)]
        sig_types = {1: (SignalEncoding.SE_2STATE, 32)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert actual == changes

    def test_8bit(self):
        changes = [(i, i % 256) for i in range(50)]
        sig_types = {1: (SignalEncoding.SE_2STATE, 8)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert actual == changes


class Test4State:
    def test_xz_values_1bit(self):
        """4-state 1-bit signal with X and Z transitions."""
        changes = [(0, 0), (10, 1), (20, 'X'), (30, 'Z'), (40, 1)]
        sig_types = {1: (SignalEncoding.SE_4STATE, 1)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        # X/Z should round-trip
        assert len(actual) == len(changes)
        assert actual[0] == (0, 0)
        assert actual[1] == (10, 1)
        assert actual[2][0] == 20 and actual[2][1] == 'X'
        assert actual[3][0] == 30 and actual[3][1] == 'Z'


class Test9State:
    def test_all_9_states(self):
        """9-state signal with all 9 states represented."""
        states = [0, 1, 'X', 'Z', 'H', 'U', 'W', 'L', '-']
        changes = [(i * 10, s) for i, s in enumerate(states)]
        sig_types = {1: (SignalEncoding.SE_9STATE, 1)}
        # 9-state 1-bit: each value is a single-bit state
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert len(actual) == len(states)
        for i, (t, v) in enumerate(actual):
            assert t == i * 10


class TestReal:
    def test_float_roundtrip(self):
        changes = [(i * 10, float(i) * 1.5) for i in range(20)]
        sig_types = {1: (SignalEncoding.SE_REAL, 0)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert len(actual) == len(changes)
        for (et, ev), (at, av) in zip(changes, actual):
            assert et == at
            assert abs(av - ev) < 1e-10

    def test_nan_inf(self):
        changes = [(0, float('nan')), (10, float('inf')), (20, float('-inf'))]
        sig_types = {1: (SignalEncoding.SE_REAL, 0)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [c.value for c in result if c.var_id == 1]
        assert math.isnan(actual[0])
        assert math.isinf(actual[1]) and actual[1] > 0
        assert math.isinf(actual[2]) and actual[2] < 0


class TestString:
    def test_string_roundtrip(self):
        changes = [(0, "hello"), (10, "world"), (20, "日本語")]
        sig_types = {1: (SignalEncoding.SE_STRING, 0)}
        result = make_block(0, {1: changes}, sig_types)
        actual = [(c.time, c.value) for c in result if c.var_id == 1]
        assert actual == changes


class TestDeduplication:
    def test_identical_patterns_share_wave_data(self):
        """Two vars with identical changes → encoded block < 2× single-var."""
        changes = [(i * 10, i % 2) for i in range(100)]
        sig_types = {
            1: (SignalEncoding.SE_2STATE, 1),
            2: (SignalEncoding.SE_2STATE, 1),
        }
        # Build block with both vars having identical patterns
        blk = VcDataBlock(start_time=0, sig_types=sig_types, compress=False)
        for t, v in changes:
            blk.add_change(1, t, v)
            blk.add_change(2, t, v)
        encoded_two = blk.encode_block()

        # Single-var block
        blk1 = VcDataBlock(start_time=0, sig_types={1: (SignalEncoding.SE_2STATE, 1)}, compress=False)
        for t, v in changes:
            blk1.add_change(1, t, v)
        encoded_one = blk1.encode_block()

        # Two-var encoded block should be notably smaller than 2× single-var
        assert len(encoded_two) < 2 * len(encoded_one)


class TestInitialValues:
    def test_initial_value_stored_even_without_changes(self):
        """Vars with initial values but no changes in this block."""
        sig_types = {
            1: (SignalEncoding.SE_2STATE, 1),
            2: (SignalEncoding.SE_2STATE, 8),
        }
        blk = VcDataBlock(start_time=100, sig_types=sig_types, compress=False)
        blk.set_initial(1, 1)
        blk.set_initial(2, 0xFF)
        blk.add_change(1, 110, 0)

        encoded = blk.encode_block()
        flags = encoded[1]
        blk2 = VcDataBlock(start_time=0, sig_types=sig_types)
        result = blk2.read_block(encoded[10:], flags=flags)

        times_by_var = {c.var_id: c.time for c in result}
        assert 1 in times_by_var
        assert 2 in times_by_var


class TestMultipleVcBlocks:
    def test_timeline_assembled(self):
        sig_types = {1: (SignalEncoding.SE_2STATE, 1)}

        def make(start, changes):
            blk = VcDataBlock(start_time=start, sig_types=sig_types, compress=False)
            for t, v in changes:
                blk.add_change(1, t, v)
            return blk.encode_block()

        b1 = make(0, [(0, 0), (10, 1)])
        b2 = make(20, [(20, 0), (30, 1)])

        def decode(enc):
            flags = enc[1]
            blk = VcDataBlock(start_time=0, sig_types=sig_types)
            return blk.read_block(enc[10:], flags=flags)

        all_changes = decode(b1) + decode(b2)
        times = [c.time for c in all_changes if c.var_id == 1]
        assert sorted(times) == [0, 10, 20, 30]
