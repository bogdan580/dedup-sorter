"""Content-characterization tests for tools/fetch-ffmpeg.ps1 and its callers
(run.ps1, run.sh), same shape as test_run_cmd.py: no network, no execution --
only the text contract. The pinned hashes it verifies come from
bin/ffmpeg/VERSION.txt, so that file's machine-readable fields are checked too.
Rules: docs/technical/components/compression-mechanism.md
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "tools" / "fetch-ffmpeg.ps1"
_VERSION = _REPO / "bin" / "ffmpeg" / "VERSION.txt"
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/"


def _field(name: str) -> str:
    match = re.search(r"^\s*" + re.escape(name) + r":\s*(\S+)\s*$", _VERSION.read_text(encoding="utf-8"), re.M)
    assert match, name
    return match.group(1)


class TestVersionTxt:
    def test_pinned_and_fallback_urls_are_btbn_releases(self):
        assert _field("Pinned URL").startswith(_BTBN)
        assert _field("Pinned URL").endswith("-win64-gpl.zip")
        assert _field("Fallback URL") == _BTBN + "latest/ffmpeg-master-latest-win64-gpl.zip"

    def test_pinned_url_is_not_the_moving_latest_tag(self):
        assert "/latest/" not in _field("Pinned URL")

    def test_has_zip_and_both_binary_sha256(self):
        text = _VERSION.read_text(encoding="utf-8")
        assert re.fullmatch(r"[0-9a-f]{64}", _field("Pinned zip SHA-256"))
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            assert re.search(r"^\s*" + re.escape(name) + r"\s+[0-9a-f]{64}\s*$", text, re.M), name


class TestFetchScript:
    def test_file_exists_and_is_ascii(self):
        # Windows PowerShell 5.1 reads a BOM-less script as ANSI.
        _SCRIPT.read_bytes().decode("ascii")

    def test_reads_pins_from_version_txt(self):
        content = _SCRIPT.read_text(encoding="utf-8")
        for field in ("Pinned URL", "Pinned zip SHA-256", "Fallback URL"):
            assert f'"{field}"' in content
        assert "VERSION.txt" in content

    def test_verifies_sha256(self):
        content = _SCRIPT.read_text(encoding="utf-8")
        assert "Get-FileHash" in content
        assert "-Algorithm SHA256" in content

    def test_fallback_is_checked_by_required_encoders(self):
        content = _SCRIPT.read_text(encoding="utf-8")
        assert '"libx265", "libsvtav1"' in content
        assert "-encoders" in content
        assert "INSTALLED.txt" in content

    def test_downloads_only_from_btbn(self):
        content = _SCRIPT.read_text(encoding="utf-8")
        assert f'$AllowedUrlPrefix = "{_BTBN}"' in content
        assert "StartsWith($AllowedUrlPrefix)" in content
        urls = re.findall(r"https?://\S+", content)
        assert urls == [_BTBN + '"']

    def test_exit_codes(self):
        content = _SCRIPT.read_text(encoding="utf-8")
        assert "exit 0" in content
        assert "exit 2" in content


class TestLaunchersCallFetch:
    def test_run_ps1_fetches_before_review_and_only_warns(self):
        content = (_REPO / "run.ps1").read_text(encoding="utf-8")
        fetch = content.index("tools\\fetch-ffmpeg.ps1")
        assert fetch < content.index("python dedup.py")
        assert "$LASTEXITCODE -ne 0" in content

    def test_run_sh_fetches_before_review_and_is_windows_only(self):
        content = (_REPO / "run.sh").read_text(encoding="utf-8")
        fetch = content.index("tools/fetch-ffmpeg.ps1")
        assert fetch < content.index("$PYTHON_CMD dedup.py --review")
        assert "powershell.exe" in content
        assert "MINGW*|MSYS*" in content

    def test_launchers_never_pass_apply(self):
        for name in ("run.ps1", "run.sh"):
            assert "--apply" not in (_REPO / name).read_text(encoding="utf-8")
