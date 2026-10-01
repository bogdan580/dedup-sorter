"""Content-characterization test for run.cmd (double-click launcher for the
review UI). Same shape and rationale as test_skanuj_cmd.py: a .cmd file can't
meaningfully be executed under pytest on this stack, so this test only asserts
on its text content -- it must exist, launch dedup.py --review with the fixed
port/worker settings, stay portable (%~dp0, no hardcoded personal path), guard
the archive's !NEW directory before starting, and never silently pass --apply.
"""
import re
from pathlib import Path

_RUN_CMD_PATH = Path(__file__).resolve().parent.parent / "run.cmd"


class TestRunCmd:
    def test_file_exists(self):
        assert _RUN_CMD_PATH.is_file()

    def test_launches_review_with_fixed_port_and_workers(self):
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        assert "dedup.py --review --port 8765 --workers 8" in content

    def test_forwards_extra_arguments(self):
        # Trailing %* so "run.cmd --no-browser" still reaches dedup.py.
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        assert "--workers 8 -y %*" in content

    def test_uses_relative_dp0_no_hardcoded_path(self):
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        assert "%~dp0" in content
        assert re.search(r"[A-Za-z]:\\", content) is None

    def test_verifies_new_dir_before_launching(self):
        # The !NEW check must both exist and abort (exit /b 1) rather than
        # falling through into the scan.
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        assert 'if not exist "%NEW_DIR%\\"' in content
        assert "exit /b 1" in content
        assert content.index("NEW_DIR") < content.index("python dedup.py --review")

    def test_ensures_ffmpeg_before_launching(self):
        # The fetch must run before the review starts, and its failure must
        # only warn -- no "exit /b" in the fetch's own error branch.
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        fetch = content.index("tools\\fetch-ffmpeg.ps1")
        assert content.index("python dedup.py --review") > fetch
        assert "-ExecutionPolicy Bypass" in content
        after_fetch = content[fetch:content.index("set \"NEW_DIR=")]
        assert "exit /b" not in after_fetch

    def test_does_not_pass_apply_flag(self):
        content = _RUN_CMD_PATH.read_text(encoding="utf-8")
        assert "--apply" not in content
