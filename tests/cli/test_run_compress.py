"""CLI wiring of --compress."""

import pytest
from tidy.moves import applier
from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from tidy.compression import encoder
from tidy.compression import hwdetect
from tidy.moves import manifest
from tidy.compression import probe
from fakes import FakeFileSystem
from tidy.compression.probe import ProbeResult, ToolBlockedError


class TestCompressCliWiring:
    """--compress/--compress-remove-originals wiring in run()/_run_compress().
    Real ffmpeg/ffprobe calls (hwdetect.detect, probe.probe_file_cached,
    encoder.run_encode, encoder.verify_output) are monkeypatched throughout:
    these tests exercise tidy/cli's own orchestration contract (plan-only
    listing, and -- the load-bearing one -- that --compress-remove-originals
    only ever reaches apply_compress_removals() for entries
    apply_compressions() itself marked status=done), not ffmpeg/encoder
    correctness, which tests/test_encoder.py already covers against real
    binaries."""

    def _fake_hw(self):
        return hwdetect.HwDetectResult(
            ffmpeg_exe="ffmpeg.exe", ffprobe_exe="ffprobe.exe",
            ffmpeg_version="test", machine_id="test-machine",
            encoders=(hwdetect.EncoderInfo(
                name="libx265", display_name="x265 (CPU)", device="", is_hardware=False,
            ),),
        )

    def _fake_probe(self):
        return ProbeResult(
            codec="hevc", width=1920, height=1080, fps=30.0, bitrate=50_000_000,
            duration=10.0, has_audio=False, rotation=0,
        )

    def test_compress_conflicts_with_other_modes_rejected_at_run_time(self, tmp_path):
        # --compress lives outside argparse's apply_group (it must combine
        # with --apply), so this conflict is rejected by run() itself
        # rather than by argparse -- unlike e.g. --series --apply.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--roots", root, "-y", "--compress", "--series"])

        assert cli_main.run(args, fs) == 1

    def test_plan_only_lists_candidate_without_apply_flag(self, tmp_path, monkeypatch, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/clip.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)

        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: self._fake_hw())
        monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: self._fake_probe())

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--compress",
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert fs.move_calls == []  # plan-only: never touches the archive
        captured = capsys.readouterr()
        assert "Compression suggestion" in captured.out
        assert "clip.mp4" in captured.out

    def test_apply_with_remove_originals_only_removes_confirmed_done_entries(
        self, tmp_path, monkeypatch
    ):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)
        fs.add_file(f"{root}/b.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=2)

        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: self._fake_hw())
        monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: self._fake_probe())
        monkeypatch.setattr(
            encoder, "run_encode",
            lambda cmd, **_k: encoder.EncodeResult(success=True, staging_path=cmd[-1]),
        )
        monkeypatch.setattr(
            encoder, "verify_output", lambda *_a, **_k: encoder.VerifyResult(ok=True, reason="ok"),
        )

        # apply_compressions() is stubbed to return one done + one failed
        # entry regardless of the plans it was handed -- isolating exactly
        # the contract under test: does _run_compress() ever let a
        # non-done entry reach apply_compress_removals()?
        def fake_apply_compressions(_fs, _root, plans, manifest_path=None, **_k):
            entries = (
                applier.ApplyEntry(
                    src_abs=f"{root}/a.mp4", dst_abs=f"{root}/a_compressed.mp4",
                    status=manifest.STATUS_DONE,
                ),
                applier.ApplyEntry(
                    src_abs=f"{root}/b.mp4", dst_abs="",
                    status=manifest.STATUS_FAILED, error="simulated move failure",
                ),
            )
            return applier.ApplyResult(
                moved=1, failed=1, batch_dir=f"{root}/!MOVES/x", manifest_path="unused", entries=entries,
            )

        captured_removals = {}

        def fake_apply_compress_removals(_fs, _root, removals, manifest_path=None, **_k):
            captured_removals["removals"] = list(removals)
            return applier.ApplyResult(
                moved=len(removals), failed=0, batch_dir=f"{root}/!REMOVE/x",
                manifest_path="unused2", entries=(),
            )

        monkeypatch.setattr(applier, "apply_compressions", fake_apply_compressions)
        monkeypatch.setattr(applier, "apply_compress_removals", fake_apply_compress_removals)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--compress", "--apply", "--compress-remove-originals",
            "--archive-root", root, "--db", str(tmp_path / "cache.sqlite3"),
            "--compress-staging-dir", str(tmp_path / "staging"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert len(captured_removals["removals"]) == 1
        assert captured_removals["removals"][0].original_abs == f"{root}/a.mp4"

    def test_apply_without_remove_originals_never_calls_apply_compress_removals(
        self, tmp_path, monkeypatch
    ):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)

        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: self._fake_hw())
        monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: self._fake_probe())
        monkeypatch.setattr(
            encoder, "run_encode",
            lambda cmd, **_k: encoder.EncodeResult(success=True, staging_path=cmd[-1]),
        )
        monkeypatch.setattr(
            encoder, "verify_output", lambda *_a, **_k: encoder.VerifyResult(ok=True, reason="ok"),
        )

        def fake_apply_compressions(_fs, _root, plans, manifest_path=None, **_k):
            entries = (
                applier.ApplyEntry(
                    src_abs=f"{root}/a.mp4", dst_abs=f"{root}/a_compressed.mp4",
                    status=manifest.STATUS_DONE,
                ),
            )
            return applier.ApplyResult(
                moved=1, failed=0, batch_dir=f"{root}/!MOVES/x", manifest_path="unused", entries=entries,
            )

        def boom(*_a, **_k):
            raise AssertionError(
                "apply_compress_removals() must not be called without --compress-remove-originals"
            )

        monkeypatch.setattr(applier, "apply_compressions", fake_apply_compressions)
        monkeypatch.setattr(applier, "apply_compress_removals", boom)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--compress", "--apply",
            "--archive-root", root, "--db", str(tmp_path / "cache.sqlite3"),
            "--compress-staging-dir", str(tmp_path / "staging"),
        ])

        exit_code = cli_main.run(args, fs)
        assert exit_code == 0

    def _fake_hw_nvenc_two_devices(self):
        return hwdetect.HwDetectResult(
            ffmpeg_exe="ffmpeg.exe", ffprobe_exe="ffprobe.exe",
            ffmpeg_version="test", machine_id="test-machine",
            encoders=(
                hwdetect.EncoderInfo(
                    name="hevc_nvenc", display_name="HEVC / NVENC (GPU0)", device="0",
                    is_hardware=True, device_key="hevc_nvenc@GPU0",
                ),
                hwdetect.EncoderInfo(
                    name="hevc_nvenc", display_name="HEVC / NVENC (GPU1)", device="1",
                    is_hardware=True, device_key="hevc_nvenc@GPU1",
                ),
            ),
        )

    def test_compress_device_index_selects_matching_device(self, tmp_path, monkeypatch):
        # docs/technical/components/compression-mechanism.md: --compress-device is an index into THIS
        # encoder's own detected devices, resolved via hw.encoders alone.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)

        cfg_path = tmp_path / "keeper_config.json"
        cfg_path.write_text(
            '{"compression": {"default_profile": "hevc_nvenc"}}', encoding="utf-8",
        )

        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: self._fake_hw_nvenc_two_devices())
        monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: self._fake_probe())

        captured_devices = []

        def fake_build_command(ffmpeg_exe, src, dst, profile, probe_result, device=""):
            captured_devices.append(device)
            return ["ffmpeg", "-i", src, dst]

        monkeypatch.setattr(encoder, "build_command", fake_build_command)
        monkeypatch.setattr(
            encoder, "run_encode",
            lambda cmd, **_k: encoder.EncodeResult(success=True, staging_path=cmd[-1]),
        )
        monkeypatch.setattr(
            encoder, "verify_output", lambda *_a, **_k: encoder.VerifyResult(ok=True, reason="ok"),
        )

        captured_plans = {}

        def fake_apply_compressions(_fs, _root, plans, manifest_path=None, **_k):
            captured_plans["plans"] = list(plans)
            entries = tuple(
                applier.ApplyEntry(src_abs=p.original_abs, dst_abs=p.dst_abs, status=manifest.STATUS_DONE)
                for p in plans
            )
            return applier.ApplyResult(
                moved=len(plans), failed=0, batch_dir=f"{root}/!MOVES/x",
                manifest_path="unused", entries=entries,
            )

        monkeypatch.setattr(applier, "apply_compressions", fake_apply_compressions)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--compress", "--apply", "--compress-device", "1",
            "--keeper-config", str(cfg_path),
            "--archive-root", root, "--db", str(tmp_path / "cache.sqlite3"),
            "--compress-staging-dir", str(tmp_path / "staging"),
        ])

        exit_code = cli_main.run(args, fs)
        assert exit_code == 0
        assert captured_devices == ["1"]
        assert "GPU1" in captured_plans["plans"][0].reason
        assert "GPU0" not in captured_plans["plans"][0].reason

    def test_compress_device_out_of_range_is_rejected_before_encoding(self, tmp_path, monkeypatch, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/a.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)

        cfg_path = tmp_path / "keeper_config.json"
        cfg_path.write_text(
            '{"compression": {"default_profile": "hevc_nvenc"}}', encoding="utf-8",
        )

        monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: self._fake_hw_nvenc_two_devices())
        monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: self._fake_probe())

        def boom(*_a, **_k):
            raise AssertionError("encoding must not start for an out-of-range device index")

        monkeypatch.setattr(encoder, "build_command", boom)
        monkeypatch.setattr(encoder, "run_encode", boom)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--compress", "--apply", "--compress-device", "5",
            "--keeper-config", str(cfg_path),
            "--archive-root", root, "--db", str(tmp_path / "cache.sqlite3"),
            "--compress-staging-dir", str(tmp_path / "staging"),
        ])

        exit_code = cli_main.run(args, fs)
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "Error" in out
        assert out.isascii()


class TestCompressToolBlocked:
    """A Smart App Control block aborts --compress with one ASCII line."""

    def _run(self, tmp_path, monkeypatch, fail_stage):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/clip.mp4", b"x" * 100, size=100, mtime_ns=1, volume_id=1, file_index=1)
        blocked = ToolBlockedError(
            "ffmpeg/ffprobe blocked by Smart App Control (WinError 4551): "
            "D:\\\u041c\u043e\u0439\\ffmpeg.exe"
        )

        def raise_blocked(*_a, **_k):
            raise blocked

        wiring = TestCompressCliWiring()
        hw = wiring._fake_hw()
        probe_result = wiring._fake_probe()
        if fail_stage == "detect":
            monkeypatch.setattr(hwdetect, "detect", raise_blocked)
        else:
            monkeypatch.setattr(hwdetect, "detect", lambda *_a, **_k: hw)
        if fail_stage == "probe":
            monkeypatch.setattr(probe, "probe_file_cached", raise_blocked)
        else:
            monkeypatch.setattr(probe, "probe_file_cached", lambda *_a, **_k: probe_result)
        if fail_stage == "encode":
            monkeypatch.setattr(encoder, "run_encode", raise_blocked)
        if fail_stage == "verify":
            monkeypatch.setattr(
                encoder, "run_encode",
                lambda cmd, **_k: encoder.EncodeResult(success=True, staging_path=cmd[-1]),
            )
            monkeypatch.setattr(encoder, "verify_output", raise_blocked)

        args = cli_args.build_arg_parser().parse_args([
            "--roots", root, "-y", "--compress", "--apply",
            "--db", str(tmp_path / "cache.sqlite3"),
            "--compress-staging-dir", str(tmp_path / "staging"),
        ])
        return cli_main.run(args, fs), fs

    @pytest.mark.parametrize("stage", ["detect", "probe", "encode", "verify"])
    def test_blocked_stage_returns_1_with_ascii_message(
        self, tmp_path, monkeypatch, capsys, stage
    ):
        exit_code, fs = self._run(tmp_path, monkeypatch, stage)

        assert exit_code == 1
        out = capsys.readouterr().out
        assert out.startswith("Error: ")
        assert "Smart App Control" in out
        out.encode("ascii")
        assert fs.move_calls == []
