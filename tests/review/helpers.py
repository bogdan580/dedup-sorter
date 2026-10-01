"""Shared non-fixture helpers for the review tests."""
from __future__ import annotations

import http.client
import io
import json
import time
import urllib.parse
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from PIL import Image
from tidy.analysis import events
from tidy.compression import hwdetect
from tidy.compression import probe
from tidy.review import state as review_state
from tidy.review.views import series as series_views
from tidy.analysis import series
from fakes import FakeFileSystem


_FFMPEG_EXE = hwdetect._resolve_ffmpeg_exe(None)


_HAS_FFMPEG = bool(_FFMPEG_EXE)


requires_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not resolvable")


def _jpeg_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 8), color).save(buf, "JPEG")
    return buf.getvalue()


# --- pure view-model builders for series/events -------------------------

def _series_member(path, *, keeper_path=None, size=1000, width=100, height=200,
                    sharpness=5.0, has_exif=True) -> "series.SeriesMember":
    when = datetime(2021, 4, 25, 1, 5, 18, tzinfo=timezone.utc)
    return series.SeriesMember(
        path=path,
        timestamp=series.timeline.Timestamp(when=when, source="exif"),
        dhash=0,
        width=width,
        height=height,
        sharpness=sharpness,
        size=size,
        has_exif=has_exif,
    )


def _series_group(members, suggested_keeper, *, kind=series.KIND_BURST, max_gap_seconds=2.0):
    return series.SeriesGroup(
        members=tuple(members),
        suggested_keeper=suggested_keeper,
        max_gap_seconds=max_gap_seconds,
        kind=kind,
    )


def _event_member(path, *, when=None, size=None, timestamp_source="exif",
                   camera_model=None, gps_source=events.GPS_SOURCE_NONE,
                   is_suspicious=False) -> "events.EventMember":
    return events.EventMember(
        path=path,
        when=when or datetime(2021, 4, 25, 1, 5, 18, tzinfo=timezone.utc),
        timestamp_source=timestamp_source,
        camera_model=camera_model,
        gps=None,
        gps_source=gps_source,
        is_suspicious=is_suspicious,
    )


def _event(members, **kwargs):
    defaults = dict(
        place_name="Krakow",
        country_cc="PL",
        suggested_folder="#Home",
        matched_existing_dir=False,
        photo_count=len(members),
        video_count=0,
    )
    defaults.update(kwargs)
    return events.Event(members=tuple(members), **defaults)


# --- selection model (pure, registry-dict based) ----------------------------

_FILE_REGISTRY = {
    "1:0": {"path": "D:/A/a.jpg", "group_id": "1", "size": 10, "hash": "h1", "reason": None},
    "1:1": {"path": "D:/A/b.jpg", "group_id": "1", "size": 10, "hash": "h1", "reason": "dup-exact"},
    "2:0": {"path": "D:/B/a.jpg", "group_id": "2", "size": 20, "hash": "h2", "reason": None},
    "2:1": {"path": "D:/B/b.jpg", "group_id": "2", "size": 20, "hash": "h2", "reason": "dup-exact"},
}


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())
    finally:
        conn.close()


def _post(port, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())
    finally:
        conn.close()


def _post_json(port, path, headers=None, payload=None):
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Type", "application/json")
    return _post(port, path, headers=hdrs, body=body)


def _wait_placement_ready(port, source="!NEW", timeout=10.0):
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        status, body, _ = _get(port, "/api/placement?source=" + urllib.parse.quote(source))
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        if payload["status"] in ("ready", "error"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"placement never became ready: {payload}")


# --- persisted "pomin ta grupe" -------------------------------------------
#
# "W Serie i podobne jezeli zaznacze pomin to zapisz gdzies w cache lub bazie
# zeby nie pokazywac znowu nawet przy nastepnym uruchomieniu."
#
# The unit-level pieces are tested next to the code that owns them
# (tests/test_series.py for the member-path identity rules,
# tests/test_cache.py for the table and its survival of every invalidation).
# What can only be tested here is the promise the user actually made: that a
# checkbox click in one process is still in effect in the NEXT one.

def _publish_series_result(state, groups):
    """Put a finished series computation on `state` without running the real
    engine -- exactly what _run_series_computation() does on success, minus
    the minutes of dHash work. Marking the computation "ready" this way also
    stops GET /api/series from starting the real background worker
    (start_if_idle() only fires from "idle")."""
    result = series.SeriesResult(groups=list(groups))
    views, thumb_sources, registry = series_views.build_series_views(result)
    state.thumb_sources.update(thumb_sources)
    state.series_registry.update(registry)
    state.series_computation.finish_ready(lambda: {"groups": views, "notes": {}})
    return views


def _series_groups_json(port):
    status, body, _ = _get(port, "/api/series")
    assert status == 200
    return json.loads(body)["groups"]


# --- compress screen ("Sugestia kompresji") -------------------------------
#
# TestBuildCompressViews is the pure layer: hand-built CompressCandidate/
# CompressSkipped/ProbeResult, no sockets, no fs I/O, no subprocess --
# mirrors TestBuildGroupViews' split from TestReviewServerHTTP at the top of
# this file. TestCompressHTTP/TestCompressEncodersHTTP are the real-socket
# layer and need an actual video ffprobe can open (probe.py calls ffprobe
# directly on a real path, bypassing the FileSystem seam -- its own module
# docstring), so the compress_server fixture below writes a real, tiny
# lavfi-generated clip next to a FakeFileSystem shadow of the same archive,
# the same combination test_encoder.py's own integration class uses.

def _probe_result(**overrides) -> "probe.ProbeResult":
    defaults = dict(
        codec="hevc", width=1920, height=1080, fps=59.94, bitrate=55_800_000,
        duration=25.76, has_audio=True, rotation=0, audio_codec="aac",
    )
    defaults.update(overrides)
    return probe.ProbeResult(**defaults)


def _wait_compress_ready(port, source="!NEW", timeout=20.0):
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        status, body, _ = _get(port, "/api/compress?source=" + urllib.parse.quote(source))
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        if payload["status"] in ("ready", "error"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"compress never became ready: {payload}")


def _wait_compress_jobs_done(port, timeout=60.0):
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        status, body, _ = _get(port, "/api/compress/jobs")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        if not payload["running"]:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"compress queue never finished: {payload}")


def _wait_calibration_terminal(port, timeout=60.0):
    """Poll GET /api/compress/jobs until calibration.status leaves
    queued/running, same shape as _wait_compress_ready()'s own polling."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = _get(port, "/api/compress/jobs")
        payload = json.loads(body.decode("utf-8"))
        calib = payload.get("calibration")
        if calib is not None and calib["status"] in ("done", "failed", "cancelled"):
            return calib
        time.sleep(0.2)
    raise TimeoutError("calibration job never reached a terminal status")


# --- Smart App Control block (probe.ToolBlockedError) ----------------------

_BLOCK_MESSAGE = (
    "ffmpeg/ffprobe blocked by Smart App Control (WinError 4551): ffmpeg.exe"
)


def _bare_state(tmp_path):
    return review_state.ReviewState(
        fs=FakeFileSystem(), archive_root="D:/Archive", duplicate_groups=[],
        decisions=[], thumbs=MagicMock(), thumb_sources={}, file_registry={},
        groups_json=[], total_reclaimable_bytes=0, token="t",
        compress_staging_dir=str(tmp_path / ".compress"),
    )


def _fake_hw_result():
    return hwdetect.HwDetectResult(
        ffmpeg_exe="ffmpeg.exe", ffprobe_exe="ffprobe.exe", ffmpeg_version="test",
        machine_id="m", encoders=(hwdetect.EncoderInfo(
            name="libx265", display_name="x265 (CPU)", device="", is_hardware=False,
            device_key="libx265",
        ),),
    )


def _raise_blocked(*_a, **_kw):
    raise probe.ToolBlockedError(_BLOCK_MESSAGE)
