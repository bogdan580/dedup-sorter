"""Tests for probe.py -- parsing ffprobe JSON output and ProbeResult logic.

The pure parsing tests (TestParseProbeJson, TestParseFps, TestParseRotation,
TestProbeResultBpp) run without ffprobe.  Integration tests that call the
real binary are in a separate class with a skipif guard.
"""
import os

import pytest

from tidy import config
from tidy.compression import probe


# -- fixtures: realistic ffprobe JSON snippets --------------------------------

def _make_probe_json(
    codec="hevc", width=1920, height=1080, fps_num=60000, fps_den=1001,
    bit_rate="55800000", duration="25.76", has_audio=True, rotation=None,
    rotation_in_tags=False, file_size=None,
):
    """Build a minimal but realistic ffprobe -show_streams -show_format JSON."""
    video_stream = {
        "codec_type": "video",
        "codec_name": codec,
        "width": width,
        "height": height,
        "avg_frame_rate": f"{fps_num}/{fps_den}",
        "r_frame_rate": f"{fps_num}/{fps_den}",
        "disposition": {"attached_pic": 0},
    }
    if bit_rate:
        video_stream["bit_rate"] = bit_rate
    if duration:
        video_stream["duration"] = duration
    if rotation is not None:
        if rotation_in_tags:
            video_stream["tags"] = {"rotate": str(rotation)}
        else:
            video_stream["side_data_list"] = [
                {"side_data_type": "Display Matrix", "rotation": rotation}
            ]

    streams = [video_stream]
    if has_audio:
        streams.append({
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
        })

    fmt = {}
    if duration:
        fmt["duration"] = duration
    if bit_rate:
        fmt["bit_rate"] = bit_rate
    if file_size:
        fmt["size"] = str(file_size)

    return {"streams": streams, "format": fmt}


# -- TestParseProbeJson -------------------------------------------------------

class TestParseProbeJson:
    def test_basic_hevc_1080p60(self):
        data = _make_probe_json()
        result = probe.parse_probe_json(data)
        assert result is not None
        assert result.codec == "hevc"
        assert result.width == 1920
        assert result.height == 1080
        assert result.fps == pytest.approx(59.94, abs=0.01)
        assert result.bitrate == 55800000
        assert result.duration == pytest.approx(25.76)
        assert result.has_audio is True
        assert result.rotation == 0

    def test_rotation_from_side_data(self):
        data = _make_probe_json(rotation=-90)
        result = probe.parse_probe_json(data)
        assert result is not None
        assert result.rotation == -90

    def test_rotation_from_tags(self):
        data = _make_probe_json(rotation=90, rotation_in_tags=True)
        result = probe.parse_probe_json(data)
        assert result is not None
        assert result.rotation == 90

    def test_no_audio_stream(self):
        data = _make_probe_json(has_audio=False)
        result = probe.parse_probe_json(data)
        assert result is not None
        assert result.has_audio is False

    def test_no_video_stream(self):
        data = {"streams": [{"codec_type": "audio", "codec_name": "aac"}], "format": {}}
        result = probe.parse_probe_json(data)
        assert result is None

    def test_attached_pic_skipped(self):
        """Album art with attached_pic disposition should not be treated as
        the video stream."""
        data = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 320,
                    "height": 240,
                    "disposition": {"attached_pic": 1},
                    "avg_frame_rate": "0/0",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                },
            ],
            "format": {"duration": "180.0"},
        }
        result = probe.parse_probe_json(data)
        assert result is None

    def test_bitrate_from_file_size_when_missing(self):
        """When bit_rate is missing, compute from file size and duration."""
        data = _make_probe_json(bit_rate=None, file_size=172100000, duration="25.76")
        # Remove bit_rate from format too
        data["format"].pop("bit_rate", None)
        result = probe.parse_probe_json(data)
        assert result is not None
        expected = int(172100000 * 8 / 25.76)
        assert result.bitrate == pytest.approx(expected, rel=0.01)

    def test_empty_streams(self):
        data = {"streams": [], "format": {}}
        assert probe.parse_probe_json(data) is None

    def test_h264_codec(self):
        data = _make_probe_json(codec="h264", width=3840, height=2160,
                                fps_num=30, fps_den=1)
        result = probe.parse_probe_json(data)
        assert result is not None
        assert result.codec == "h264"
        assert result.width == 3840
        assert result.height == 2160
        assert result.fps == pytest.approx(30.0)


# -- TestParseFps -------------------------------------------------------------

class TestParseFps:
    def test_fractional_fps(self):
        stream = {"avg_frame_rate": "60000/1001"}
        assert probe._parse_fps(stream) == pytest.approx(59.94, abs=0.01)

    def test_integer_fps(self):
        stream = {"avg_frame_rate": "30/1"}
        assert probe._parse_fps(stream) == pytest.approx(30.0)

    def test_plain_number(self):
        stream = {"avg_frame_rate": "25.0"}
        assert probe._parse_fps(stream) == pytest.approx(25.0)

    def test_fallback_to_r_frame_rate(self):
        stream = {"avg_frame_rate": "0/0", "r_frame_rate": "24000/1001"}
        assert probe._parse_fps(stream) == pytest.approx(23.976, abs=0.001)

    def test_zero_denominator_skipped(self):
        stream = {"avg_frame_rate": "60/0", "r_frame_rate": "30/1"}
        assert probe._parse_fps(stream) == pytest.approx(30.0)

    def test_empty_returns_zero(self):
        assert probe._parse_fps({}) == 0.0


# -- TestParseRotation --------------------------------------------------------

class TestParseRotation:
    def test_side_data_rotation(self):
        stream = {
            "side_data_list": [
                {"side_data_type": "Display Matrix", "rotation": -90}
            ]
        }
        assert probe._parse_rotation(stream) == -90

    def test_tag_rotation(self):
        stream = {"tags": {"rotate": "270"}}
        assert probe._parse_rotation(stream) == 270

    def test_side_data_takes_precedence(self):
        stream = {
            "side_data_list": [{"rotation": -90}],
            "tags": {"rotate": "180"},
        }
        assert probe._parse_rotation(stream) == -90

    def test_no_rotation(self):
        assert probe._parse_rotation({}) == 0

    def test_float_rotation_truncated(self):
        stream = {"side_data_list": [{"rotation": -89.9}]}
        assert probe._parse_rotation(stream) == -89


# -- TestProbeResultBpp -------------------------------------------------------

class TestProbeResultBpp:
    def test_samsung_s22_hevc(self):
        """The test.mp4 from section 2: 0.448 bpp."""
        r = probe.ProbeResult(
            codec="hevc", width=1920, height=1080, fps=59.94,
            bitrate=55_800_000, duration=25.76, has_audio=True, rotation=0,
        )
        assert r.bpp == pytest.approx(0.449, abs=0.01)

    def test_zero_dimensions(self):
        r = probe.ProbeResult(
            codec="hevc", width=0, height=1080, fps=30,
            bitrate=1000000, duration=10, has_audio=False, rotation=0,
        )
        assert r.bpp == 0.0

    def test_zero_fps(self):
        r = probe.ProbeResult(
            codec="hevc", width=1920, height=1080, fps=0,
            bitrate=1000000, duration=10, has_audio=False, rotation=0,
        )
        assert r.bpp == 0.0


# -- TestStripExtendedPrefix --------------------------------------------------

class TestStripExtendedPrefix:
    def test_long_path_prefix(self):
        assert probe._strip_extended_prefix("\\\\?\\D:\\Archive\\vid.mp4") == "D:\\Archive\\vid.mp4"

    def test_unc_prefix(self):
        assert probe._strip_extended_prefix("\\\\?\\UNC\\server\\share\\vid.mp4") == "\\\\server\\share\\vid.mp4"

    def test_no_prefix(self):
        assert probe._strip_extended_prefix("D:\\Archive\\vid.mp4") == "D:\\Archive\\vid.mp4"

    def test_forward_slash_path(self):
        assert probe._strip_extended_prefix("D:/Archive/vid.mp4") == "D:/Archive/vid.mp4"


# -- Smart App Control block (winerror 4551) ----------------------------------

def _blocked_error():
    return OSError(None, "blocked", None, config.WINERROR_APP_CONTROL_BLOCKED)


class _RecordingCache:
    def __init__(self):
        self.puts = []

    def get_compress_probe(self, *_a):
        return None

    def put_compress_probe(self, *args):
        self.puts.append(args)


class TestToolBlocked:
    def test_simulated_error_carries_winerror(self):
        if os.name != "nt":
            pytest.skip("OSError.winerror exists only on Windows")
        assert _blocked_error().winerror == config.WINERROR_APP_CONTROL_BLOCKED

    def test_raise_if_blocked_raises_chained_ascii_message(self):
        if os.name != "nt":
            pytest.skip("OSError.winerror exists only on Windows")
        exc = _blocked_error()
        with pytest.raises(probe.ToolBlockedError) as info:
            probe.raise_if_blocked(exc, "ffprobe.exe")
        assert info.value.__cause__ is exc
        assert "Smart App Control" in str(info.value)
        assert "4551" in str(info.value)
        assert "ffprobe.exe" in str(info.value)
        str(info.value).encode("ascii")

    def test_raise_if_blocked_ignores_other_errors(self):
        probe.raise_if_blocked(OSError(2, "not found"), "ffprobe.exe")
        probe.raise_if_blocked(OSError("plain"), "ffprobe.exe")

    def test_run_ffprobe_raises_when_blocked(self, monkeypatch):
        if os.name != "nt":
            pytest.skip("OSError.winerror exists only on Windows")

        def boom(*_a, **_k):
            raise _blocked_error()

        monkeypatch.setattr(probe.subprocess, "run", boom)
        with pytest.raises(probe.ToolBlockedError):
            probe._run_ffprobe("ffprobe.exe", "C:/x.mp4")
        with pytest.raises(probe.ToolBlockedError):
            probe.probe_file("ffprobe.exe", "C:/x.mp4")

    def test_run_ffprobe_other_oserror_returns_none(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError(2, "not found")

        monkeypatch.setattr(probe.subprocess, "run", boom)
        assert probe._run_ffprobe("ffprobe.exe", "C:/x.mp4") is None
        assert probe.probe_file("ffprobe.exe", "C:/x.mp4") is None

    def test_blocked_probe_is_never_cached(self, monkeypatch):
        if os.name != "nt":
            pytest.skip("OSError.winerror exists only on Windows")

        def boom(*_a, **_k):
            raise _blocked_error()

        monkeypatch.setattr(probe.subprocess, "run", boom)
        cache = _RecordingCache()
        with pytest.raises(probe.ToolBlockedError):
            probe.probe_file_cached("ffprobe.exe", "C:/x.mp4", 1, 2, cache=cache)
        assert cache.puts == []


class TestProbeProtocolWhitelist:
    def test_ffprobe_argv_whitelists_protocols(self, monkeypatch):
        seen = {}

        class _R:
            returncode = 1
            stderr = ""
            stdout = ""

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return _R()

        monkeypatch.setattr(probe.subprocess, "run", _fake_run)
        probe._run_ffprobe("ffprobe.exe", "C:/x.mp4")
        cmd = seen["cmd"]
        assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
        assert cmd.index("-protocol_whitelist") < len(cmd) - 1
