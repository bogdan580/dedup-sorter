"""Compress queue over HTTP: apply validation, lifecycle, concurrency, skips."""
from __future__ import annotations

import json
import os
import shutil
import threading
from unittest.mock import MagicMock
from tidy.compression import compress
from tidy import config
from tidy.compression import hwdetect
from tidy.moves import manifest
from tidy.fs import platform_win
from tidy.compression import encoder
from tidy.progress import ProgressCounter
from tidy.review import lazy as review_lazy
from tidy.review import server as review_server
from tidy.review import skips as review_skips
from tidy.review import state as review_state
from tidy.review.workers import compress_queue as review_compress_queue
from tidy.store.cache import Cache
from fakes import FakeFileSystem

from .helpers import (
    _get,
    _post_json,
    _wait_compress_jobs_done,
    _wait_compress_ready,
)


class TestApplyCompressValidationHTTP:
    """Everything that is rejected before a queue is ever spawned -- no
    real encode happens in any test in this class."""

    def test_unknown_profile_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "not-a-real-profile",
            },
        )
        assert status == 400
        assert "unknown profile" in json.loads(body)["error"]

    def test_unknown_file_id_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        _wait_compress_ready(state.port)
        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": "c1_99", "file_ids": ["c1_99:0"]}],
                "profile": "x265",
            },
        )
        assert status == 400
        assert "unknown id" in json.loads(body)["error"]

    def test_encoder_unavailable_on_this_machine_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]

        # _hwdetect_for() memoises on state.hwdetect_cache -- overwriting
        # it directly (rather than monkeypatching hwdetect.detect, which
        # the GET /api/compress call above has already made moot) is the
        # reliable way to make "x265" look unavailable on this machine.
        hw = hwdetect.detect(None)
        stripped = hwdetect.HwDetectResult(
            ffmpeg_exe=hw.ffmpeg_exe, ffprobe_exe=hw.ffprobe_exe,
            ffmpeg_version=hw.ffmpeg_version, machine_id=hw.machine_id,
            encoders=tuple(e for e in hw.encoders if e.name != "libx265"),
        )
        state.hwdetect_cache = stripped

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "x265",
            },
        )
        assert status == 400
        assert "not available" in json.loads(body)["error"]

    def test_already_applied_group_is_refused_with_409(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        state.applied_compress_group_ids.add(candidate["group_id"])

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "x265",
            },
        )
        assert status == 409
        assert "already applied" in json.loads(body)["error"]

    def test_requires_the_mutating_request_gate(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "wrong-token"},
            payload={"selections": [], "profile": "x265"},
        )
        assert status == 403

    def test_device_key_for_a_cpu_profile_is_rejected(self, compress_queue_server):
        # docs/technical/components/compression-mechanism.md: a device_key is only ever meaningful for a
        # hardware profile -- passing one with a CPU profile is rejected,
        # even if it happens to spell that CPU encoder's own bare name.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "x265", "device_key": "libx265",
            },
        )
        assert status == 400
        assert "CPU" in json.loads(body)["error"]
        with state.compress_queue_lock:
            assert state.compress_queue_running is False

    def test_unknown_device_key_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]

        hw = hwdetect.detect(None)
        fake_hw_encoder = hwdetect.EncoderInfo(
            name="hevc_qsv", display_name="HEVC / Quick Sync", device="",
            is_hardware=True, device_key="hevc_qsv@Fake Device",
        )
        state.hwdetect_cache = hwdetect.HwDetectResult(
            ffmpeg_exe=hw.ffmpeg_exe, ffprobe_exe=hw.ffprobe_exe,
            ffmpeg_version=hw.ffmpeg_version, machine_id=hw.machine_id,
            encoders=(fake_hw_encoder,),
        )

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "hevc_qsv", "device_key": "hevc_qsv@does-not-exist",
            },
        )
        assert status == 400
        assert "unknown or mismatched" in json.loads(body)["error"]
        with state.compress_queue_lock:
            assert state.compress_queue_running is False

    def test_device_key_belonging_to_a_different_encoder_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]

        hw = hwdetect.detect(None)
        fake_encoders = (
            hwdetect.EncoderInfo(
                name="hevc_qsv", display_name="HEVC / Quick Sync", device="",
                is_hardware=True, device_key="hevc_qsv@Intel(R) Iris(R) Xe Graphics",
            ),
            hwdetect.EncoderInfo(
                name="hevc_nvenc", display_name="HEVC / NVENC", device="0",
                is_hardware=True, device_key="hevc_nvenc@NVIDIA GeForce RTX 4060",
            ),
        )
        state.hwdetect_cache = hwdetect.HwDetectResult(
            ffmpeg_exe=hw.ffmpeg_exe, ffprobe_exe=hw.ffprobe_exe,
            ffmpeg_version=hw.ffmpeg_version, machine_id=hw.machine_id,
            encoders=fake_encoders,
        )

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "hevc_qsv",
                "device_key": "hevc_nvenc@NVIDIA GeForce RTX 4060",
            },
        )
        assert status == 400
        assert "unknown or mismatched" in json.loads(body)["error"]
        with state.compress_queue_lock:
            assert state.compress_queue_running is False

    def test_device_key_must_be_a_string(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}],
                "profile": "x265", "device_key": 123,
            },
        )
        assert status == 400
        assert "device_key" in json.loads(body)["error"]


class TestApplyCompressRemoveValidationHTTP:
    def test_requires_the_mutating_request_gate(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/apply-compress-remove",
            headers={"X-Dedup-Token": "wrong-token"},
            payload={"selections": []},
        )
        assert status == 403

    def test_unresolvable_selection_is_rejected(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/apply-compress-remove",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [{"group_id": "c9_9", "file_ids": ["c9_9:0"]}]},
        )
        assert status == 400


class TestCompressCancelHTTP:
    def test_noop_when_nothing_is_running(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/compress/cancel",
            headers={"X-Dedup-Token": "test-token"}, payload={},
        )
        assert status == 200
        assert json.loads(body)["cancelled"] is False

    def test_requires_the_mutating_request_gate(self, compress_queue_server):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        status, body, _ = _post_json(
            state.port, "/api/compress/cancel",
            headers={"X-Dedup-Token": "wrong-token"}, payload={},
        )
        assert status == 403


class TestCompressQueueLifecycleHTTP:
    """Real encode+verify+apply, one candidate at a time -- the queue's
    happy path plus the contract tests listed in docs/technical/development/conventions-and-tests.md."""

    def test_queue_runs_queued_through_done_and_registers_a_compress_batch(
        self, compress_queue_server,
    ):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        assert json.loads(body)["queued"] == 1

        final = _wait_compress_jobs_done(state.port)
        assert len(final["jobs"]) == 1
        job = final["jobs"][0]
        assert job["status"] == "done", job
        assert job["result_size"] > 0
        assert job["original_size"] > 0

        # remove_originals was False -- the original is untouched.
        assert os.path.exists(clip_path)

        compress_batches = [b for b in state.applied_batches if b.get("kind") == "compress"]
        assert len(compress_batches) == 1
        assert compress_batches[0]["moved"] == 1

        # The results panel now offers this original for removal.
        assert any(r["path"].endswith("clip.avi") for r in final["removable"])

    def test_encode_failure_leaves_original_untouched_and_creates_no_remove_batch(
        self, compress_queue_server, monkeypatch,
    ):
        # Contract test #1 (docs/technical/development/conventions-and-tests.md): the one scenario of real
        # data loss this whole feature exists to prevent.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        monkeypatch.setattr(
            encoder, "run_encode",
            lambda *a, **kw: encoder.EncodeResult(
                success=False, staging_path="", reason="forced failure",
            ),
        )
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": True},
        )
        assert status == 200

        final = _wait_compress_jobs_done(state.port)
        job = final["jobs"][0]
        assert job["status"] == "failed"
        assert job["error"] == "forced failure"

        assert os.path.exists(clip_path)
        remove_batches = [b for b in state.applied_batches if b.get("kind") == "remove"]
        assert all(b["moved"] == 0 for b in remove_batches)
        for b in remove_batches:
            assert candidate["group_id"] not in b["group_ids"]

    def test_verify_failure_leaves_original_untouched_and_creates_no_remove_batch(
        self, compress_queue_server, monkeypatch,
    ):
        # Contract test #2: same assertions, different failing step.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        monkeypatch.setattr(
            encoder, "verify_output",
            lambda *a, **kw: encoder.VerifyResult(
                ok=False, reason="forced verify failure",
            ),
        )
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": True},
        )
        assert status == 200

        final = _wait_compress_jobs_done(state.port)
        job = final["jobs"][0]
        assert job["status"] == "failed"
        assert job["error"] == "forced verify failure"

        assert os.path.exists(clip_path)
        remove_batches = [b for b in state.applied_batches if b.get("kind") == "remove"]
        assert all(b["moved"] == 0 for b in remove_batches)

    def test_result_not_smaller_than_original_refuses_removal(
        self, compress_queue_server, monkeypatch,
    ):
        # Contract test #3: encode+verify BOTH succeed, but the "result"
        # is a byte-for-byte copy of the source (never smaller) --
        # apply_compress_removals()'s own safety net (already covered at
        # the applier layer by tests/test_applier_compress.py) must still
        # refuse the removal when reached through this review-server
        # wiring, not just when called directly.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server

        def fake_run_encode(cmd, **kwargs):
            dst = cmd[-1]
            shutil.copyfile(clip_path, dst)
            return encoder.EncodeResult(success=True, staging_path=dst)

        monkeypatch.setattr(encoder, "run_encode", fake_run_encode)
        monkeypatch.setattr(
            encoder, "verify_output",
            lambda *a, **kw: encoder.VerifyResult(ok=True, reason="ok"),
        )

        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": True},
        )
        assert status == 200

        final = _wait_compress_jobs_done(state.port)
        job = final["jobs"][0]
        # The compress side succeeded -- only the SEPARATE removal
        # decision is refused.
        assert job["status"] == "done"
        assert os.path.exists(clip_path)

        remove_batches = [b for b in state.applied_batches if b.get("kind") == "remove"]
        assert remove_batches, "expected a remove batch to have been attempted"
        assert remove_batches[-1]["moved"] == 0
        assert remove_batches[-1]["failed"] == 1

    def test_multi_file_queue_lands_in_one_moves_and_one_remove_batch(
        self, compress_queue_server,
    ):
        httpd, state, archive_root, clip_path, clip_path2 = compress_queue_server
        payload = _wait_compress_ready(state.port)
        candidates = payload["candidates"]
        assert len(candidates) == 2
        selections = [{"group_id": c["group_id"], "file_ids": [c["id"]]} for c in candidates]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": True},
        )
        assert status == 200
        final = _wait_compress_jobs_done(state.port)
        assert [j["status"] for j in final["jobs"]] == ["done", "done"]
        assert not os.path.exists(clip_path)
        assert not os.path.exists(clip_path2)

        for kind, container in (("compress", config.MOVES_DIR_NAME), ("remove", config.REMOVE_DIR_NAME)):
            batches = [b for b in state.applied_batches if b.get("kind") == kind]
            assert len(batches) == 1, kind
            assert batches[0]["moved"] == 2
            batch_dirs = [
                d for d in os.listdir(os.path.join(archive_root, container))
                if os.path.isdir(os.path.join(archive_root, container, d))
            ]
            assert len(batch_dirs) == 1, (container, batch_dirs)

    def test_apply_compress_remove_moves_the_original_to_remove(
        self, compress_queue_server,
    ):
        # Remove-originals followed immediately by the results panel is out of
        # scope for one test -- this covers the results panel
        # (POST /api/apply-compress-remove) directly against a real, already-landed compress result.
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
        removable = final["removable"]
        assert len(removable) == 1

        status, body, _ = _post_json(
            state.port, "/api/apply-compress-remove",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": [
                {"group_id": removable[0]["group_id"], "file_ids": [removable[0]["file_id"]]},
            ]},
        )
        assert status == 200
        out = json.loads(body)
        assert out["moved"] == 1
        assert not os.path.exists(clip_path)

    def test_allow_lower_quality_uses_relaxed_xpsnr_threshold(
        self, compress_queue_server, monkeypatch,
    ):
        # The "Quality" panel's opt-in checkbox (docs/business/rules/compression.md) --
        # verify_output() must be called with
        # cfg.min_xpsnr_relaxed, not cfg.min_xpsnr, when the request sets
        # allow_lower_quality: true.
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        seen_min_xpsnr = []
        real_verify = encoder.verify_output

        def _spy_verify_output(*args, **kwargs):
            seen_min_xpsnr.append(kwargs.get("min_xpsnr"))
            return real_verify(*args, **kwargs)

        monkeypatch.setattr(encoder, "verify_output", _spy_verify_output)

        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": selections, "profile": "x265", "remove_originals": False,
                "allow_lower_quality": True,
            },
        )
        assert status == 200
        _wait_compress_jobs_done(state.port)

        cfg = compress.load_compress_config(state.compress_config_path)
        assert seen_min_xpsnr == [cfg.min_xpsnr_relaxed]
        assert cfg.min_xpsnr_relaxed < cfg.min_xpsnr

    def test_allow_lower_quality_defaults_to_false(
        self, compress_queue_server, monkeypatch,
    ):
        httpd, state, archive_root, clip_path, _clip2 = compress_queue_server
        seen_min_xpsnr = []
        real_verify = encoder.verify_output

        def _spy_verify_output(*args, **kwargs):
            seen_min_xpsnr.append(kwargs.get("min_xpsnr"))
            return real_verify(*args, **kwargs)

        monkeypatch.setattr(encoder, "verify_output", _spy_verify_output)

        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        selections = [{"group_id": candidate["group_id"], "file_ids": [candidate["id"]]}]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        _wait_compress_jobs_done(state.port)

        cfg = compress.load_compress_config(state.compress_config_path)
        assert seen_min_xpsnr == [cfg.min_xpsnr]


class TestApplyCompressConcurrency:
    def test_second_apply_is_refused_while_the_first_queue_is_running(
        self, compress_queue_server, monkeypatch,
    ):
        httpd, state, archive_root, clip_path, clip_path2 = compress_queue_server
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

        status1, _b1, _h1 = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [
                    {"group_id": candidates[0]["group_id"], "file_ids": [candidates[0]["id"]]},
                ],
                "profile": "x265", "remove_originals": False,
            },
        )
        assert status1 == 200
        assert started.wait(timeout=10)

        status2, body2, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={
                "selections": [
                    {"group_id": candidates[1]["group_id"], "file_ids": [candidates[1]["id"]]},
                ],
                "profile": "x265", "remove_originals": False,
            },
        )
        assert status2 == 409
        assert "already running" in json.loads(body2)["error"]

        release.set()
        _wait_compress_jobs_done(state.port)

    def test_cancel_between_jobs_marks_remaining_queued_as_cancelled(
        self, compress_queue_server, monkeypatch,
    ):
        httpd, state, archive_root, clip_path, clip_path2 = compress_queue_server
        started_first = threading.Event()
        release_first = threading.Event()
        real_run_encode = encoder.run_encode
        call_count = {"n": 0}

        def controlled_run_encode(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                started_first.set()
                release_first.wait(timeout=15)
            return real_run_encode(cmd, **kwargs)

        monkeypatch.setattr(encoder, "run_encode", controlled_run_encode)

        payload = _wait_compress_ready(state.port)
        candidates = payload["candidates"]
        assert len(candidates) == 2
        selections = [
            {"group_id": c["group_id"], "file_ids": [c["id"]]} for c in candidates
        ]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        assert started_first.wait(timeout=10)

        # Cancel while the first job is still blocked "encoding".
        status, body, _ = _post_json(
            state.port, "/api/compress/cancel",
            headers={"X-Dedup-Token": "test-token"}, payload={},
        )
        assert status == 200
        assert json.loads(body)["cancelled"] is True

        release_first.set()
        final = _wait_compress_jobs_done(state.port)
        by_group = {j["group_id"]: j["status"] for j in final["jobs"]}
        # First job: cancel_event was set only AFTER it started encoding,
        # and this test's mock doesn't itself observe cancel_event (that
        # is encoder.run_encode()'s own contract, already covered by
        # tests/test_encoder.py) -- it completes normally.
        assert by_group[candidates[0]["group_id"]] == "done"
        # Second job: the queue loop checks cancel_event BEFORE starting
        # it and never does -- marked "cancelled", the actual point of
        # this test.
        assert by_group[candidates[1]["group_id"]] == "cancelled"

    def test_first_job_lands_in_archive_without_waiting_for_the_rest_of_the_queue(
        self, compress_queue_server, monkeypatch,
    ):
        # Regression: an earlier design collected every job's CompressPlan
        # and moved them all into the archive in ONE apply_compressions()
        # call made only after the whole queue finished encoding -- so the
        # first file in a multi-file queue stayed invisible in its own
        # folder for as long as the rest of the queue was still running,
        # even though its own encode+verify had long since finished. Each
        # job must now be moved into the archive (its own
        # apply_compressions() call, appended to the queue's shared
        # "compress" batch) as soon as ITS OWN verify passes, never waiting
        # on any other job.
        httpd, state, archive_root, clip_path, clip_path2 = compress_queue_server
        started_second = threading.Event()
        release_second = threading.Event()
        real_run_encode = encoder.run_encode
        call_count = {"n": 0}

        def controlled_run_encode(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                started_second.set()
                release_second.wait(timeout=15)
            return real_run_encode(cmd, **kwargs)

        monkeypatch.setattr(encoder, "run_encode", controlled_run_encode)

        payload = _wait_compress_ready(state.port)
        candidates = payload["candidates"]
        assert len(candidates) == 2
        selections = [
            {"group_id": c["group_id"], "file_ids": [c["id"]]} for c in candidates
        ]

        status, _body, _ = _post_json(
            state.port, "/api/apply-compress",
            headers={"X-Dedup-Token": "test-token"},
            payload={"selections": selections, "profile": "x265", "remove_originals": False},
        )
        assert status == 200
        assert started_second.wait(timeout=10)

        # The second job is still blocked mid-"encoding" -- but the first
        # job's own encode+verify+move already ran to completion
        # (sequentially, before the second job's run_encode call was even
        # made): "done", its own compressed file already present on disk,
        # and its own "compress" batch already registered.
        status, body, _ = _get(state.port, "/api/compress/jobs")
        assert status == 200
        mid_payload = json.loads(body.decode("utf-8"))
        by_group = {j["group_id"]: j for j in mid_payload["jobs"]}
        first_job = by_group[candidates[0]["group_id"]]
        assert first_job["status"] == "done"
        assert by_group[candidates[1]["group_id"]]["status"] == "encoding"

        first_compress_batches = [
            b for b in state.applied_batches
            if b.get("kind") == "compress" and candidates[0]["group_id"] in b.get("group_ids", [])
        ]
        assert len(first_compress_batches) == 1
        compressed_dst = compress.compressed_dst_path(clip_path, compress.load_compress_config(None).suffix)
        assert os.path.exists(compressed_dst)
        assert not any(
            b.get("kind") == "compress" and candidates[1]["group_id"] in b.get("group_ids", [])
            for b in state.applied_batches
        )

        release_second.set()
        final = _wait_compress_jobs_done(state.port)
        by_group_final = {j["group_id"]: j["status"] for j in final["jobs"]}
        assert by_group_final[candidates[0]["group_id"]] == "done"
        assert by_group_final[candidates[1]["group_id"]] == "done"
        # One queue, one shared !MOVES batch -- both jobs appended to it.
        compress_batches = [b for b in state.applied_batches if b.get("kind") == "compress"]
        assert len(compress_batches) == 1
        assert compress_batches[0]["moved"] == 2
        assert set(compress_batches[0]["group_ids"]) == {c["group_id"] for c in candidates}


class TestCompressRemovableRestartSurvival:
    def test_removable_candidate_survives_a_brand_new_reviewstate(
        self, compress_queue_server, tmp_path,
    ):
        # The results panel is rebuilt from manifests on disk
        # (docs/technical/components/review-ui.md) -- a brand-new ReviewState,
        # with applied_batches empty, must still see the removable
        # candidate purely by reading the !MOVES manifest back off disk.
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
            token="test-token-2",
        )
        try:
            assert state2.applied_batches == []
            status2, body2, _ = _get(state2.port, "/api/compress/jobs")
            assert status2 == 200
            removable = json.loads(body2)["removable"]
            assert any(r["path"].endswith("clip.avi") for r in removable)
        finally:
            httpd2.shutdown()
            httpd2.server_close()
            thread2.join(timeout=5)


class TestCompressRemovableDeduplicatesRepeatedCompressions:
    def test_older_manifest_for_the_same_pair_is_dropped_not_shown_twice(
        self, real_archive_root,
    ):
        # Regression: re-compressing the same original across two separate
        # sessions (trying a different quality profile, deleting the
        # compressed sibling in between to retry -- compress.
        # compressed_dst_path() is deterministic per original, independent
        # of profile, so both runs target the SAME dst_abs) leaves two
        # `!MOVES` batches on disk, each with its own op=compress/
        # status=done row for the same (src_abs, dst_abs) pair. Only one
        # file can physically exist at dst_abs right now -- the results
        # panel must show that ONE current result, not one row per
        # historical manifest, and must report the size actually on disk,
        # not whichever row happened to be read.
        new_dir = f"{real_archive_root}/!NEW"
        os.makedirs(new_dir, exist_ok=True)
        src_abs = f"{new_dir}/clip.avi"
        with open(src_abs, "wb") as f:
            f.write(b"original bytes" * 100)
        dst_abs = compress.compressed_dst_path(src_abs, compress.load_compress_config(None).suffix)

        def write_compress_batch(ts, compressed_content):
            batch_dir = f"{real_archive_root}/{config.MOVES_DIR_NAME}/{ts}"
            manifest_path = f"{batch_dir}/{config.MANIFEST_FILENAME}"
            with open(dst_abs, "wb") as f:
                f.write(compressed_content)
            row = manifest.ManifestRow(
                op=manifest.OP_COMPRESS, src_abs=src_abs, dst_rel=dst_abs,
                size=len(compressed_content), mtime=0, hash="deadbeef", group_id="1",
                keeper_abs=src_abs, reason="libx265 crf21", ts=ts,
                tool_version=config.TOOL_VERSION, status=manifest.STATUS_DONE,
            )
            manifest.append_planned(manifest_path, row)
            manifest.append_confirmation(manifest_path, row)

        # Older run: recorded a result whose bytes are no longer at dst_abs.
        write_compress_batch("2026-01-01_00-00", b"x" * 1000)
        # Newer run: what is ACTUALLY on disk right now.
        write_compress_batch("2026-01-01_00-05", b"y" * 2000)

        state = review_state.ReviewState(
            fs=platform_win.WindowsFileSystem(), archive_root=real_archive_root,
            duplicate_groups=[], decisions=[], thumbs=MagicMock(), thumb_sources={},
            file_registry={}, groups_json=[], total_reclaimable_bytes=0, token="t",
        )

        removable = review_compress_queue._compress_removable_candidates(state)

        matches = [r for r in removable if r["path"].endswith("clip.avi")]
        assert len(matches) == 1, f"expected exactly one row, got {matches}"
        assert matches[0]["compressed_size"] == 2000


class TestCompressSkipHTTP:
    def test_skip_round_trip(self, compress_server):
        httpd, state, fs = compress_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        group_id = candidate["group_id"]

        status, body, _ = _post_json(
            state.port, "/api/compress/skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": group_id, "skip": True},
        )
        assert status == 200
        out = json.loads(body)
        assert out["skipped"] is True
        assert out["persisted"] is True

        payload2 = _wait_compress_ready(state.port)
        row = next(c for c in payload2["candidates"] if c["group_id"] == group_id)
        assert row["skipped"] is True

        status, body, _ = _post_json(
            state.port, "/api/compress/skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": group_id, "skip": False},
        )
        assert status == 200
        assert json.loads(body)["skipped"] is False
        payload3 = _wait_compress_ready(state.port)
        row3 = next(c for c in payload3["candidates"] if c["group_id"] == group_id)
        assert row3["skipped"] is False

    def test_the_decision_is_written_to_the_cache_database_itself(self, compress_server):
        httpd, state, fs = compress_server
        payload = _wait_compress_ready(state.port)
        candidate = payload["candidates"][0]
        _post_json(
            state.port, "/api/compress/skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": candidate["group_id"], "skip": True},
        )
        cache = Cache(state.db_path)
        try:
            stored = cache.list_compress_skips()
        finally:
            cache.close()
        assert len(stored) == 1

    def test_unknown_group_id_is_rejected(self, compress_server):
        httpd, state, fs = compress_server
        status, body, _ = _post_json(
            state.port, "/api/compress/skip",
            headers={"X-Dedup-Token": "test-token"},
            payload={"group_id": "c9_9", "skip": True},
        )
        assert status == 400

    def test_requires_the_mutating_request_gate(self, compress_server):
        httpd, state, fs = compress_server
        status, body, _ = _post_json(
            state.port, "/api/compress/skip",
            headers={"X-Dedup-Token": "wrong-token"},
            payload={"group_id": "c1_1", "skip": True},
        )
        assert status == 403


class TestRescanClearsCompressButKeepsSkips:
    def test_rescan_clears_compress_registry_and_applied_ids_but_keeps_skips(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")

        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root="D:/Archive", duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"), host="127.0.0.1", port=0,
            token="test-token", db_path=str(tmp_path / "cache.sqlite3"),
            rescan=lambda: ([], [], []),
        )
        try:
            state.compress_registry["c1_1:0"] = {
                "path": "D:/Archive/clip.mp4", "group_id": "c1_1", "size": 100,
            }
            state.applied_compress_group_ids.add("c1_1")
            with state.compress_lock:
                state.compress_computations["!NEW"] = review_lazy._LazyComputation()
                state.compress_progress["!NEW"] = ProgressCounter()

            _payload_out, error = review_skips._set_compress_skip(state, "c1_1", True)
            assert error is None

            status, _body, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 200

            assert state.compress_registry == {}
            assert state.applied_compress_group_ids == set()
            assert state.compress_computations == {}
            assert state.compress_progress == {}
            # Skips survive a rescan -- keyed by member paths, not ids.
            assert len(state.compress_skips) == 1
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_a_running_queue_is_left_untouched_by_a_rescan(self, tmp_path):
        # Rescan only clears the
        # CANDIDATE-LIST registries, never an in-flight queue's own state.
        fs = FakeFileSystem()
        fs.add_dir("D:/Archive")
        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root="D:/Archive", duplicate_groups=[], decisions=[],
            thumbs_dir=str(tmp_path / "thumbs"), host="127.0.0.1", port=0,
            token="test-token", rescan=lambda: ([], [], []),
        )
        try:
            job = review_state.CompressJob(
                file_id="c1_1:0", group_id="c1_1", path="D:/Archive/clip.mp4",
                size=100, profile_name="x265", status="encoding",
            )
            state.compress_jobs = [job]
            state.compress_queue_running = True

            status, _body, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert status == 200

            assert state.compress_queue_running is True
            assert state.compress_jobs == [job]
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
