"""Tests for manifest.py: the append-only manifest.csv event log that is
the engine's sole vehicle of reversibility.

No FileSystem/FakeFileSystem here -- manifest.py writes its own bookkeeping
file directly with plain open()/os.fsync(), exactly like report.py's
write_csv(), never through the `fs` seam (the seam is for archive files
being moved, not for the tool's own manifest file).
"""
import csv
import os

import pytest

from tidy import config
from tidy.moves import manifest


def _row(**overrides):
    defaults = dict(
        op=manifest.OP_REMOVE,
        src_abs="D:/ARCHIVE/!NEW/Camera/img.jpg",
        dst_rel="!NEW/Camera/img.jpg",
        size=1234,
        mtime=1_700_000_000_000_000_000,
        hash="deadbeef",
        group_id="1",
        keeper_abs="D:/ARCHIVE/#Travel/2023 Mountains/img.jpg",
        reason="rule-0",
        ts="2026-07-26T10:00:00",
        tool_version=config.TOOL_VERSION,
        status=manifest.STATUS_PLANNED,
        error="",
    )
    defaults.update(overrides)
    return manifest.ManifestRow(**defaults)


class TestAppendPlanned:
    def test_creates_file_with_bom_and_header(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row())

        raw = (tmp_path / "manifest.csv").read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=config.CSV_DELIMITER)
            header = next(reader)
        assert header == list(manifest.MANIFEST_COLUMNS)

    def test_row_is_readable_back(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row())

        rows = manifest.read_rows(path)
        assert len(rows) == 1
        assert rows[0].status == manifest.STATUS_PLANNED
        assert rows[0].src_abs == "D:/ARCHIVE/!NEW/Camera/img.jpg"
        assert rows[0].size == 1234
        assert rows[0].group_id == "1"

    def test_fsyncs_after_write(self, tmp_path, monkeypatch):
        path = str(tmp_path / "manifest.csv")
        calls = []
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
        manifest.append_planned(path, _row())
        assert len(calls) >= 1

    def test_second_planned_call_appends_not_overwrites(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row(src_abs="A"))
        manifest.append_planned(path, _row(src_abs="B"))

        rows = manifest.read_rows(path)
        assert [r.src_abs for r in rows] == ["A", "B"]


class TestAppendConfirmation:
    def test_appends_second_row_for_same_src_abs(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row())
        manifest.append_confirmation(
            path, _row(status=manifest.STATUS_DONE, ts="2026-07-26T10:00:05")
        )

        rows = manifest.read_rows(path)
        assert len(rows) == 2
        assert rows[0].status == manifest.STATUS_PLANNED
        assert rows[1].status == manifest.STATUS_DONE

    def test_failed_confirmation_records_error_message(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row())
        manifest.append_confirmation(
            path, _row(status=manifest.STATUS_FAILED, error="[WinError 32] plik uzywany")
        )

        rows = manifest.read_rows(path)
        assert rows[-1].status == manifest.STATUS_FAILED
        assert "plik uzywany" in rows[-1].error


class TestResolveFinalRows:
    def test_last_row_wins_per_src_abs(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row(src_abs="A"))
        manifest.append_planned(path, _row(src_abs="B"))
        manifest.append_confirmation(path, _row(src_abs="A", status=manifest.STATUS_DONE))

        rows = manifest.read_rows(path)
        final = manifest.resolve_final_rows(rows)

        assert final["A"].status == manifest.STATUS_DONE
        assert final["B"].status == manifest.STATUS_PLANNED

    def test_empty_manifest_resolves_to_empty_dict(self):
        assert manifest.resolve_final_rows([]) == {}


class TestPathFieldsRoundTripExactly:
    """src_abs/dst_rel must round-trip byte-exact through read_rows() (they
    drive real fs.move()/fs.exists() calls during --restore/--verify-
    manifest) -- but, per manifest.py's module docstring, they are now
    ALSO guarded on-disk against CSV/formula injection the same way
    report.py's raport.csv guards equivalent path fields (manifest.csv
    sits in the same folder and could just as easily be opened directly in
    Excel). The escape-then-unescape round trip must be transparent to
    every caller of read_rows()."""

    def test_src_abs_starting_with_equals_round_trips_to_original_value(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        weird_src = "D:/ARCHIVE/=weird/=file.jpg"
        manifest.append_planned(path, _row(src_abs=weird_src, dst_rel="=weird/=file.jpg"))

        rows = manifest.read_rows(path)
        assert rows[0].src_abs == weird_src
        assert rows[0].dst_rel == "=weird/=file.jpg"

    def test_src_abs_starting_with_equals_is_guarded_on_disk(self, tmp_path):
        # The raw CSV bytes must carry report.py's leading-quote guard --
        # proving a user opening manifest.csv directly in Excel is
        # protected, even though read_rows() transparently strips it again.
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row(src_abs="=cmd|'/c calc'!A1.jpg", dst_rel="x.jpg"))

        raw = (tmp_path / "manifest.csv").read_text(encoding="utf-8-sig")
        assert "'=cmd|'/c calc'!A1.jpg" in raw

    def test_cyrillic_paths_round_trip(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        src = "C:/Photo Archive/#Місто/2019 Місто-Море/фото.jpg"
        manifest.append_planned(path, _row(src_abs=src, dst_rel="#Місто/2019 Місто-Море/фото.jpg"))

        rows = manifest.read_rows(path)
        assert rows[0].src_abs == src
        assert rows[0].dst_rel == "#Місто/2019 Місто-Море/фото.jpg"


class TestReadRowsOnMissingOrEmptyFile:
    def test_read_rows_on_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(OSError):
            manifest.read_rows(str(tmp_path / "nope.csv"))


class TestReadRowsTruncatedLastLine:
    """A crash mid-write of a row's own bytes (distinct from a kill
    that hits the process *between* rows) can only ever tear the
    LAST physical line -- every earlier row was already a complete,
    flushed+fsynced unit. read_rows() must tolerate that, since
    --verify-manifest/--restore are exactly the tools meant to run right
    after such a crash."""

    def test_truncated_last_line_is_dropped_not_raised(self, tmp_path):
        path = str(tmp_path / "manifest.csv")
        manifest.append_planned(path, _row(src_abs="A"))
        manifest.append_planned(path, _row(src_abs="B"))
        # Simulate a crash mid-write: append a partial line (too few
        # fields, no trailing newline) directly, bypassing the normal API.
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write("remove;C;C;123")

        rows = manifest.read_rows(path)

        assert [r.src_abs for r in rows] == ["A", "B"]

    def test_malformed_row_before_the_last_one_still_raises(self, tmp_path):
        # Corruption anywhere except the very last line is a real integrity
        # problem, not an in-progress write -- must not be silently dropped.
        path = str(tmp_path / "manifest.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(";".join(manifest.MANIFEST_COLUMNS) + "\r\n")
            f.write("remove;A;A;not-a-number\r\n")  # malformed, NOT last
            f.write("remove;B;B;123;456;h;1;k;r;t;v;done;\r\n")  # well-formed, last

        with pytest.raises(ValueError):
            manifest.read_rows(path)
