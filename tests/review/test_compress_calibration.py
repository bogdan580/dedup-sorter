"""Compress calibration over HTTP and its frontend feedback."""
from __future__ import annotations

import json
import os
import threading
import time
import pytest
from tidy import config
from tidy.compression import hwdetect
from tidy.fs import platform_win
from tidy.compression import probe
from tidy.compression import encoder
from tidy.review import server as review_server
from tidy.store.cache import Cache

from .helpers import (
    _get,
    _post_json,
    _wait_calibration_terminal,
    _wait_compress_jobs_done,
    _wait_compress_ready,
)


class TestCompressCalibrateHTTP:
    """POST /api/compress/calibrate -- docs/technical/components/compression-mechanism.md. Uses the same
    real-ffmpeg, real-archive fixture as the apply-compress tests: this
    endpoint's whole point is a real encode + real XPSNR measurement of a
    short clip, written to the real cache DB."""

    def test_requires_the_mutating_request_gate(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "wrong-token"},
            payload={"file_id": "c1_1:0"},
        )
        assert status == 403

    def test_unknown_file_id_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        _wait_compress_ready(state.port)
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": "c1_99:0"},
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]

    def test_unknown_device_key_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": candidate["id"], "device_keys": ["does-not-exist"]},
        )
        assert status == 400
        assert "unknown device_key" in json.loads(body)["error"]

    def test_default_device_keys_are_every_uncalibrated_device(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": candidate["id"]},
        )
        assert status == 200
        result = json.loads(body)
        assert result["started"] is True
        # libx265 is always in the bundled build's -encoders list with no
        # calibration yet on this fresh test DB.
        assert "libx265" in result["device_keys"]
        _wait_calibration_terminal(state.port)

    def test_real_calibration_measures_libx265_and_writes_to_cache(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": candidate["id"], "device_keys": ["libx265"]},
        )
        assert status == 200
        assert json.loads(body)["started"] is True

        calib = _wait_calibration_terminal(state.port)
        assert calib["status"] == "done", calib["error"]
        assert "libx265" in calib["measured"]
        measured = calib["measured"]["libx265"]
        assert measured["bpp"] > 0
        assert measured["mpx_s"] > 0

        # Written to the real cache DB, not just held in memory.
        hw = hwdetect.detect(None)
        cache = Cache(state.db_path)
        try:
            db_calibration = cache.get_calibration(hw.machine_id)
        finally:
            cache.close()
        assert "libx265" in db_calibration
        assert db_calibration["libx265"]["bpp"] == pytest.approx(measured["bpp"])

        # Staging is scratch space for this measurement alone -- nothing
        # calibration touches survives in .compress after the job ends.
        staging_dir = state.compress_staging_dir
        leftover = [
            name for name in os.listdir(staging_dir) if name.startswith("calib-")
        ] if os.path.isdir(staging_dir) else []
        assert leftover == []

        # Never touches the archive: no !MOVES, no !REMOVE, original untouched.
        assert os.path.isfile(clip_path)
        assert not os.path.isdir(f"{archive_root}/!MOVES")
        assert not os.path.isdir(f"{archive_root}/!REMOVE")

    def test_completed_calibration_invalidates_cached_estimates(self, compress_queue_server):
        # _LazyComputation never recomputes once ready (CLAUDE.md) -- a
        # fresh measurement must drop state.compress_computations so the
        # NEXT GET /api/compress?source=... recomputes estimates with it,
        # exactly like POST /api/rescan's own invalidation.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        _wait_compress_ready(state.port)
        with state.compress_lock:
            assert "!NEW" in state.compress_computations

        _status, body, _headers = _get(state.port, "/api/compress?source=!NEW")
        candidate = json.loads(body.decode("utf-8"))["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": candidate["id"], "device_keys": ["libx265"]},
        )
        assert status == 200
        _wait_calibration_terminal(state.port)

        with state.compress_lock:
            assert "!NEW" not in state.compress_computations

    def test_calibrate_conflicts_with_a_running_compress_queue(
        self, compress_queue_server, monkeypatch,
    ):
        httpd, state, archive_root, clip_path, clip2_path = compress_queue_server
        started = threading.Event()
        release = threading.Event()
        real_run_encode = encoder.run_encode

        def blocking_run_encode(cmd, **kwargs):
            started.set()
            release.wait(timeout=15)
            return real_run_encode(cmd, **kwargs)

        monkeypatch.setattr(encoder, "run_encode", blocking_run_encode)

        payload = _wait_compress_ready(state.port)
        candidates = payload["candidates"]
        assert len(candidates) == 2

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [
                    {"group_id": candidates[0]["group_id"], "file_ids": [candidates[0]["id"]]},
                ],
                "profile": "x265",
            },
        )
        assert status == 200
        assert started.wait(timeout=10)

        status, body, _ = _post_json(
            state.port, "/api/compress/calibrate",
            headers={"X-Dedup-Token": "test-token"},
            payload={"file_id": candidates[1]["id"], "device_keys": ["libx265"]},
        )
        assert status == 409

        release.set()
        _wait_compress_jobs_done(state.port)


class TestCompressCalibrationFeedback:
    """Calibration from real work (docs/technical/components/compression-mechanism.md): a completed job's own real, measured bpp/mpx_s must feed
    back into cache.compress_calibration, and a LATER candidate scan on
    the same machine must read it back -- closing the gap where every
    estimate forever used config.SEED_CALIBRATION's single-sample,
    single-machine seed regardless of what actually happened on THIS
    machine (cache.put_calibration()/get_calibration() already existed but
    neither call site was ever wired to them)."""

    def test_completed_job_writes_real_calibration_to_the_cache_db(
        self, compress_queue_server,
    ):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        final = _wait_compress_jobs_done(state.port)
        assert final["jobs"][0]["status"] == "done"

        status, body, _ = _get(state.port, "/api/compress/encoders")
        machine_id = json.loads(body)["machine_id"]
        cache = Cache(state.db_path)
        try:
            calibration = cache.get_calibration(machine_id)
        finally:
            cache.close()

        assert "libx265" in calibration
        assert calibration["libx265"]["bpp"] > 0
        assert calibration["libx265"]["mpx_s"] > 0
        assert calibration["libx265"]["calibrated_at"]

    def test_encoders_endpoint_reflects_the_new_calibration(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, body, _ = _get(state.port, "/api/compress/encoders")
        before = {e["name"]: e["calibrated"] for e in json.loads(body)["encoders"]}
        assert before.get("libx265") is False

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        _wait_compress_jobs_done(state.port)

        status, body, _ = _get(state.port, "/api/compress/encoders")
        after = {e["name"]: e["calibrated"] for e in json.loads(body)["encoders"]}
        assert after["libx265"] is True

    def test_calibrated_estimates_appear_on_a_fresh_reviewstate_after_a_completed_job(
        self, compress_queue_server, tmp_path,
    ):
        # Same "read the shared DB fresh" shape as
        # TestCompressRemovableRestartSurvival, but this time proving the
        # OTHER, never-compressed clip's OWN estimate switches from the
        # config.py seed to this machine's real measurement -- calibration
        # is per-(machine, encoder), never per-file.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        final = _wait_compress_jobs_done(state.port)
        assert final["jobs"][0]["status"] == "done"

        fs2 = platform_win.WindowsFileSystem()
        httpd2, thread2, state2 = review_server.start_server(
            fs=fs2, archive_root=archive_root, duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs2"), host="127.0.0.1", port=0,
            token="test-token-2", db_path=state.db_path,
        )
        try:
            payload2 = _wait_compress_ready(state2.port)
            clip2_candidate = next(
                c for c in payload2["candidates"] if c["path"].endswith("clip2.avi")
            )
            x265_estimate = next(
                e for e in clip2_candidate["estimates"] if e["profile_name"] == "x265"
            )
            assert x265_estimate["calibrated"] is True
        finally:
            httpd2.shutdown()
            httpd2.server_close()
            thread2.join(timeout=5)

    def test_calibration_is_normalized_by_the_profile_bpp_multiplier(
        self, compress_queue_server, monkeypatch,
    ):
        # compress_calibration is keyed by ENCODER ("libx265"), not by
        # profile -- x265/x265_medium/x265_high all share it while
        # targeting different bpp (config.PROFILE_BPP_SEED_MULTIPLIER). A
        # measurement taken while running x265_high (1.56x) must be
        # divided back down to the CRF21-equivalent baseline before being
        # stored, or a LATER "x265" (1.0x) estimate would silently inherit
        # CRF18's bigger bpp and overstate the size of its own, smaller
        # CRF21 result.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server

        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        # Deterministic stand-ins for the real encode: 1 Mpx of total
        # "video" (1000 x 1000 x 1 fps x 1 s) compressed to exactly
        # 125,000 bytes (1,000,000 bits) -> raw measured bpp of 1.0 flat,
        # so the normalization arithmetic is exact, not approximate.
        fake_probe = probe.ProbeResult(
            codec="mjpeg", width=1000, height=1000, fps=1.0, bitrate=8_000_000,
            duration=1.0, has_audio=False, rotation=0,
        )
        monkeypatch.setattr(probe, "probe_file_cached", lambda *a, **kw: fake_probe)

        def fake_run_encode(cmd, **kwargs):
            # A real encode always takes measurable wall time; this fake
            # one is a single small file write, fast enough that
            # time.time()'s coarse resolution on Windows (~15 ms) could
            # read the SAME instant before and after, silently zeroing
            # out encode_seconds and skipping the calibration write with
            # no exception. The sleep makes elapsed time deterministic.
            time.sleep(0.05)
            dst = cmd[-1]
            with open(dst, "wb") as f:
                f.write(b"x" * 125_000)
            return encoder.EncodeResult(success=True, staging_path=dst)

        monkeypatch.setattr(encoder, "run_encode", fake_run_encode)
        monkeypatch.setattr(
            encoder, "verify_output",
            lambda *a, **kw: encoder.VerifyResult(ok=True, reason="ok"),
        )

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265_high", "remove_originals": False},
        )
        assert status == 200
        final = _wait_compress_jobs_done(state.port)
        assert final["jobs"][0]["status"] == "done"

        status, body, _ = _get(state.port, "/api/compress/encoders")
        machine_id = json.loads(body)["machine_id"]
        cache = Cache(state.db_path)
        try:
            calibration = cache.get_calibration(machine_id)
        finally:
            cache.close()

        expected_multiplier = config.PROFILE_BPP_SEED_MULTIPLIER["x265_high"]
        assert calibration["libx265"]["bpp"] == pytest.approx(1.0 / expected_multiplier, rel=1e-6)
