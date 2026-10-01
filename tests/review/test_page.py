import shutil

import pytest

from tidy.review import page


def _copy_static(tmp_path, crlf=False):
    dst = tmp_path / "static"
    shutil.copytree(page.STATIC_DIR, dst)
    if crlf:
        for f in dst.rglob("*"):
            if f.is_file():
                f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    return dst


class TestPageAssembly:
    def test_all_markers_resolved(self):
        assert "@inline" not in page.INDEX_HTML

    def test_token_placeholder_appears_once(self):
        assert page.INDEX_HTML.count("__DEDUP_TOKEN__") == 1

    def test_inlined_not_external(self):
        assert "<style>" in page.INDEX_HTML
        assert "<script src" not in page.INDEX_HTML

    def test_crlf_input_normalizes(self, tmp_path):
        out = page.assemble(_copy_static(tmp_path, crlf=True))
        assert "\r" not in out
        assert out == page.INDEX_HTML

    def test_unknown_marker_raises(self, tmp_path):
        d = _copy_static(tmp_path)
        idx = d / "index.html"
        idx.write_text(idx.read_text(encoding="utf-8").replace(
            "//@inline js/main.js", "//@inline js/evil.js"), encoding="utf-8")
        with pytest.raises(ValueError):
            page.assemble(d)

    def test_missing_file_raises(self, tmp_path):
        d = _copy_static(tmp_path)
        (d / "js" / "main.js").unlink()
        with pytest.raises(FileNotFoundError):
            page.assemble(d)
