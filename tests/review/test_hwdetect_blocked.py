"""Blocked ffmpeg/ffprobe tool handling in the review server."""
from __future__ import annotations

import json
from tidy.compression import hwdetect
from tidy.compression import probe
from tidy.compression import encoder
from tidy.review import computations as review_computations
from tidy.review import state as review_state
from tidy.review.workers import calibration as review_calibration
from tidy.review.workers import compress_queue as review_compress_queue

from .helpers import (
    _BLOCK_MESSAGE,
    _bare_state,
    _fake_hw_result,
    _get,
    _raise_blocked,
)


class TestHwdetectBlocked:
    def test_blocked_detect_returns_none_records_reason_and_is_not_memoised(
        self, tmp_path, monkeypatch
    ):
        state = _bare_state(tmp_path)
        calls = []

        def _detect(_path):
            calls.append(1)
            if len(calls) == 1:
                raise probe.ToolBlockedError(_BLOCK_MESSAGE)
            return _fake_hw_result()

        monkeypatch.setattr(hwdetect, "detect", _detect)

        assert review_computations._hwdetect_for(state) is None
        assert state.hwdetect_block_reason == _BLOCK_MESSAGE
        assert state.hwdetect_cache is None

        # Recovers on the next call, without a restart, and clears the reason.
        assert review_computations._hwdetect_for(state) is not None
        assert state.hwdetect_block_reason is None
        assert len(calls) == 2

    def test_note_tool_blocked_drops_cache_and_records_reason(self, tmp_path):
        state = _bare_state(tmp_path)
        state.hwdetect_cache = _fake_hw_result()

        reason = review_computations._note_tool_blocked(state, probe.ToolBlockedError(_BLOCK_MESSAGE))

        assert reason == _BLOCK_MESSAGE
        assert state.hwdetect_cache is None
        assert state.hwdetect_block_reason == _BLOCK_MESSAGE

    def test_encoders_endpoint_reports_unavailable_reason(self, compress_server, monkeypatch):
        httpd, state, fs = compress_server
        monkeypatch.setattr(hwdetect, "detect", _raise_blocked)
        status, body, _ = _get(state.port, "/api/compress/encoders")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        assert payload["available"] is False
        assert payload["unavailable_reason"] == _BLOCK_MESSAGE

    def test_encoders_endpoint_reason_is_null_when_available(self, compress_server):
        httpd, state, fs = compress_server
        status, body, _ = _get(state.port, "/api/compress/encoders")
        assert json.loads(body.decode("utf-8"))["unavailable_reason"] is None

    def test_scan_publishes_unavailable_result_with_reason(self, tmp_path, monkeypatch):
        state = _bare_state(tmp_path)
        monkeypatch.setattr(hwdetect, "detect", _raise_blocked)

        review_compress_queue._run_compress_computation(state, "!NEW")

        computation, _progress = review_computations._compress_slots(state, "!NEW")
        snapshot = computation.snapshot()
        assert snapshot["status"] == "ready"
        assert snapshot["result"]["encoders_available"] is False
        assert snapshot["result"]["unavailable_reason"] == _BLOCK_MESSAGE

    def test_scan_block_during_probe_clears_cache_and_publishes_reason(
        self, tmp_path, monkeypatch
    ):
        state = _bare_state(tmp_path)
        state.hwdetect_cache = _fake_hw_result()
        root = "D:/Archive"
        state.fs.add_dir(root)
        state.fs.add_dir(f"{root}/!NEW")
        state.fs.add_file(
            f"{root}/!NEW/a.mp4", b"x" * 10, size=10, mtime_ns=1,
            volume_id=1, file_index=1,
        )
        monkeypatch.setattr(probe, "probe_file_cached", _raise_blocked)

        review_compress_queue._run_compress_computation(state, "!NEW")

        computation, _progress = review_computations._compress_slots(state, "!NEW")
        snapshot = computation.snapshot()
        assert snapshot["status"] == "ready"
        assert snapshot["result"]["unavailable_reason"] == _BLOCK_MESSAGE
        assert state.hwdetect_cache is None
        assert state.hwdetect_block_reason == _BLOCK_MESSAGE


class TestCompressQueueBlocked:
    def _jobs(self, n=2, state=None):
        if state is not None:
            state.fs.add_dir("D:/Archive")
            for i in range(n):
                state.fs.add_file(
                    f"D:/Archive/{i}.mp4", b"x" * 10, size=10, mtime_ns=1,
                    volume_id=1, file_index=i + 1,
                )
        return [
            review_state.CompressJob(
                file_id=f"c1:{i}", group_id=f"c1_{i}", path=f"D:/Archive/{i}.mp4",
                size=10, profile_name="x265",
            )
            for i in range(n)
        ]

    def test_block_mid_queue_fails_pending_jobs_with_readable_error(
        self, tmp_path, monkeypatch
    ):
        state = _bare_state(tmp_path)
        state.hwdetect_cache = _fake_hw_result()
        state.compress_queue_running = True
        monkeypatch.setattr(probe, "probe_file_cached", _raise_blocked)
        jobs = self._jobs(state=state)

        review_compress_queue._run_compress_queue(state, jobs, "x265", False)

        assert [j.status for j in jobs] == ["failed", "failed"]
        assert all(j.error == _BLOCK_MESSAGE for j in jobs)
        assert state.hwdetect_cache is None
        assert state.hwdetect_block_reason == _BLOCK_MESSAGE
        assert state.compress_queue_running is False

    def test_block_at_detect_fails_all_jobs_with_the_reason(self, tmp_path, monkeypatch):
        state = _bare_state(tmp_path)
        state.compress_queue_running = True
        monkeypatch.setattr(hwdetect, "detect", _raise_blocked)
        jobs = self._jobs()

        review_compress_queue._run_compress_queue(state, jobs, "x265", False)

        assert all(j.status == "failed" and j.error == _BLOCK_MESSAGE for j in jobs)

    def test_block_in_run_encode_is_readable_on_the_job(self, tmp_path, monkeypatch):
        state = _bare_state(tmp_path)
        state.hwdetect_cache = _fake_hw_result()
        state.compress_queue_running = True
        monkeypatch.setattr(
            probe, "probe_file_cached",
            lambda *_a, **_kw: probe.ProbeResult(
                codec="hevc", width=1920, height=1080, fps=30.0, bitrate=50_000_000,
                duration=10.0, has_audio=False, rotation=0,
            ),
        )
        monkeypatch.setattr(encoder, "run_encode", _raise_blocked)
        jobs = self._jobs(state=state)

        review_compress_queue._run_compress_queue(state, jobs, "x265", False)

        assert all(j.status == "failed" and j.error == _BLOCK_MESSAGE for j in jobs)
        assert state.hwdetect_cache is None

    def test_calibration_block_sets_readable_error(self, tmp_path, monkeypatch):
        state = _bare_state(tmp_path)
        state.hwdetect_cache = _fake_hw_result()
        monkeypatch.setattr(probe, "probe_file_cached", _raise_blocked)
        self._jobs(1, state=state)
        job = review_state.CalibrationJob(
            file_id="c1:0", path="D:/Archive/0.mp4", size=10, device_keys=["libx265"],
        )

        review_calibration._run_calibration_job(state, job)

        assert job.status == "failed"
        assert job.error == _BLOCK_MESSAGE
        assert state.hwdetect_cache is None
