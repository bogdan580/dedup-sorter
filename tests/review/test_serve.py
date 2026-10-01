"""serve() shutdown behaviour."""
from __future__ import annotations

from unittest.mock import MagicMock
from tidy.review import server as review_server
from tidy.review import state as review_state
from fakes import FakeFileSystem


class TestServeStopsOnCtrlC:
    """Bug report: "jak odpalam run.cmd to Ctrl+C nie stopuje
    programu". serve() waited with a bare thread.join(), and on Windows a
    lock acquired with no timeout is uninterruptible (CPython's thread_nt.h
    says so in as many words) -- so the console's Ctrl+C set its flag and the
    main thread, parked in that acquire until serve_forever() returned (i.e.
    never), had no chance to turn it into a KeyboardInterrupt.

    Measured on this machine before the fix: an interrupt raised 1s in was
    only seen 20s later, when the joined thread happened to finish.

    The tests fake start_server() rather than opening a real socket: what is
    being pinned is the WAIT, not the server."""

    class _FakeHttpd:
        def __init__(self):
            self.calls = []

        def shutdown(self):
            self.calls.append("shutdown")

        def server_close(self):
            self.calls.append("server_close")

    class _FakeThread:
        """A server thread that, like the real one, never ends on its own."""

        def __init__(self, *, interrupt_after=None):
            self.joins = []
            self._interrupt_after = interrupt_after

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.joins.append(timeout)
            if timeout is None:
                # The bug itself: on Windows this call swallows Ctrl+C.
                raise AssertionError("serve() must never join without a timeout")
            if self._interrupt_after is not None and len(self.joins) >= self._interrupt_after:
                raise KeyboardInterrupt

    def _serve(self, monkeypatch, thread, httpd, printed):
        state = review_state.ReviewState(
            fs=FakeFileSystem(), archive_root="D:/Archive", duplicate_groups=[],
            decisions=[], thumbs=MagicMock(), thumb_sources={}, file_registry={},
            groups_json=[], total_reclaimable_bytes=0, token="t", host="127.0.0.1",
            port=8765,
        )
        monkeypatch.setattr(
            review_server, "start_server",
            lambda *args, **kwargs: (httpd, thread, state),
        )
        return review_server.serve(
            FakeFileSystem(), "D:/Archive", [], [], "D:/thumbs",
            open_browser=False, print_func=printed.append,
        )

    def test_ctrl_c_during_the_wait_stops_the_server(self, monkeypatch):
        httpd = self._FakeHttpd()
        thread = self._FakeThread(interrupt_after=2)
        printed = []

        exit_code = self._serve(monkeypatch, thread, httpd, printed)

        assert exit_code == 0
        assert httpd.calls == ["shutdown", "server_close"]

    def test_the_wait_wakes_up_often_enough_to_notice_an_interrupt(self, monkeypatch):
        # Half a second: long enough to cost nothing, short enough that Ctrl+C
        # feels immediate. A None here is the regression that shipped.
        httpd = self._FakeHttpd()
        thread = self._FakeThread(interrupt_after=3)
        printed = []

        self._serve(monkeypatch, thread, httpd, printed)

        assert thread.joins == [0.5, 0.5, 0.5]

    def test_user_is_told_the_interrupt_was_heard(self, monkeypatch):
        httpd = self._FakeHttpd()
        thread = self._FakeThread(interrupt_after=1)
        printed = []

        self._serve(monkeypatch, thread, httpd, printed)

        assert any("Stopping" in line for line in printed)

    def test_server_thread_dying_on_its_own_also_returns(self, monkeypatch):
        # Not the Ctrl+C path: if serve_forever() ever does stop (a crash in
        # the server thread), serve() must return instead of spinning on a
        # dead thread forever.
        class DyingThread:
            def __init__(self):
                self.alive = True
                self.joins = []

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                self.joins.append(timeout)
                self.alive = False

        httpd = self._FakeHttpd()
        thread = DyingThread()
        printed = []

        exit_code = self._serve(monkeypatch, thread, httpd, printed)

        assert exit_code == 0
        assert thread.joins == [0.5]
        assert httpd.calls == ["shutdown", "server_close"]
