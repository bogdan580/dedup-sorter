"""CLI wiring of --series."""

import os
from tidy import config
from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from tidy.analysis import series
from fakes import FakeFileSystem


class TestSeriesCliWiring:
    """--series scans (like the default report path) but skips
    tiers.find_duplicates()/keeper selection entirely, handing the
    candidate list straight to series.find_series() -- then writes a CSV
    and prints a summary instead of the exact-duplicate report."""

    def _make_fs_with_a_burst_at(self, root):
        import io as _io

        from PIL import ExifTags, Image, ImageDraw

        def base_pattern():
            image = Image.new("RGB", (320, 240), (30, 30, 30))
            draw = ImageDraw.Draw(image)
            draw.ellipse([64, 48, 256, 192], fill=(220, 200, 50))
            return image

        def jpeg_bytes(image, when):
            # Real Exif SubIFD placement, not top-level IFD0 -- see
            # test_timeline.py's _jpeg_bytes_with_exif() docstring.
            buf = _io.BytesIO()
            exif = Image.Exif()
            exif[ExifTags.IFD.Exif] = {36867: when}
            image.save(buf, "JPEG", exif=exif.tobytes())
            return buf.getvalue()

        base = base_pattern()
        small = base.resize((80, 60))

        fs = FakeFileSystem()
        fs.add_dir(root)
        fs.add_file(f"{root}/full.jpg", jpeg_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file(f"{root}/small.jpg", jpeg_bytes(small, "2021:01:01 10:00:02"))
        return fs

    def test_series_writes_csv_and_prints_summary(self, tmp_path, capsys):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_a_burst_at(root)
        series_report_path = str(tmp_path / "raport_serie.csv")

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--series",
            "--series-report", series_report_path,
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert os.path.isfile(series_report_path)
        captured = capsys.readouterr()
        assert "Series and similar photos" in captured.out
        assert "Groups: 1" in captured.out

    def test_series_rescan_forces_recompute(self, tmp_path, monkeypatch):
        # Regression: --rescan used to be silently ignored under --series
        # (the branch always used a plain, freshness-respecting Cache).
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_a_burst_at(root)
        captured = {}

        def fake_find_series(fs_arg, candidates, cache, **kw):
            captured["kw"] = kw
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--series", "--rescan",
            "--series-report", str(tmp_path / "raport_serie.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["force_recompute"] is True

    def test_series_workers_flag_is_forwarded(self, tmp_path, monkeypatch):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_a_burst_at(root)
        captured = {}

        def fake_find_series(fs_arg, candidates, cache, **kw):
            captured["kw"] = kw
            return series.SeriesResult()

        monkeypatch.setattr(series, "find_series", fake_find_series)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--series", "--workers", "2",
            "--series-report", str(tmp_path / "raport_serie.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert captured["kw"]["max_workers"] == 2

    def test_series_never_calls_move_itself(self, tmp_path):
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_a_burst_at(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "-y", "--series",
            "--series-report", str(tmp_path / "raport_serie.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        cli_main.run(args, fs)

        assert fs.move_calls == []

    def test_series_does_not_run_exact_duplicate_confirmation_prompt(self, tmp_path):
        # --series must not go through tiers.find_duplicates()'s
        # size-estimate confirm() gate -- omitting -y here would otherwise
        # hang on input() if the two files' sizes happened to collide, or
        # simply prove the exact-dup pipeline never runs for --series.
        root = str(tmp_path / "ARCHIVE").replace("\\", "/")
        fs = self._make_fs_with_a_burst_at(root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", root, "--series",  # no -y
            "--series-report", str(tmp_path / "raport_serie.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0  # would hang/raise on a real input() call if reached

    def test_series_refuses_remove_root(self, tmp_path):
        fs = FakeFileSystem()
        remove_root = str(tmp_path / "ARCHIVE" / config.REMOVE_DIR_NAME).replace("\\", "/")
        fs.add_dir(remove_root)

        parser = cli_args.build_arg_parser()
        args = parser.parse_args([
            "--roots", remove_root, "-y", "--series",
            "--series-report", str(tmp_path / "raport_serie.csv"),
            "--report", str(tmp_path / "raport.csv"),
            "--db", str(tmp_path / "cache.sqlite3"),
        ])

        assert cli_main.run(args, fs) == 1
