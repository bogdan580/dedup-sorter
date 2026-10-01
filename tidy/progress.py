"""Thread-safe (done, total) progress counter shared by series.py's and
events.py's parallel candidate processing (--workers), polled by
tidy/review/computations.py's background computation threads to report "done/total"
to the review UI's progress bar.

A single mutable (done, total) pair, updated by whichever worker threads
happen to be doing the decoding, read by a completely unrelated thread (the
HTTP request handler) via snapshot(). No FileSystem/Cache object is
involved, so this carries none of the "sqlite3 connections can't cross
threads" restrictions cache.py's module docstring warns about.
"""
# Progress in the review UI: docs/technical/components/review-ui.md
from __future__ import annotations

import threading
from typing import Tuple

# Default worker count for series.py's/events.py's parallel per-candidate
# decode step (ThreadPoolExecutor) -- I/O + Pillow decode bound work, not
# CPU-bound Python, so a small multiple of typical core counts is a
# reasonable default without needing to detect the machine's core count.
DEFAULT_WORKERS = 4


class ProgressCounter:
    """Mutable, thread-safe (done, total) pair.

    `total` starts at 0 ("unknown yet") and grows via add_total() as the
    real scope of work becomes known -- callers must treat 0 as "not
    started", not "nothing to do". `done` only ever increases via
    increment(). A reader must only ever call snapshot(); mutating the pair
    from the reading side would race with the worker threads still writing
    to it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = 0
        self._total = 0

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = total

    def add_total(self, delta: int) -> None:
        with self._lock:
            self._total += delta

    def increment(self, delta: int = 1) -> None:
        with self._lock:
            self._done += delta

    def snapshot(self) -> Tuple[int, int]:
        with self._lock:
            return self._done, self._total

    def phase(self, declared: int = 0) -> "PhaseCounter":
        """A counter to hand to ONE engine call that is only a part of a
        larger job sharing a single progress bar with other calls.

        Every engine that takes a ProgressCounter opens with
        `set_total(len(its own candidates))` -- correct when it owns the
        whole job, destructive when it is one of several calls sharing one
        bar, since it would wipe the totals of the calls before it. A phase
        turns that set_total() into "add my share to the running total"
        instead. `declared` is that share already counted by the caller up
        front; an engine that then declares exactly that many items adds
        nothing more."""
        return PhaseCounter(self, declared=declared)


class PhaseCounter:
    """One engine call's view of a shared ProgressCounter -- see
    ProgressCounter.phase(). Same four methods, so it is accepted anywhere a
    ProgressCounter is; increment()/snapshot() go straight through to the
    parent, and only the meaning of set_total() differs.

    Its own lock guards only `_declared` (how much of the parent's total this
    phase has claimed so far); the parent's (done, total) stays guarded by
    the parent's lock. One lock per piece of state, never both held at once.
    """

    def __init__(self, parent: ProgressCounter, declared: int = 0) -> None:
        self._parent = parent
        self._lock = threading.Lock()
        self._declared = declared

    def set_total(self, total: int) -> None:
        with self._lock:
            delta = total - self._declared
            self._declared = total
        if delta:
            self._parent.add_total(delta)

    def add_total(self, delta: int) -> None:
        with self._lock:
            self._declared += delta
        self._parent.add_total(delta)

    def increment(self, delta: int = 1) -> None:
        self._parent.increment(delta)

    def snapshot(self) -> Tuple[int, int]:
        return self._parent.snapshot()
