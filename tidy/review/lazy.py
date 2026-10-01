"""Background computation slot polled by the UI."""
from __future__ import annotations

import threading
from typing import Callable, Optional


# See: docs/technical/architecture.md
class _LazyComputation:
    """One lazy "compute once, cache forever" resource (one instance for
    series, a separate one for events -- see ReviewState.series_computation/
    events_computation below).

    Uses its OWN threading.Lock, deliberately NOT the shared
    `ReviewState.lock` that POST /api/apply also takes: a real series/
    events computation can run for minutes, and sharing state.lock would
    mean an in-flight GET /api/series blocks every POST /api/apply for
    that entire time -- moving files (the one operation with real,
    irreversible-without---restore consequences) must never wait on a
    read-only background computation. This lock is only ever held for
    the brief state-transition checks below (start_if_idle's
    idle->computing flip, finish_ready's computing->ready flip, snapshot's
    read) -- never for the actual engine call, which runs lock-free in the
    background thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "idle"  # idle -> computing -> ready | error
        self._result: Optional[dict] = None
        self._error: Optional[str] = None

    def start_if_idle(self, worker: Callable[[], None]) -> bool:
        """Atomically claim the idle->computing transition; only the
        caller that actually flips it starts `worker` in a new daemon
        thread. Two concurrent HTTP requests calling this at (almost) the
        same instant must start exactly one computation -- putting the
        check ("is it idle?") and the flip ("mark it computing") inside
        one `with self._lock:` makes that check-and-set atomic, so only
        one of the two racing calls can ever see status == "idle"."""
        with self._lock:
            if self._status != "idle":
                return False
            self._status = "computing"
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return True

    def finish_ready(self, publish: Callable[[], dict]) -> None:
        """Called by the worker thread once the engine call has already
        succeeded and the view models are already built. `publish` is
        invoked INSIDE this lock and is expected to merge the new
        thumb_sources/open_registry entries into ReviewState and return
        the result dict to cache -- merging and flipping status to
        "ready" happen as ONE atomic step so a concurrent snapshot()
        (what GET /api/series's response is built from) can never
        observe status == "ready" before the ids it just promised are
        actually resolvable: GET /api/thumb/<id> and POST /api/open must
        never see a half-published result."""
        with self._lock:
            self._result = publish()
            self._status = "ready"

    def fail(self, message: str) -> None:
        with self._lock:
            self._status = "error"
            self._error = message

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self._status, "result": self._result, "error": self._error}
