"""Tests for hashing.py: BLAKE2b partial (first+last 64KB) and full-file
hashers, operating over an injected BinaryIO reader (io.BytesIO in tests --
no real files touched).
"""
import hashlib
import io

import pytest

from tidy import config
from tidy.duplicates import hashing


def blake2b_hex(data: bytes) -> str:
    return hashlib.blake2b(data).hexdigest()


class TestFullHash:
    def test_empty_stream(self):
        assert hashing.full_hash(io.BytesIO(b"")) == blake2b_hex(b"")

    def test_small_content(self):
        data = b"hello world"
        assert hashing.full_hash(io.BytesIO(data)) == blake2b_hex(data)

    def test_large_content_read_in_chunks(self):
        data = b"x" * (5 * 1024 * 1024 + 37)  # not a round chunk multiple
        assert hashing.full_hash(io.BytesIO(data)) == blake2b_hex(data)

    def test_seeks_to_start_before_hashing(self):
        data = b"0123456789"
        stream = io.BytesIO(data)
        stream.seek(5)  # simulate a stream left mid-way by a prior read
        assert hashing.full_hash(stream) == blake2b_hex(data)


class TestPartialHash:
    def test_small_file_hashes_whole_content_once(self):
        # size <= 2*window: partial hash must equal hashing the whole file
        # exactly once (no duplicated/overlapping bytes fed to the hasher),
        # so tier-3 can reuse this value verbatim.
        data = b"y" * 100  # well under 128KB
        result = hashing.partial_hash(io.BytesIO(data), size=len(data),
                                       window_bytes=config.TIER2_WINDOW_BYTES)
        assert result == blake2b_hex(data)

    def test_exactly_at_threshold_hashes_whole_content_once(self):
        window = 16  # small window for a fast test
        data = b"z" * (2 * window)
        result = hashing.partial_hash(io.BytesIO(data), size=len(data),
                                       window_bytes=window)
        assert result == blake2b_hex(data)

    def test_large_file_hashes_first_and_last_window_only(self):
        window = 16
        head = b"H" * window
        middle = b"M" * 1000
        tail = b"T" * window
        data = head + middle + tail
        result = hashing.partial_hash(io.BytesIO(data), size=len(data),
                                       window_bytes=window)
        expected = blake2b_hex(head + tail)
        assert result == expected

    def test_different_middle_same_head_tail_same_partial_hash(self):
        # Tier-2 property under test: two large files with identical
        # first/last window but different middles hash identically at tier 2.
        window = 16
        head = b"H" * window
        tail = b"T" * window
        data_a = head + b"AAAA" * 100 + tail
        data_b = head + b"BBBB" * 50 + tail
        ha = hashing.partial_hash(io.BytesIO(data_a), size=len(data_a), window_bytes=window)
        hb = hashing.partial_hash(io.BytesIO(data_b), size=len(data_b), window_bytes=window)
        assert ha == hb

    def test_different_head_gives_different_partial_hash(self):
        window = 16
        tail = b"T" * window
        data_a = b"H" * window + b"middle" + tail
        data_b = b"X" * window + b"middle" + tail
        ha = hashing.partial_hash(io.BytesIO(data_a), size=len(data_a), window_bytes=window)
        hb = hashing.partial_hash(io.BytesIO(data_b), size=len(data_b), window_bytes=window)
        assert ha != hb


class TestReusePolicy:
    def test_small_file_reuses_partial_as_full(self):
        assert hashing.can_reuse_partial_as_full(
            size=config.SMALL_FILE_REUSE_THRESHOLD, window_bytes=config.TIER2_WINDOW_BYTES
        )
        assert hashing.can_reuse_partial_as_full(
            size=1, window_bytes=config.TIER2_WINDOW_BYTES
        )

    def test_large_file_cannot_reuse(self):
        assert not hashing.can_reuse_partial_as_full(
            size=config.SMALL_FILE_REUSE_THRESHOLD + 1,
            window_bytes=config.TIER2_WINDOW_BYTES,
        )
