"""Tests for hwdetect.py -- binary resolution, encoder listing, init test.

Uses monkeypatching / mock subprocess for unit tests.  Integration tests
that call the real ffmpeg binary are in a separate class with skipif.
"""
import os
import pytest
from unittest.mock import patch, MagicMock

from tidy.compression import hwdetect
from tidy import config
from tidy import paths
from tidy.compression.probe import ToolBlockedError


# -- TestResolveFfmpegExe -----------------------------------------------------

class TestResolveFfmpegExe:
    def test_config_json_override(self, tmp_path):
        """Custom path in config.json takes priority."""
        ffmpeg = tmp_path / "custom_ffmpeg.exe"
        ffmpeg.touch()
        cfg = tmp_path / "config.json"
        # JSON requires forward slashes or escaped backslashes in paths.
        ffmpeg_json_path = str(ffmpeg).replace("\\", "/")
        cfg.write_text(
            f'{{"compression": {{"ffmpeg_path": "{ffmpeg_json_path}"}}}}',
            encoding="utf-8",
        )
        result = hwdetect._resolve_ffmpeg_exe(str(cfg))
        assert os.path.normpath(result) == os.path.normpath(str(ffmpeg))

    def test_config_json_missing_path_falls_through(self, tmp_path):
        """When config.json points to a nonexistent file, fall through."""
        cfg = tmp_path / "config.json"
        cfg.write_text('{"compression": {"ffmpeg_path": "Z:/nonexistent/ffmpeg.exe"}}',
                       encoding="utf-8")
        with patch.object(hwdetect, "_repo_root", return_value=str(tmp_path)):
            result = hwdetect._resolve_ffmpeg_exe(str(cfg))
        # Falls through to bundled, then PATH -- both may be None in CI
        # The key assertion is it didn't return the nonexistent path
        assert result != "Z:/nonexistent/ffmpeg.exe"

    def test_bundled_binary_found(self, tmp_path):
        """Bundled binary in bin/ffmpeg/ is found."""
        bin_dir = tmp_path / config.BUNDLED_FFMPEG_REL.replace("/", os.sep)
        bin_dir.mkdir(parents=True)
        ffmpeg = bin_dir / "ffmpeg.exe"
        ffmpeg.touch()
        with patch.object(hwdetect, "_repo_root", return_value=str(tmp_path)):
            result = hwdetect._resolve_ffmpeg_exe(None)
        assert os.path.normpath(result) == os.path.normpath(str(ffmpeg))

    @pytest.mark.skipif(
        not os.path.isfile(os.path.join(str(paths.DEDUP_DIR), config.BUNDLED_FFMPEG_REL, "ffmpeg.exe")),
        reason="bin/ffmpeg not fetched yet (tools/fetch-ffmpeg.ps1)",
    )
    def test_real_repo_bundled_binary_is_resolved_not_path_fallback(self):
        """Unpatched: _repo_root() is the .dedup repo root, so the fetched
        bin/ffmpeg/ffmpeg.exe is found even when PATH has no ffmpeg."""
        expected = os.path.join(
            str(paths.DEDUP_DIR), config.BUNDLED_FFMPEG_REL, "ffmpeg.exe"
        )
        assert hwdetect._repo_root() == str(paths.DEDUP_DIR)
        with patch("shutil.which", return_value=None):
            result = hwdetect._resolve_ffmpeg_exe(None)
        assert result == expected

    def test_path_fallback(self, tmp_path):
        """When no bundled binary, falls back to PATH."""
        with patch.object(hwdetect, "_repo_root", return_value=str(tmp_path)):
            with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                result = hwdetect._resolve_ffmpeg_exe(None)
        assert result == "/usr/bin/ffmpeg"

    def test_nothing_found(self, tmp_path):
        """Returns None when ffmpeg is nowhere."""
        with patch.object(hwdetect, "_repo_root", return_value=str(tmp_path)):
            with patch("shutil.which", return_value=None):
                result = hwdetect._resolve_ffmpeg_exe(None)
        assert result is None


# -- TestResolveFfprobeExe ----------------------------------------------------

class TestResolveFfprobeExe:
    def test_same_directory(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg.exe"
        ffprobe = tmp_path / "ffprobe.exe"
        ffmpeg.touch()
        ffprobe.touch()
        result = hwdetect._resolve_ffprobe_exe(str(ffmpeg))
        assert result == str(ffprobe)

    def test_fallback_to_path(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg.exe"
        ffmpeg.touch()
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            result = hwdetect._resolve_ffprobe_exe(str(ffmpeg))
        assert result == "/usr/bin/ffprobe"


# -- TestListEncoders ---------------------------------------------------------

class TestListEncoders:
    def test_parses_encoder_list(self):
        sample_output = """Encoders:
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V..... libsvtav1            SVT-AV1(Scalable Video Technology for AV1) encoder (codec av1)
 V..... hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 A..... aac                  AAC (Advanced Audio Coding) (codec aac)
"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = sample_output
        with patch("subprocess.run", return_value=mock_result):
            encoders = hwdetect._list_encoders("ffmpeg.exe")
        assert "libx265" in encoders
        assert "libsvtav1" in encoders
        assert "hevc_nvenc" in encoders
        assert "aac" in encoders  # audio encoders are listed too

    def test_empty_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            encoders = hwdetect._list_encoders("ffmpeg.exe")
        assert encoders == []


# -- TestTestEncoder ----------------------------------------------------------

class TestTestEncoder:
    def test_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert hwdetect._test_encoder("ffmpeg.exe", "libx265") is True

    def test_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            assert hwdetect._test_encoder("ffmpeg.exe", "hevc_nvenc") is False

    def test_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            assert hwdetect._test_encoder("ffmpeg.exe", "hevc_qsv") is False

    def test_uses_256x256_not_64x64_init_frame(self):
        # Regression guard for the bug docs/technical/components/compression-mechanism.md fixes: a 64x64 init
        # frame makes hevc_qsv fail to initialize (exit -40) on real Intel
        # hardware even though the encoder works fine at normal sizes -- the
        # init test must never shrink back under 256x256.
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            hwdetect._test_encoder("ffmpeg.exe", "hevc_qsv")
        cmd = mock_run.call_args[0][0]
        size_arg = next(a for a in cmd if a.startswith("testsrc="))
        assert "size=256x256" in size_arg
        assert "size=64x64" not in size_arg


# -- TestListNvencDevices ------------------------------------------------------

class TestListNvencDevices:
    def _mock_run(self, stderr: str, returncode: int = 1):
        mock_result = MagicMock()
        mock_result.returncode = returncode
        mock_result.stdout = ""
        mock_result.stderr = stderr
        return mock_result

    def test_no_gpu_lines_returns_empty(self):
        with patch("subprocess.run", return_value=self._mock_run("")):
            assert hwdetect._list_nvenc_devices("ffmpeg.exe") == []

    def test_one_gpu(self):
        stderr = "[ GPU #0 - < NVIDIA GeForce RTX 4060 > has Compute SM 8.9 ]\n"
        with patch("subprocess.run", return_value=self._mock_run(stderr)):
            devices = hwdetect._list_nvenc_devices("ffmpeg.exe")
        assert devices == [("0", "NVIDIA GeForce RTX 4060")]

    def test_two_gpus(self):
        stderr = (
            "[ GPU #0 - < NVIDIA GeForce RTX 4060 > has Compute SM 8.9 ]\n"
            "[ GPU #1 - < NVIDIA GeForce RTX 3060 > has Compute SM 8.6 ]\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(stderr)):
            devices = hwdetect._list_nvenc_devices("ffmpeg.exe")
        assert devices == [
            ("0", "NVIDIA GeForce RTX 4060"),
            ("1", "NVIDIA GeForce RTX 3060"),
        ]

    def test_garbage_output_returns_empty(self):
        stderr = "some unrelated ffmpeg error, no gpu here\n"
        with patch("subprocess.run", return_value=self._mock_run(stderr)):
            assert hwdetect._list_nvenc_devices("ffmpeg.exe") == []

    def test_timeout_returns_empty(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            assert hwdetect._list_nvenc_devices("ffmpeg.exe") == []


# -- TestDetectNvencVariant -----------------------------------------------------

class TestDetectNvencVariant:
    def test_two_gpus_yield_two_encoder_infos(self):
        gpu_list_stderr = (
            "[ GPU #0 - < NVIDIA GeForce RTX 4060 > has Compute SM 8.9 ]\n"
            "[ GPU #1 - < NVIDIA GeForce RTX 3060 > has Compute SM 8.6 ]\n"
        )

        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            if "list" in cmd:
                result.returncode = 1
                result.stderr = gpu_list_stderr
            else:
                result.returncode = 0
                result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            infos = hwdetect._detect_nvenc_variant(
                "ffmpeg.exe", "hevc_nvenc", {"hevc_nvenc"},
            )
        assert [i.device for i in infos] == ["0", "1"]
        assert infos[0].device_key == "hevc_nvenc@NVIDIA GeForce RTX 4060"
        assert infos[1].device_key == "hevc_nvenc@NVIDIA GeForce RTX 3060"
        assert all(i.is_hardware for i in infos)
        assert "NVIDIA GeForce RTX 4060" in infos[0].display_name

    def test_device_failing_its_own_init_test_is_dropped(self):
        gpu_list_stderr = (
            "[ GPU #0 - < NVIDIA GeForce RTX 4060 > has Compute SM 8.9 ]\n"
            "[ GPU #1 - < Old Unsupported Card > has Compute SM 5.0 ]\n"
        )

        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            if "list" in cmd:
                result.returncode = 1
                result.stderr = gpu_list_stderr
            elif "-gpu" in cmd and cmd[cmd.index("-gpu") + 1] == "1":
                result.returncode = 1  # device #1 fails its own init test
                result.stderr = ""
            else:
                result.returncode = 0
                result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            infos = hwdetect._detect_nvenc_variant(
                "ffmpeg.exe", "hevc_nvenc", {"hevc_nvenc"},
            )
        assert [i.device for i in infos] == ["0"]

    def test_unparseable_list_falls_back_to_device_0_when_it_passes(self):
        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            result.stderr = "garbage, no gpu lines"
            result.returncode = 0
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            infos = hwdetect._detect_nvenc_variant(
                "ffmpeg.exe", "hevc_nvenc", {"hevc_nvenc"},
            )
        assert len(infos) == 1
        assert infos[0].device == "0"
        assert infos[0].device_key == "hevc_nvenc@GPU 0"

    def test_unparseable_list_and_failing_device_0_yields_nothing(self):
        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stdout = ""
            result.stderr = "garbage"
            result.returncode = 1
            return result

        with patch("subprocess.run", side_effect=_fake_run):
            infos = hwdetect._detect_nvenc_variant(
                "ffmpeg.exe", "hevc_nvenc", {"hevc_nvenc"},
            )
        assert infos == []

    def test_encoder_not_listed_short_circuits(self):
        with patch("subprocess.run") as mock_run:
            infos = hwdetect._detect_nvenc_variant("ffmpeg.exe", "av1_nvenc", {"hevc_nvenc"})
        assert infos == []
        mock_run.assert_not_called()


# -- TestDetectQsvVariant --------------------------------------------------------

class TestDetectQsvVariant:
    def test_missing_card_name_still_yields_an_entry(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(hwdetect, "_detect_intel_gpu_name", return_value=None):
                infos = hwdetect._detect_qsv_variant("ffmpeg.exe", "hevc_qsv", {"hevc_qsv"})
        assert len(infos) == 1
        assert infos[0].device_key == "hevc_qsv"  # bare, like a CPU encoder
        assert infos[0].display_name == "HEVC / Quick Sync"

    def test_card_name_found_is_used_in_key_and_display(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(
                hwdetect, "_detect_intel_gpu_name",
                return_value="Intel(R) Iris(R) Xe Graphics",
            ):
                infos = hwdetect._detect_qsv_variant("ffmpeg.exe", "hevc_qsv", {"hevc_qsv"})
        assert infos[0].device_key == "hevc_qsv@Intel(R) Iris(R) Xe Graphics"
        assert infos[0].display_name == "HEVC / Quick Sync (Intel(R) Iris(R) Xe Graphics)"

    def test_failing_init_test_yields_nothing(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            infos = hwdetect._detect_qsv_variant("ffmpeg.exe", "hevc_qsv", {"hevc_qsv"})
        assert infos == []

    def test_encoder_not_listed_short_circuits(self):
        with patch("subprocess.run") as mock_run:
            infos = hwdetect._detect_qsv_variant("ffmpeg.exe", "av1_qsv", {"hevc_qsv"})
        assert infos == []
        mock_run.assert_not_called()


# -- TestMachineId ------------------------------------------------------------

class TestMachineId:
    def test_returns_stable_string(self):
        mid = hwdetect.get_machine_id()
        assert isinstance(mid, str)
        assert len(mid) > 0
        assert "/" in mid  # node/cpu format

    def test_deterministic(self):
        assert hwdetect.get_machine_id() == hwdetect.get_machine_id()


# -- TestDisplayName ----------------------------------------------------------

class TestDisplayName:
    def test_known_encoders(self):
        assert hwdetect._make_display_name("libx265") == "x265 (CPU)"
        assert hwdetect._make_display_name("libsvtav1") == "AV1 / SVT (CPU)"
        assert hwdetect._make_display_name("hevc_nvenc") == "HEVC / NVENC"

    def test_unknown_encoder(self):
        assert hwdetect._make_display_name("libfoo") == "libfoo"

    def test_with_device_name(self):
        assert (
            hwdetect._make_display_name("hevc_nvenc", "NVIDIA GeForce RTX 4060")
            == "HEVC / NVENC (NVIDIA GeForce RTX 4060)"
        )
        assert (
            hwdetect._make_display_name("hevc_qsv", "Intel(R) Iris(R) Xe Graphics")
            == "HEVC / Quick Sync (Intel(R) Iris(R) Xe Graphics)"
        )

    def test_no_device_name_omits_parens(self):
        assert hwdetect._make_display_name("hevc_qsv", None) == "HEVC / Quick Sync"


# -- TestDeviceKey --------------------------------------------------------------

class TestDeviceKey:
    def test_cpu_encoder_bare_name(self):
        assert hwdetect._device_key("libx265", None) == "libx265"

    def test_hardware_with_device_name(self):
        assert (
            hwdetect._device_key("hevc_nvenc", "NVIDIA GeForce RTX 4060")
            == "hevc_nvenc@NVIDIA GeForce RTX 4060"
        )

    def test_hardware_without_device_name_falls_back_to_bare(self):
        assert hwdetect._device_key("hevc_qsv", None) == "hevc_qsv"


# -- TestDetectRealFfmpeg -------------------------------------------------------
# Integration: real bundled binary, no mocks. Runs whenever ffmpeg is
# resolvable at all (bin/ffmpeg/ is fetched by the launchers -- see
# docs/technical/components/compression-mechanism.md; skips on a fresh clone without it).

_FFMPEG = hwdetect._resolve_ffmpeg_exe(None)
requires_ffmpeg = pytest.mark.skipif(not _FFMPEG, reason="ffmpeg not resolvable")


@requires_ffmpeg
class TestDetectRealFfmpeg:
    def test_detect_does_not_raise_and_returns_a_result(self):
        result = hwdetect.detect(None)
        assert result is not None
        assert result.ffmpeg_exe
        assert result.ffprobe_exe

    def test_any_detected_hardware_encoder_has_a_usable_device_key(self):
        # Calibration is stored under device_key, so an empty one would merge
        # unrelated devices' measurements. Holds for whatever hardware is present.
        result = hwdetect.detect(None)
        assert result is not None
        for enc in result.encoders:
            if enc.is_hardware:
                assert enc.device_key, f"{enc.name} has empty device_key"


# -- Smart App Control block (winerror 4551) ----------------------------------

class TestDetectToolBlocked:
    @staticmethod
    def _run_blocking(blocked_exe):
        def fake_run(cmd, *_a, **_k):
            if cmd[0] == blocked_exe:
                raise OSError(None, "blocked", None, config.WINERROR_APP_CONTROL_BLOCKED)
            return MagicMock(returncode=0, stdout="ffmpeg version test\n", stderr="")
        return fake_run

    def _patched(self, monkeypatch, blocked_exe):
        monkeypatch.setattr(hwdetect, "_resolve_ffmpeg_exe", lambda *_a: "ffmpeg.exe")
        monkeypatch.setattr(hwdetect, "_resolve_ffprobe_exe", lambda *_a: "ffprobe.exe")
        monkeypatch.setattr(hwdetect.subprocess, "run", self._run_blocking(blocked_exe))

    @pytest.mark.skipif(os.name != "nt", reason="OSError.winerror exists only on Windows")
    def test_ffmpeg_blocked_raises(self, monkeypatch):
        self._patched(monkeypatch, "ffmpeg.exe")
        with pytest.raises(ToolBlockedError) as info:
            hwdetect.detect(None)
        assert "ffmpeg.exe" in str(info.value)

    @pytest.mark.skipif(os.name != "nt", reason="OSError.winerror exists only on Windows")
    def test_ffprobe_only_blocked_raises(self, monkeypatch):
        self._patched(monkeypatch, "ffprobe.exe")
        with pytest.raises(ToolBlockedError) as info:
            hwdetect.detect(None)
        assert "ffprobe.exe" in str(info.value)

    def test_not_found_still_returns_none(self, monkeypatch):
        monkeypatch.setattr(hwdetect, "_resolve_ffmpeg_exe", lambda *_a: None)
        assert hwdetect.detect(None) is None

    def test_other_launch_failure_is_not_a_block(self, monkeypatch):
        def fake_run(cmd, *_a, **_k):
            raise OSError(2, "no such file")

        monkeypatch.setattr(hwdetect.subprocess, "run", fake_run)
        assert hwdetect._get_ffmpeg_version("ffmpeg.exe") == "unknown"
        hwdetect._check_launchable("ffprobe.exe")
