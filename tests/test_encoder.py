"""Tests for encoder.py -- command construction (pure) and staging/
encode/verify (integration, real ffmpeg via lavfi-generated clips).

Command-construction tests never touch a subprocess. The integration
class calls the real bundled ffmpeg on tiny (~1s, low-res) lavfi clips
and skips itself when ffmpeg is not resolvable (docs/technical/development/conventions-and-tests.md).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from tidy import config
from tidy.compression import encoder
from tidy.compression import hwdetect
from tidy.compression.probe import ProbeResult, ToolBlockedError, probe_file

_FFMPEG = hwdetect._resolve_ffmpeg_exe(None)
_FFPROBE = hwdetect._resolve_ffprobe_exe(_FFMPEG) if _FFMPEG else None
_HAS_FFMPEG = bool(_FFMPEG and _FFPROBE)

requires_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not resolvable")


def _probe(
    codec="h264", width=320, height=240, fps=25.0, bitrate=500_000,
    duration=1.0, has_audio=False, rotation=0, audio_codec="",
):
    return ProbeResult(
        codec=codec, width=width, height=height, fps=fps, bitrate=bitrate,
        duration=duration, has_audio=has_audio, rotation=rotation,
        audio_codec=audio_codec,
    )


def _make_clip(path: str, duration: float = 1.0, size: str = "160x120", rate: int = 25) -> None:
    cmd = [
        _FFMPEG, "-y", "-v", "error", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", path,
    ]
    subprocess.run(cmd, check=True, timeout=30)


# -- TestBuildCommand (pure) --------------------------------------------------

class TestBuildCommand:
    def test_x265_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(),
        )
        assert cmd[cmd.index("-c:v") + 1] == "libx265"
        assert cmd[cmd.index("-crf") + 1] == "21"
        assert cmd[cmd.index("-preset") + 1] == "slow"
        assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p10le"
        assert cmd[cmd.index("-c:s") + 1] == "copy"
        assert "-noautorotate" in cmd
        assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
        assert cmd[-1] == "dst.mp4"

    def test_av1_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["av1"], _probe(),
        )
        assert cmd[cmd.index("-c:v") + 1] == "libsvtav1"
        assert "-svtav1-params" in cmd
        assert "-c:s" not in cmd  # subtitle copy is HEVC-only
        assert "-tag:v" not in cmd  # hvc1 is HEVC-specific

    def test_audio_copy_when_not_pcm(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"],
            _probe(has_audio=True, audio_codec="aac"),
        )
        assert cmd[cmd.index("-c:a") + 1] == "copy"

    def test_audio_reencoded_when_pcm(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"],
            _probe(has_audio=True, audio_codec="pcm_s16le"),
        )
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert "-b:a" in cmd

    def test_no_audio_flags_when_silent(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(has_audio=False),
        )
        assert "-c:a" not in cmd

    def test_rotation_metadata_written(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(rotation=-90),
        )
        assert cmd[cmd.index("-metadata:s:v:0") + 1] == "rotate=-90"

    def test_unknown_encoder_raises(self):
        with pytest.raises(ValueError):
            encoder.build_command(
                "ffmpeg.exe", "src.mp4", "dst.mp4", {"encoder": "mystery"}, _probe(),
            )

    def test_input_duration_limit_adds_t_flag_before_i(self):
        # docs/technical/components/compression-mechanism.md: -t on the INPUT, not a trim filter and not an
        # output-side -t (which would still decode the whole source).
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(),
            input_duration_limit=6.0,
        )
        t_index = cmd.index("-t")
        i_index = cmd.index("-i")
        assert cmd[t_index + 1] == "6.0"
        assert t_index < i_index

    def test_no_input_duration_limit_by_default(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(),
        )
        assert "-t" not in cmd


# -- TestBuildCommandHardware (pure) -- docs/technical/components/compression-mechanism.md ------------------------

_HEVC_NVENC_PROFILE = {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"}
_AV1_NVENC_PROFILE = {"encoder": "av1_nvenc", "cq": 30, "preset": "p7"}
_HEVC_QSV_PROFILE = {"encoder": "hevc_qsv", "global_quality": 23}
_AV1_QSV_PROFILE = {"encoder": "av1_qsv", "global_quality": 30}


class TestBuildCommandHardware:
    def test_hevc_nvenc_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _HEVC_NVENC_PROFILE, _probe(),
        )
        assert cmd[cmd.index("-c:v") + 1] == "hevc_nvenc"
        assert cmd[cmd.index("-preset") + 1] == "p7"
        assert cmd[cmd.index("-tune") + 1] == "hq"
        assert cmd[cmd.index("-rc") + 1] == "vbr"
        assert cmd[cmd.index("-cq") + 1] == "23"
        assert cmd[cmd.index("-b:v") + 1] == "0"
        assert cmd[cmd.index("-profile:v") + 1] == "main10"
        assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"
        assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
        assert cmd[cmd.index("-c:s") + 1] == "copy"
        assert "-gpu" not in cmd  # no device given
        assert "-noautorotate" in cmd
        assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"

    def test_hevc_nvenc_device_passed_as_gpu_flag(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _HEVC_NVENC_PROFILE, _probe(), device="1",
        )
        assert cmd[cmd.index("-gpu") + 1] == "1"

    def test_av1_nvenc_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _AV1_NVENC_PROFILE, _probe(), device="0",
        )
        assert cmd[cmd.index("-c:v") + 1] == "av1_nvenc"
        assert cmd[cmd.index("-cq") + 1] == "30"
        assert cmd[cmd.index("-gpu") + 1] == "0"
        assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"
        assert "-profile:v" not in cmd  # AV1 has no main10 profile flag
        assert "-tag:v" not in cmd
        assert "-c:s" not in cmd

    def test_hevc_qsv_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _HEVC_QSV_PROFILE, _probe(),
        )
        assert cmd[cmd.index("-c:v") + 1] == "hevc_qsv"
        assert cmd[cmd.index("-preset") + 1] == "veryslow"
        assert cmd[cmd.index("-global_quality") + 1] == "23"
        assert cmd[cmd.index("-profile:v") + 1] == "main10"
        assert cmd[cmd.index("-pix_fmt") + 1] == "p010le"
        assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
        assert cmd[cmd.index("-c:s") + 1] == "copy"
        assert "-qsv_device" not in cmd
        assert "-gpu" not in cmd

    def test_hevc_qsv_ignores_device_param(self):
        # hwdetect.py's QSV EncoderInfo.device is always "" (one
        # default adapter) -- build_command() accepts `device` for
        # signature uniformity with NVENC but must not emit -qsv_device.
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _HEVC_QSV_PROFILE, _probe(), device="0",
        )
        assert "-qsv_device" not in cmd

    def test_av1_qsv_profile_flags(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _AV1_QSV_PROFILE, _probe(),
        )
        assert cmd[cmd.index("-c:v") + 1] == "av1_qsv"
        assert cmd[cmd.index("-global_quality") + 1] == "30"
        assert "-profile:v" not in cmd
        assert "-tag:v" not in cmd
        assert "-c:s" not in cmd

    def test_hevc_nvenc_audio_and_rotation_still_apply(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4", _HEVC_NVENC_PROFILE,
            _probe(has_audio=True, audio_codec="aac", rotation=-90),
        )
        assert cmd[cmd.index("-c:a") + 1] == "copy"
        assert cmd[cmd.index("-metadata:s:v:0") + 1] == "rotate=-90"


class TestStagingPath:
    def test_builds_under_staging_dir(self, tmp_path):
        path = encoder.staging_path(str(tmp_path / ".compress"), "job1")
        assert path.endswith("job1.mp4")
        assert ".compress" in path.replace(os.sep, "/").split("/")

    def test_ensure_staging_dir_creates(self, tmp_path):
        d = str(tmp_path / ".compress")
        assert not os.path.isdir(d)
        encoder.ensure_staging_dir(d)
        assert os.path.isdir(d)


# -- integration: real lavfi clips -------------------------------------------

@requires_ffmpeg
class TestEncodeAndVerifyRealFfmpeg:
    @pytest.fixture
    def src_clip(self, tmp_path):
        path = str(tmp_path / "src.mp4")
        _make_clip(path, duration=1.0, rate=25)
        return path

    def test_input_duration_limit_truncates_the_real_encode(self, tmp_path):
        # docs/technical/components/compression-mechanism.md's calibration clip: encoding a 3s slice out of a
        # longer real source must actually produce a ~3s file, not the
        # full source -- proves -t before -i works as an INPUT limit on
        # the real bundled binary, not just in the argv shape.
        src = str(tmp_path / "src_long.mp4")
        _make_clip(src, duration=10.0, rate=25)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_calib_clip")
        cmd = encoder.build_command(
            _FFMPEG, src, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
            input_duration_limit=3.0,
        )
        result = encoder.run_encode(cmd, duration=3.0)
        assert result.success, result.reason
        dst_probe = probe_file(_FFPROBE, dst)
        assert dst_probe is not None
        assert dst_probe.duration < 5.0

    def test_encode_x265_produces_valid_staged_file_under_compress_dir(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        assert p is not None
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job1")

        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        result = encoder.run_encode(cmd, duration=p.duration)

        assert result.success, result.reason
        assert os.path.isfile(dst)
        # Every encoder.py output path lands under .dedup/.compress/, never
        # directly in the archive (docs/technical/components/compression-mechanism.md).
        assert ".compress" in dst.replace(os.sep, "/").split("/")

    def test_run_encode_reports_progress(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job2")
        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        seen = []
        result = encoder.run_encode(cmd, duration=p.duration, on_progress=seen.append)
        assert result.success, result.reason
        assert seen, "expected at least one progress callback"
        assert seen[-1] <= p.duration + 0.01

    def test_run_encode_failure_on_bad_input(self, tmp_path):
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job3")
        missing_src = str(tmp_path / "does-not-exist.mp4")
        p = _probe()
        cmd = encoder.build_command(
            _FFMPEG, missing_src, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        result = encoder.run_encode(cmd, duration=p.duration)
        assert not result.success
        assert result.reason

    def test_verify_output_passes_for_good_encode(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job4")
        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        assert encoder.run_encode(cmd, duration=p.duration).success

        verdict = encoder.verify_output(
            _FFMPEG, _FFPROBE, src_clip, dst, p, verify_xpsnr=True, min_xpsnr=20.0,
        )
        assert verdict.ok, verdict.reason
        assert verdict.xpsnr_y is not None and verdict.xpsnr_y > 20.0

    def test_verify_output_fails_on_duration_mismatch(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        other = str(tmp_path / "other.mp4")
        _make_clip(other, duration=2.0, rate=25)  # different duration
        verdict = encoder.verify_output(_FFMPEG, _FFPROBE, src_clip, other, p, verify_xpsnr=False)
        assert not verdict.ok
        assert "duration" in verdict.reason

    def test_verify_output_fails_on_frame_count_mismatch(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        # Same duration, different frame rate -> different frame count
        # (this is exactly the VFR->CFR duplication pitfall, docs/technical/components/compression-mechanism.md).
        other = str(tmp_path / "other.mp4")
        _make_clip(other, duration=1.0, rate=30)
        verdict = encoder.verify_output(_FFMPEG, _FFPROBE, src_clip, other, p, verify_xpsnr=False)
        assert not verdict.ok
        assert "frame count" in verdict.reason

    def test_verify_output_fails_on_missing_staged_file(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        missing = str(tmp_path / "nope.mp4")
        verdict = encoder.verify_output(_FFMPEG, _FFPROBE, src_clip, missing, p)
        assert not verdict.ok
        assert "missing" in verdict.reason

    def test_run_encode_stamps_src_mtime_on_staged_file(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job6")
        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        fake_mtime_ns = 1_600_000_000_000_000_000  # 2020-09-13, arbitrary
        result = encoder.run_encode(cmd, duration=p.duration, src_mtime_ns=fake_mtime_ns)
        assert result.success, result.reason
        assert os.stat(dst).st_mtime_ns == fake_mtime_ns

    def test_run_encode_cancellation_kills_process_and_reports_cancelled(self, tmp_path):
        # A long, heavier clip so a "slow"-preset x265 encode has enough
        # real work in flight for the cancel to land mid-encode rather
        # than after ffmpeg has already exited on its own.
        src = str(tmp_path / "src_long.mp4")
        _make_clip(src, duration=8.0, size="1280x720", rate=30)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_cancel")
        cmd = encoder.build_command(
            _FFMPEG, src, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        cancel_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.5)
            cancel_event.set()

        canceller = threading.Thread(target=_cancel_soon)
        canceller.start()
        start = time.monotonic()
        result = encoder.run_encode(cmd, duration=p.duration, cancel_event=cancel_event)
        elapsed = time.monotonic() - start
        canceller.join()

        assert not result.success
        assert result.reason == "cancelled"
        # A full slow-preset encode of 8s/720p would take much longer than
        # this on any machine this test runs on -- proof the process was
        # actually killed rather than left to finish on its own.
        assert elapsed < 6.0

    def test_decode_frame_count(self, src_clip):
        assert encoder._decode_frame_count(_FFMPEG, src_clip) == 25

    def test_decode_frame_count_fails_on_bad_file(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video")
        assert encoder._decode_frame_count(_FFMPEG, str(bad)) is None

    def test_measure_xpsnr_of_own_encode_is_reasonably_high(self, tmp_path, src_clip):
        p = probe_file(_FFPROBE, src_clip)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job5")
        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        assert encoder.run_encode(cmd, duration=p.duration).success
        y = encoder.measure_xpsnr_y(_FFMPEG, dst, src_clip)
        assert y is not None and y > 20.0

    def test_measure_xpsnr_of_head_clip_compares_only_that_head(self, tmp_path):
        # The calibration case: a clip encoded from the source's first
        # second, measured against the full source. Without shortest=1 the
        # clip's last frame was compared against every remaining source
        # frame (~11 dB for a fine encode).
        src = str(tmp_path / "src.mp4")
        subprocess.run([
            _FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=duration=3:size=320x240:rate=30",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", src,
        ], check=True, timeout=30)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "calib-1")
        cmd = encoder.build_command(
            _FFMPEG, src, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
            input_duration_limit=1.0,
        )
        assert encoder.run_encode(cmd, duration=1.0).success

        y = encoder.measure_xpsnr_y(_FFMPEG, dst, src)
        assert y is not None and y > 35.0

    def test_measure_xpsnr_of_vfr_source_is_high(self, tmp_path):
        # Phone-like VFR: a 60 fps grid with irregularly dropped frames on
        # a 1/90000 timescale, which the encode's own timescale cannot
        # reproduce exactly -- frames must still pair up one to one.
        src = str(tmp_path / "vfr.mp4")
        subprocess.run([
            _FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=duration=2:size=320x240:rate=60",
            "-vf", "select='not(eq(mod(n,7),3)+eq(mod(n,11),5))'",
            "-fps_mode", "passthrough", "-video_track_timescale", "90000",
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", src,
        ], check=True, timeout=30)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_vfr")
        cmd = encoder.build_command(
            _FFMPEG, src, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        assert encoder.run_encode(cmd, duration=p.duration).success

        verdict = encoder.verify_output(
            _FFMPEG, _FFPROBE, src, dst, p, verify_xpsnr=True, min_xpsnr=35.0,
        )
        assert verdict.ok, verdict.reason

    def test_measure_xpsnr_survives_non_cp1250_path(self, tmp_path):
        # Regression test: a source path containing characters outside the
        # Windows console codepage (Cyrillic, matching this tool's own
        # archive root D:\Фото\Мій архів) used to make measure_xpsnr_y()
        # return None on every real-archive file, because
        # subprocess.run(..., text=True) without an explicit encoding=
        # decodes ffmpeg's stderr with locale.getpreferredencoding()
        # (cp1250 on this machine) -- and ffmpeg's own output is UTF-8
        # regardless. The mismatch raised UnicodeDecodeError inside
        # subprocess's internal reader thread, silently, leaving
        # result.stderr None and the XPSNR regex never matching, which
        # verify_output() reported as "XPSNR measurement failed" -- a
        # generic message indistinguishable from a real ffmpeg crash.
        # Same fix and same reasoning as probe.py's _run_ffprobe().
        cyrillic_dir = tmp_path / "Фото" / "Мій архів"
        cyrillic_dir.mkdir(parents=True)
        src_clip = str(cyrillic_dir / "src.mp4")
        _make_clip(src_clip, duration=1.0, rate=25)

        p = probe_file(_FFPROBE, src_clip)
        staging_dir = str(cyrillic_dir / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_cyrillic")
        cmd = encoder.build_command(
            _FFMPEG, src_clip, dst, config.DEFAULT_COMPRESS_PROFILES["x265"], p,
        )
        assert encoder.run_encode(cmd, duration=p.duration).success

        y = encoder.measure_xpsnr_y(_FFMPEG, dst, src_clip)
        assert y is not None and y > 20.0

        verdict = encoder.verify_output(
            _FFMPEG, _FFPROBE, src_clip, dst, p, verify_xpsnr=True, min_xpsnr=20.0,
        )
        assert verdict.ok, verdict.reason


# -- integration: real hardware encoders ---------------------------------------
#
# Each class skips unless hwdetect.detect() finds that encoder working on the
# machine running the suite; hardware facts are never asserted unconditionally.

@requires_ffmpeg
class TestEncodeAndVerifyHevcQsv:
    @pytest.fixture(autouse=True)
    def _skip_if_qsv_unavailable(self):
        hw = hwdetect.detect(None)
        if hw is None or not any(e.name == "hevc_qsv" for e in hw.encoders):
            pytest.skip("hevc_qsv not detected as working on this machine")

    def test_hevc_qsv_encode_and_verify_full_cycle(self, tmp_path):
        src = str(tmp_path / "src.mp4")
        _make_clip(src, duration=1.5, size="320x240", rate=25)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_qsv")
        profile = {"encoder": "hevc_qsv", "global_quality": 23}
        cmd = encoder.build_command(_FFMPEG, src, dst, profile, p)
        result = encoder.run_encode(cmd, duration=p.duration)
        assert result.success, result.reason
        assert os.path.isfile(dst)
        # Same "lands under .dedup/.compress/" contract as every other
        # encoder.py output path.
        assert ".compress" in dst.replace(os.sep, "/").split("/")

        verdict = encoder.verify_output(
            _FFMPEG, _FFPROBE, src, dst, p, verify_xpsnr=True, min_xpsnr=20.0,
        )
        assert verdict.ok, verdict.reason
        assert verdict.xpsnr_y is not None and verdict.xpsnr_y > 20.0

    def test_hevc_qsv_rotation_and_fps_mode_preserved(self, tmp_path):
        # Same pitfalls as docs/technical/components/compression-mechanism.md (autorotation, VFR->CFR), now
        # checked against the hardware branch, not just x265/av1.
        src = str(tmp_path / "src_rot.mp4")
        _make_clip(src, duration=1.0, size="320x240", rate=25)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_qsv_rot")
        profile = {"encoder": "hevc_qsv", "global_quality": 30}
        cmd = encoder.build_command(_FFMPEG, src, dst, profile, p)
        assert "-metadata:s:v:0" in cmd
        assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
        result = encoder.run_encode(cmd, duration=p.duration)
        assert result.success, result.reason
        dst_probe = probe_file(_FFPROBE, dst)
        assert dst_probe is not None
        assert abs(dst_probe.duration - p.duration) <= 0.5


@requires_ffmpeg
class TestEncodeAndVerifyHevcNvenc:
    @pytest.fixture(autouse=True)
    def _skip_if_nvenc_unavailable(self):
        hw = hwdetect.detect(None)
        devices = [e.device for e in hw.encoders if e.name == "hevc_nvenc"] if hw else []
        if not devices:
            pytest.skip("hevc_nvenc not detected as working on this machine")
        self.device = devices[0]

    def test_hevc_nvenc_encode_and_verify_full_cycle(self, tmp_path):
        src = str(tmp_path / "src.mp4")
        _make_clip(src, duration=1.5, size="320x240", rate=25)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_nvenc")
        profile = {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"}
        cmd = encoder.build_command(_FFMPEG, src, dst, profile, p, device=self.device)
        result = encoder.run_encode(cmd, duration=p.duration)
        assert result.success, result.reason
        assert ".compress" in dst.replace(os.sep, "/").split("/")

        verdict = encoder.verify_output(
            _FFMPEG, _FFPROBE, src, dst, p, verify_xpsnr=True, min_xpsnr=20.0,
        )
        assert verdict.ok, verdict.reason
        assert verdict.xpsnr_y is not None and verdict.xpsnr_y > 20.0

    def test_hevc_nvenc_rotation_and_fps_mode_preserved(self, tmp_path):
        src = str(tmp_path / "src_rot.mp4")
        _make_clip(src, duration=1.0, size="320x240", rate=25)
        p = probe_file(_FFPROBE, src)
        staging_dir = str(tmp_path / ".compress")
        encoder.ensure_staging_dir(staging_dir)
        dst = encoder.staging_path(staging_dir, "job_nvenc_rot")
        profile = {"encoder": "hevc_nvenc", "cq": 30, "preset": "p7"}
        cmd = encoder.build_command(_FFMPEG, src, dst, profile, p, device=self.device)
        assert "-metadata:s:v:0" in cmd
        assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
        result = encoder.run_encode(cmd, duration=p.duration)
        assert result.success, result.reason
        dst_probe = probe_file(_FFPROBE, dst)
        assert dst_probe is not None
        assert abs(dst_probe.duration - p.duration) <= 0.5


# -- Smart App Control block (winerror 4551) ----------------------------------

_needs_winerror = pytest.mark.skipif(
    os.name != "nt", reason="OSError.winerror exists only on Windows",
)


def _blocked_error():
    return OSError(None, "blocked", None, config.WINERROR_APP_CONTROL_BLOCKED)


def _raise_blocked(*_a, **_k):
    raise _blocked_error()


class TestToolBlocked:
    @_needs_winerror
    def test_run_encode_blocked_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(encoder.subprocess, "Popen", _raise_blocked)
        with pytest.raises(ToolBlockedError):
            encoder.run_encode(["ffmpeg.exe", "-i", "a", str(tmp_path / "out.mp4")])

    def test_run_encode_other_oserror_returns_failure(self, monkeypatch, tmp_path):
        def boom(*_a, **_k):
            raise OSError(2, "no such file")

        monkeypatch.setattr(encoder.subprocess, "Popen", boom)
        dst = str(tmp_path / "out.mp4")
        result = encoder.run_encode(["ffmpeg.exe", "-i", "a", dst])
        assert result.success is False
        assert result.staging_path == dst
        assert "no such file" in result.reason

    @_needs_winerror
    def test_verify_output_propagates_block_from_ffprobe(self, monkeypatch, tmp_path):
        staged = tmp_path / "staged.mp4"
        staged.write_bytes(b"x" * 10)
        monkeypatch.setattr(subprocess, "run", _raise_blocked)
        with pytest.raises(ToolBlockedError):
            encoder.verify_output(
                "ffmpeg.exe", "ffprobe.exe", "src.mp4", str(staged), _probe(),
            )

    @_needs_winerror
    def test_verify_output_propagates_block_from_decode(self, monkeypatch, tmp_path):
        staged = tmp_path / "staged.mp4"
        staged.write_bytes(b"x" * 10)
        monkeypatch.setattr(encoder, "probe_file", lambda *_a, **_k: _probe())
        monkeypatch.setattr(subprocess, "run", _raise_blocked)
        with pytest.raises(ToolBlockedError):
            encoder.verify_output(
                "ffmpeg.exe", "ffprobe.exe", "src.mp4", str(staged), _probe(),
            )

    @_needs_winerror
    def test_measure_xpsnr_blocked_raises(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_blocked)
        with pytest.raises(ToolBlockedError):
            encoder.measure_xpsnr_y("ffmpeg.exe", "a.mp4", "b.mp4")

    def test_decode_other_oserror_returns_none(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError(2, "no such file")

        monkeypatch.setattr(subprocess, "run", boom)
        assert encoder._decode_frame_count("ffmpeg.exe", "a.mp4") is None
        assert encoder.measure_xpsnr_y("ffmpeg.exe", "a.mp4", "b.mp4") is None


class TestProtocolWhitelist:
    def test_build_command_whitelists_protocols_before_input(self):
        cmd = encoder.build_command(
            "ffmpeg.exe", "src.mp4", "dst.mp4",
            config.DEFAULT_COMPRESS_PROFILES["x265"], _probe(),
        )
        i = cmd.index("-i")
        assert cmd[i - 2:i] == ["-protocol_whitelist", "file,pipe"]
