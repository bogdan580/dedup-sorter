"""Tests for compress.py -- heuristic, estimation, and candidacy selection.

All tests use FakeFileSystem for the `fs` seam and hand-built ProbeResult
objects -- no real files, no ffprobe.
"""
import pytest

from tidy.compression import compress
from tidy import config
from tidy.compression import hwdetect
from tidy.compression.probe import ProbeResult


# -- helpers ------------------------------------------------------------------

class _FakeFs:
    """Minimal fs stub: only needs exists()."""
    def __init__(self, existing_paths=()):
        self._existing = {p.casefold() for p in existing_paths}

    def exists(self, path):
        return path.casefold() in self._existing


def _probe(
    codec="hevc", width=1920, height=1080, fps=59.94,
    bitrate=55_800_000, duration=25.76, has_audio=True, rotation=0,
):
    return ProbeResult(
        codec=codec, width=width, height=height, fps=fps,
        bitrate=bitrate, duration=duration, has_audio=has_audio,
        rotation=rotation,
    )


# -- TestHeuristic ------------------------------------------------------------

class TestHeuristic:
    """Test each of the 9 rules (docs/business/rules/compression.md)."""

    def test_rule_1_compressed_suffix_skipped(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/video_compressed.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None
        assert s is not None
        assert "_compressed" in s.reason

    def test_rule_1_compressed_suffix_case_insensitive(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/video_COMPRESSED.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None

    def test_rule_2_sibling_compressed_exists(self):
        fs = _FakeFs(["D:/Archive/video_compressed.mp4"])
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None
        assert s is not None
        assert "sibling" in s.reason

    def test_rule_3_not_video_extension(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/photo.jpg", 100_000_000, _probe(), fs,
        )
        assert c is None
        assert s is not None
        assert "not a video" in s.reason

    def test_rule_4_excluded_zone_remove(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/!REMOVE/2024-01-01/video.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None
        assert "excluded" in s.reason

    def test_rule_4_excluded_zone_moves(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/!MOVES/2024-01-01/video.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None

    def test_rule_4_excluded_zone_dedup(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/.dedup/something/video.mp4", 100_000_000, _probe(), fs,
        )
        assert c is None

    def test_rule_4_new_is_not_excluded(self):
        """!NEW is deliberately NOT excluded (docs/business/rules/compression.md)."""
        fs = _FakeFs()
        probe = _probe(bitrate=55_800_000)  # bpp ~0.448, well above threshold
        c, s = compress.evaluate_file(
            "D:/Archive/!NEW/video.mp4", 100_000_000, probe, fs,
        )
        assert c is not None
        assert s is None

    def test_rule_5_probe_failed(self):
        fs = _FakeFs()
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, None, fs,
        )
        assert c is None
        assert "probe failed" in s.reason

    def test_rule_6_too_short(self):
        fs = _FakeFs()
        short_probe = _probe(duration=1.5)
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, short_probe, fs,
        )
        assert c is None
        assert "duration" in s.reason

    def test_rule_6_exactly_at_threshold(self):
        fs = _FakeFs()
        probe = _probe(duration=2.0, bitrate=55_800_000)
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, probe, fs,
        )
        # duration == MIN_COMPRESS_DURATION_S, not below, so it should pass
        assert c is not None

    def test_rule_7_intra_codec(self):
        """Intra codecs are always worth compressing."""
        fs = _FakeFs()
        for codec in ("mjpeg", "dv", "rawvideo", "prores", "ffv1", "mpeg2video"):
            probe = _probe(codec=codec, bitrate=1000)
            c, s = compress.evaluate_file(
                "D:/Archive/video.mp4", 100_000_000, probe, fs,
            )
            assert c is not None, f"{codec} should be a candidate"
            assert "intra" in c.reason

    def test_rule_8_high_bpp_qualifies(self):
        """Samsung S22 HEVC at 0.448 bpp >> threshold 0.10."""
        fs = _FakeFs()
        probe = _probe(codec="hevc", bitrate=55_800_000)
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 172_000_000, probe, fs,
        )
        assert c is not None
        assert "bpp" in c.reason

    def test_rule_8_h264_threshold(self):
        """h264 threshold is 0.15."""
        fs = _FakeFs()
        # bpp = 0.16 (just above 0.15)
        bitrate = int(0.16 * 1920 * 1080 * 30)
        probe = _probe(codec="h264", fps=30, bitrate=bitrate)
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, probe, fs,
        )
        assert c is not None

    def test_rule_9_low_bpp_rejected(self):
        """HEVC at 0.05 bpp -- below 0.10 threshold."""
        fs = _FakeFs()
        bitrate = int(0.05 * 1920 * 1080 * 30)
        probe = _probe(codec="hevc", fps=30, bitrate=bitrate)
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 10_000_000, probe, fs,
        )
        assert c is None
        assert "bpp" in s.reason
        assert "<=" in s.reason

    def test_custom_bpp_thresholds(self):
        """Custom thresholds override defaults."""
        fs = _FakeFs()
        bitrate = int(0.20 * 1920 * 1080 * 30)
        probe = _probe(codec="hevc", fps=30, bitrate=bitrate)
        # With high custom threshold, this should be skipped
        c, s = compress.evaluate_file(
            "D:/Archive/video.mp4", 100_000_000, probe, fs,
            bpp_thresholds={"hevc": 0.50, "default": 0.50},
        )
        assert c is None


# -- TestHasCompressedSuffix --------------------------------------------------

class TestHasCompressedSuffix:
    def test_basic(self):
        assert compress._has_compressed_suffix("D:/Archive/vid_compressed.mp4") is True

    def test_no_suffix(self):
        assert compress._has_compressed_suffix("D:/Archive/video.mp4") is False

    def test_suffix_in_dir_not_file(self):
        assert compress._has_compressed_suffix("D:/compressed/video.mp4") is False

    def test_no_extension(self):
        assert compress._has_compressed_suffix("D:/Archive/vid_compressed") is True


# -- TestEstimation -----------------------------------------------------------

class TestEstimateSize:
    def test_samsung_s22_x265(self):
        """Seed values (docs/technical/components/compression-mechanism.md): x265 bpp=0.101, source 172.1 MB HEVC 1080p60."""
        p = _probe(codec="hevc", width=1920, height=1080, fps=59.94,
                    bitrate=55_800_000, duration=25.76)
        est = compress.estimate_size(p, 172_100_000, 0.101)
        # Should be approximately 39 MB
        assert 35_000_000 < est < 45_000_000

    def test_clamped_to_90_percent(self):
        """When estimated size exceeds 90% of original, clamp."""
        p = _probe(width=320, height=240, fps=15, bitrate=50000, duration=5)
        est = compress.estimate_size(p, 10_000, 0.5)
        assert est <= 9000  # 90% of 10000

    def test_zero_dimensions(self):
        p = _probe(width=0, height=0, fps=0, bitrate=0, duration=0)
        est = compress.estimate_size(p, 100_000, 0.1)
        assert est == 100_000


class TestEstimateTime:
    def test_samsung_s22_x265(self):
        """Seed values (docs/technical/components/compression-mechanism.md): x265 at 20.9 Mpx/s, 1080p60, 25.76s."""
        p = _probe(width=1920, height=1080, fps=59.94, duration=25.76)
        est = compress.estimate_time(p, 20.9)
        # Should be approximately 153 seconds
        assert 140 < est < 170

    def test_zero_throughput(self):
        p = _probe()
        assert compress.estimate_time(p, 0.0) == 0.0


class TestEstimateAllProfiles:
    def test_with_seed_calibration(self):
        """Uses seed values when no calibration is provided."""
        p = _probe(width=1920, height=1080, fps=59.94,
                    bitrate=55_800_000, duration=25.76)
        estimates = compress.estimate_all_profiles(p, 172_100_000, calibration={})
        assert len(estimates) >= 2
        x265 = next(e for e in estimates if e.profile_name == "x265")
        assert not x265.calibrated
        assert x265.estimated_size > 0
        assert x265.estimated_seconds > 0

    def test_with_real_calibration(self):
        """Calibrated values override seeds."""
        p = _probe(width=1920, height=1080, fps=59.94,
                    bitrate=55_800_000, duration=25.76)
        cal = {"libx265": {"bpp": 0.08, "mpx_s": 25.0}}
        estimates = compress.estimate_all_profiles(p, 172_100_000, calibration=cal)
        x265 = next(e for e in estimates if e.profile_name == "x265")
        assert x265.calibrated
        # Lower bpp -> smaller estimate
        uncal_estimates = compress.estimate_all_profiles(p, 172_100_000, calibration={})
        x265_uncal = next(e for e in uncal_estimates if e.profile_name == "x265")
        assert x265.estimated_size < x265_uncal.estimated_size

    def test_x265_quality_tiers_produce_different_size_estimates(self):
        """CRF19 measured ~+35% size, CRF18 ~+56% vs CRF21 on real footage
        (docs/business/rules/compression.md) -- until per-profile
        calibration exists, the seed estimate must at least reflect that
        measured relationship instead of collapsing to one identical number
        for every x265 tier (all three share one SEED_CALIBRATION key,
        "libx265")."""
        p = _probe(width=1920, height=1080, fps=59.94,
                    bitrate=55_800_000, duration=25.76)
        estimates = compress.estimate_all_profiles(p, 172_100_000, calibration={})
        by_profile = {e.profile_name: e for e in estimates}
        assert by_profile["x265"].estimated_size < by_profile["x265_medium"].estimated_size
        assert by_profile["x265_medium"].estimated_size < by_profile["x265_high"].estimated_size

    def test_unknown_encoder_gives_zero(self):
        """An encoder with no seed and no calibration gives zeros."""
        p = _probe()
        custom_profiles = {"custom": {"encoder": "libfoo", "crf": 20, "preset": "slow"}}
        estimates = compress.estimate_all_profiles(p, 100_000_000,
                                                    calibration={},
                                                    profiles=custom_profiles)
        assert len(estimates) == 1
        assert estimates[0].estimated_size == 0
        assert not estimates[0].calibrated

    def test_calibration_keyed_by_device_key_is_found_for_a_hardware_profile(self):
        # docs/technical/components/compression-mechanism.md: calibration is now keyed by device_key, not
        # bare encoder name -- a hardware profile's estimate must still
        # find its own machine's measurement.
        p = _probe(width=1920, height=1080, fps=30.0, bitrate=20_000_000, duration=10.0)
        cal = {"hevc_nvenc@NVIDIA GeForce RTX 4060": {"bpp": 0.06, "mpx_s": 200.0}}
        profiles = {"hevc_nvenc": {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"}}
        estimates = compress.estimate_all_profiles(p, 500_000_000, calibration=cal, profiles=profiles)
        assert len(estimates) == 1
        assert estimates[0].calibrated
        assert estimates[0].estimated_size > 0

    def test_xpsnr_diff_none_when_either_side_unmeasured(self):
        p = _probe(width=1920, height=1080, fps=30.0, bitrate=20_000_000, duration=10.0)
        cal = {"hevc_nvenc@GPU0": {"bpp": 0.06, "mpx_s": 200.0, "xpsnr_y": 37.0}}
        profiles = {"hevc_nvenc": {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"}}
        # libx265 has no calibration entry here -- no baseline to diff against.
        estimates = compress.estimate_all_profiles(p, 500_000_000, calibration=cal, profiles=profiles)
        assert estimates[0].xpsnr_diff is None

    def test_xpsnr_diff_computed_against_libx265_baseline(self):
        p = _probe(width=1920, height=1080, fps=30.0, bitrate=20_000_000, duration=10.0)
        cal = {
            "libx265": {"bpp": 0.1, "mpx_s": 20.9, "xpsnr_y": 38.9},
            "hevc_nvenc@GPU0": {"bpp": 0.06, "mpx_s": 200.0, "xpsnr_y": 37.0},
        }
        profiles = {
            "x265": {"encoder": "libx265", "crf": 21, "preset": "slow"},
            "hevc_nvenc": {"encoder": "hevc_nvenc", "cq": 23, "preset": "p7"},
        }
        estimates = compress.estimate_all_profiles(p, 500_000_000, calibration=cal, profiles=profiles)
        by_profile = {e.profile_name: e for e in estimates}
        assert by_profile["x265"].xpsnr_diff == pytest.approx(0.0)
        assert by_profile["hevc_nvenc"].xpsnr_diff == pytest.approx(37.0 - 38.9)


class TestFindCalibrationForEncoder:
    def test_bare_key_hit(self):
        cal = {"libx265": {"bpp": 0.1}}
        assert compress._find_calibration_for_encoder("libx265", cal) == {"bpp": 0.1}

    def test_device_qualified_key_hit(self):
        cal = {"hevc_qsv@Intel(R) Iris(R) Xe Graphics": {"bpp": 0.2}}
        assert compress._find_calibration_for_encoder("hevc_qsv", cal) == {"bpp": 0.2}

    def test_no_match_returns_none(self):
        cal = {"libx265": {"bpp": 0.1}}
        assert compress._find_calibration_for_encoder("hevc_nvenc", cal) is None


# -- TestIsInExcludedZone ----------------------------------------------------

class TestIsInExcludedZone:
    def test_remove(self):
        assert compress._is_in_excluded_zone("D:/Archive/!REMOVE/batch/f.mp4") is True

    def test_moves(self):
        assert compress._is_in_excluded_zone("D:/Archive/!MOVES/batch/f.mp4") is True

    def test_dedup(self):
        assert compress._is_in_excluded_zone("D:/Archive/.dedup/f.mp4") is True

    def test_new_not_excluded(self):
        assert compress._is_in_excluded_zone("D:/Archive/!NEW/f.mp4") is False

    def test_regular_path(self):
        assert compress._is_in_excluded_zone("D:/Archive/#Home/2024/f.mp4") is False

    def test_case_insensitive(self):
        assert compress._is_in_excluded_zone("D:/Archive/!remove/f.mp4") is True


class TestLoadCompressConfig:
    """compress.load_compress_config() -- reads config.json's "compression"
    section, same forgiving per-field-fallback contract as
    keeper.load_keeper_config()."""

    def _write(self, tmp_path, text):
        path = tmp_path / "config.json"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_missing_path_returns_defaults(self):
        cfg = compress.load_compress_config(None)
        assert cfg.default_profile == "x265"
        assert cfg.default_source == config.NEW_DIR_NAME
        assert cfg.suffix == config.COMPRESSED_SUFFIX
        assert cfg.verify_xpsnr is True
        assert cfg.min_xpsnr == 38.0
        assert cfg.min_xpsnr_relaxed == 36.0
        assert cfg.min_xpsnr_relaxed < cfg.min_xpsnr  # a "relaxed" bar that isn't lower is a bug
        assert cfg.bpp_thresholds == config.DEFAULT_BPP_THRESHOLDS

    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = compress.load_compress_config(str(tmp_path / "nope.json"))
        assert cfg.default_profile == "x265"

    def test_loads_valid_compression_section(self, tmp_path):
        path = self._write(tmp_path, """
        {"compression": {
            "default_profile": "av1",
            "default_source": "!NEW",
            "suffix": "_small",
            "parallel_jobs": 2,
            "verify_xpsnr": false,
            "min_xpsnr": 35.5,
            "min_xpsnr_relaxed": 33.0,
            "bpp_thresholds": {"h264": 0.2},
            "profiles": {"av1": {"encoder": "libsvtav1", "crf": 28, "preset": "6"}}
        }}
        """)
        cfg = compress.load_compress_config(path)
        assert cfg.default_profile == "av1"
        assert cfg.suffix == "_small"
        assert cfg.parallel_jobs == 2
        assert cfg.verify_xpsnr is False
        assert cfg.min_xpsnr == 35.5
        assert cfg.min_xpsnr_relaxed == 33.0
        assert cfg.bpp_thresholds == {"h264": 0.2}
        assert cfg.profiles == {"av1": {"encoder": "libsvtav1", "crf": 28, "preset": "6"}}

    def test_malformed_json_falls_back_and_warns(self, tmp_path, capsys):
        path = self._write(tmp_path, "{not valid json")
        cfg = compress.load_compress_config(path)
        assert cfg.default_profile == "x265"
        assert "Warning" in capsys.readouterr().out

    def test_wrong_typed_field_falls_back_only_for_that_field(self, tmp_path, capsys):
        path = self._write(tmp_path, """
        {"compression": {"default_profile": "av1", "min_xpsnr": "not-a-number"}}
        """)
        cfg = compress.load_compress_config(path)
        assert cfg.default_profile == "av1"          # valid field kept
        assert cfg.min_xpsnr == 38.0                  # invalid field falls back
        assert "Warning" in capsys.readouterr().out

    def test_wrong_typed_min_xpsnr_relaxed_falls_back(self, tmp_path, capsys):
        path = self._write(tmp_path, """
        {"compression": {"min_xpsnr_relaxed": "not-a-number"}}
        """)
        cfg = compress.load_compress_config(path)
        assert cfg.min_xpsnr_relaxed == 36.0
        assert "Warning" in capsys.readouterr().out

    def test_no_compression_section_returns_defaults(self, tmp_path):
        path = self._write(tmp_path, '{"priority_dirs": ["#Home"]}')
        cfg = compress.load_compress_config(path)
        assert cfg.default_profile == "x265"

    def test_hardware_profiles_are_accepted_without_crf(self, tmp_path):
        # docs/technical/components/compression-mechanism.md: profile validation is generic on "encoder"
        # alone, so cq/global_quality-keyed profiles (no "crf" at all) must
        # load exactly like x265/av1's crf-keyed ones.
        path = self._write(tmp_path, """
        {"compression": {"profiles": {
            "hevc_qsv": {"encoder": "hevc_qsv", "global_quality": 25}
        }}}
        """)
        cfg = compress.load_compress_config(path)
        assert cfg.profiles == {"hevc_qsv": {"encoder": "hevc_qsv", "global_quality": 25}}


# ---------------------------------------------------------------------------
# resolve_device() / manifest_reason() -- docs/technical/components/compression-mechanism.md
# ---------------------------------------------------------------------------

def _enc(name, device, device_key, is_hardware=True):
    return hwdetect.EncoderInfo(
        name=name, display_name=name, device=device,
        is_hardware=is_hardware, device_key=device_key,
    )


_CPU_ENCODERS = (_enc("libx265", "", "libx265", is_hardware=False),)
_HW_ENCODERS = (
    _enc("hevc_nvenc", "0", "hevc_nvenc@NVIDIA GeForce RTX 4060"),
    _enc("hevc_nvenc", "1", "hevc_nvenc@NVIDIA GeForce RTX 3060"),
    _enc("hevc_qsv", "", "hevc_qsv@Intel(R) Iris(R) Xe Graphics"),
)


class TestResolveDevice:
    def test_cpu_encoder_no_device_key_ok_no_entry(self):
        ok, entry, error = compress.resolve_device(_CPU_ENCODERS, "libx265", None)
        assert ok is True
        assert entry is None
        assert error is None

    def test_cpu_encoder_with_device_key_rejected(self):
        ok, entry, error = compress.resolve_device(_CPU_ENCODERS, "libx265", "libx265")
        assert ok is False
        assert entry is None
        assert "CPU" in error

    def test_encoder_not_available_at_all_rejected(self):
        ok, entry, error = compress.resolve_device(_CPU_ENCODERS, "hevc_nvenc", None)
        assert ok is False
        assert entry is None
        assert "not available" in error

    def test_hardware_no_device_key_picks_first_device(self):
        ok, entry, error = compress.resolve_device(_HW_ENCODERS, "hevc_nvenc", None)
        assert ok is True
        assert entry.device_key == "hevc_nvenc@NVIDIA GeForce RTX 4060"
        assert error is None

    def test_hardware_matching_device_key_resolved(self):
        ok, entry, error = compress.resolve_device(
            _HW_ENCODERS, "hevc_nvenc", "hevc_nvenc@NVIDIA GeForce RTX 3060",
        )
        assert ok is True
        assert entry.device == "1"
        assert error is None

    def test_unknown_device_key_rejected(self):
        ok, entry, error = compress.resolve_device(
            _HW_ENCODERS, "hevc_nvenc", "hevc_nvenc@does-not-exist",
        )
        assert ok is False
        assert entry is None
        assert "unknown or mismatched" in error

    def test_device_key_belonging_to_a_different_encoder_rejected(self):
        # "hevc_qsv@..." exists in hw.encoders, but not for hevc_nvenc --
        # must reject, never silently pick a device from the wrong encoder.
        ok, entry, error = compress.resolve_device(
            _HW_ENCODERS, "hevc_nvenc", "hevc_qsv@Intel(R) Iris(R) Xe Graphics",
        )
        assert ok is False
        assert entry is None
        assert "unknown or mismatched" in error


class TestManifestReason:
    def test_cpu_profile_has_no_device_suffix(self):
        reason = compress.manifest_reason("x265", {"encoder": "libx265"}, "")
        assert reason == "libx265 x265"

    def test_hardware_profile_includes_ascii_device_name(self):
        reason = compress.manifest_reason(
            "hevc_nvenc", {"encoder": "hevc_nvenc"},
            "hevc_nvenc@NVIDIA GeForce RTX 4060",
        )
        assert reason == "hevc_nvenc hevc_nvenc NVIDIA GeForce RTX 4060"

    def test_non_ascii_device_name_is_stripped(self):
        reason = compress.manifest_reason(
            "hevc_qsv", {"encoder": "hevc_qsv"}, "hevc_qsv@Intel(R) Iris(R) Xe™ Graphics",
        )
        assert "™" not in reason
        assert all(ord(ch) < 128 for ch in reason)
