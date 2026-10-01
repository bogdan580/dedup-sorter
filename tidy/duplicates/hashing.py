"""BLAKE2b partial and full-file hashers.

Every function here operates on an already-open `BinaryIO` reader (as
returned by `FileSystem.open_read`) -- never on a path -- so tests can pass
in `io.BytesIO` fakes without touching any real file.
"""
from __future__ import annotations

import hashlib
from typing import BinaryIO

from tidy import config

_CHUNK_SIZE = 1024 * 1024  # 1 MB read chunks for full_hash


def _new_hasher(algo: str = config.DEFAULT_HASH_ALGO):
    if algo != config.BLAKE2B:
        raise ValueError(f"Unsupported hash algorithm: {algo!r}")
    return hashlib.blake2b()


def can_reuse_partial_as_full(size: int, window_bytes: int) -> bool:
    """True when the tier-2 (partial) hash already covers every byte of the
    file exactly once, so tier-3 can reuse it verbatim instead of reading
    the file a second time."""
    return size <= window_bytes * 2


def full_hash(stream: BinaryIO, algo: str = config.DEFAULT_HASH_ALGO) -> str:
    """Hash the entire content of `stream`, reading in fixed-size chunks so
    multi-gigabyte files don't need to be loaded into memory at once."""
    stream.seek(0)
    hasher = _new_hasher(algo)
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def partial_hash(
    stream: BinaryIO,
    size: int,
    window_bytes: int = config.TIER2_WINDOW_BYTES,
    algo: str = config.DEFAULT_PARTIAL_ALGO,
) -> str:
    """Tier-2 hash: BLAKE2b over the first `window_bytes` and last
    `window_bytes` of the file.

    When `size <= 2 * window_bytes` the two windows would overlap, so this
    reads and hashes the whole file exactly once instead -- making the
    result identical to what a full-file hash would produce, which is what
    lets tier-3 reuse this value for small files without a second read
    (see `can_reuse_partial_as_full`).
    """
    hasher = _new_hasher(algo)
    stream.seek(0)
    if can_reuse_partial_as_full(size, window_bytes):
        hasher.update(stream.read())
        return hasher.hexdigest()

    hasher.update(stream.read(window_bytes))
    stream.seek(size - window_bytes)
    hasher.update(stream.read(window_bytes))
    return hasher.hexdigest()
