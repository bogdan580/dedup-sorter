"""Tests for report.py: raport.csv writer (UTF-8 BOM, ';' delimiter, RFC-4180
quoting) and the pure console-summary formatting function.
"""
import csv
import io

import pytest

from tidy.duplicates import report


def row(**overrides):
    defaults = dict(
        group_id="1",
        path="D:/ARCHIVE/a.jpg",
        size=100,
        mtime=1_700_000_000,
        partial_hash="abc",
        full_hash="def",
        kind="dup",
    )
    defaults.update(overrides)
    return report.ReportRow(**defaults)


class TestWriteCsv:
    def test_header_row_matches_spec(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row()])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            header = next(reader)
        assert header == [
            "group_id", "path", "size", "mtime", "partial_hash", "full_hash", "kind",
            "role", "reason",
        ]

    def test_creates_missing_parent_directory(self, tmp_path):
        """Reports live in `.dedup\\.reports\\`, which is not in git and
        which the user may delete -- the writer has to create it rather
        than fail on the first run after a fresh clone."""
        path = tmp_path / ".reports" / "raport.csv"
        report.write_csv(str(path), [row()])

        assert path.is_file()

    def test_file_has_utf8_bom(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row()])

        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_uses_crlf_line_endings(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row()])

        raw = path.read_bytes()
        # csv module writes \r\n by default when newline='' is used on open;
        # verify no bare \n survives without a preceding \r.
        text_after_bom = raw[3:]
        assert b"\r\n" in text_after_bom
        assert b"\n\n" not in text_after_bom

    def test_all_kind_values_accepted(self, tmp_path):
        path = tmp_path / "raport.csv"
        kinds = ["dup", "unique", "hardlink-of", "offline", "zero-byte", "error"]
        rows = [row(kind=k, path=f"D:/ARCHIVE/{k}.jpg") for k in kinds]
        report.write_csv(str(path), rows)

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)  # header
            read_kinds = [r[6] for r in reader]
        assert read_kinds == kinds

    def test_cyrillic_path_roundtrips(self, tmp_path):
        path = tmp_path / "raport.csv"
        cyr_path = "C:/Photo Archive/#Місто/фото.jpg"
        report.write_csv(str(path), [row(path=cyr_path)])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[1] == cyr_path

    def test_path_with_semicolon_roundtrips(self, tmp_path):
        path = tmp_path / "raport.csv"
        tricky_path = "D:/ARCHIVE/weird;name;with;semicolons.jpg"
        report.write_csv(str(path), [row(path=tricky_path)])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[1] == tricky_path

    def test_path_with_quote_and_newline_roundtrips(self, tmp_path):
        path = tmp_path / "raport.csv"
        tricky_path = 'D:/ARCHIVE/weird"name\nwith-newline.jpg'
        report.write_csv(str(path), [row(path=tricky_path)])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[1] == tricky_path


class TestFormatSummary:
    def test_formats_key_counts(self):
        summary = report.ScanSummary(
            files_scanned=100,
            tier1_unique_count=80,
            tier2_dropped_count=5,
            tier3_not_duplicate_count=2,
            duplicate_group_count=3,
            reclaimable_bytes=1_073_741_824,  # 1 GiB
            skipped_reparse_count=1,
            skipped_offline_count=2,
            errors_count=1,
            zero_byte_count=4,
            inventory_only_count=6,
            hardlink_count=2,
        )
        text = report.format_summary(summary)

        assert "100" in text  # files scanned
        assert "3" in text  # duplicate groups
        assert "1.00 GB" in text or "1,00 GB" in text  # reclaimable size, tolerant of locale decimal
        assert "1" in text  # errors count appears somewhere

    def test_zero_duplicates_still_formats_cleanly(self):
        summary = report.ScanSummary(
            files_scanned=10,
            tier1_unique_count=10,
            tier2_dropped_count=0,
            tier3_not_duplicate_count=0,
            duplicate_group_count=0,
            reclaimable_bytes=0,
            skipped_reparse_count=0,
            skipped_offline_count=0,
            errors_count=0,
            zero_byte_count=0,
            inventory_only_count=0,
            hardlink_count=0,
        )
        text = report.format_summary(summary)
        assert "0" in text
        assert isinstance(text, str)
        assert len(text) > 0


class TestDisplayPath:
    def test_strips_long_path_prefix(self):
        assert report.display_path("\\\\?\\D:\\ARCHIVE\\a.jpg") == "D:\\ARCHIVE\\a.jpg"

    def test_strips_unc_prefix(self):
        assert report.display_path("\\\\?\\UNC\\server\\share\\a.jpg") == "\\\\server\\share\\a.jpg"

    def test_plain_path_is_unchanged(self):
        assert report.display_path("D:/ARCHIVE/a.jpg") == "D:/ARCHIVE/a.jpg"


class TestCsvFormulaInjectionGuard:
    """Regression coverage for the review finding: raport.csv's `path`
    column is built directly from filesystem names the tool doesn't
    control, and Excel treats a cell starting with =, +, -, @, or a tab as
    a formula/DDE command."""

    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t"])
    def test_path_starting_with_formula_trigger_is_neutralized(self, tmp_path, trigger):
        # A full archive path always starts with a drive letter, but the
        # guard operates on whatever ends up in the `path` cell -- exercise
        # it directly with a value that starts with the trigger character.
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row(path=f"{trigger}cmd.jpg")])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)  # header
            data_row = next(reader)
        assert data_row[1] == f"'{trigger}cmd.jpg"

    def test_ordinary_path_is_not_modified(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row(path="D:/ARCHIVE/normal.jpg")])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[1] == "D:/ARCHIVE/normal.jpg"

    def test_long_path_prefix_is_stripped_in_report(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row(path="\\\\?\\D:\\ARCHIVE\\a.jpg")])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[1] == "D:\\ARCHIVE\\a.jpg"


class TestRoleReasonColumns:
    """role/reason columns marking keeper vs discarded within a
    duplicate group (reason vocabulary: dup-exact/rule-0)."""

    def test_keeper_role_roundtrips(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row(role=report.ROLE_KEEPER, reason="")])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[7] == "keeper"
        assert data_row[8] == ""

    def test_discarded_role_and_reason_roundtrip(self, tmp_path):
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row(role=report.ROLE_DISCARDED, reason="rule-0")])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[7] == "discarded"
        assert data_row[8] == "rule-0"

    def test_role_and_reason_default_to_blank(self, tmp_path):
        # Non-duplicate rows (unique/offline/etc.) never call select_keeper,
        # so ReportRow's role/reason must default to blank without callers
        # having to pass them explicitly everywhere.
        path = tmp_path / "raport.csv"
        report.write_csv(str(path), [row()])

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader)
            data_row = next(reader)
        assert data_row[7] == ""
        assert data_row[8] == ""


class _FakeDecision:
    """Duck-typed stand-in for keeper.KeeperDecision: report.py's
    build_grouped_report() only ever accesses .keeper/.discarded[].path, so
    tests here don't need a real keeper.py import."""

    def __init__(self, keeper_path, discarded_paths):
        self.keeper = keeper_path
        self.discarded = [_FakeDiscard(p) for p in discarded_paths]


class _FakeDiscard:
    def __init__(self, path):
        self.path = path


class _FakeGroup:
    """Duck-typed stand-in for tiers.DuplicateGroup: only .size is read."""

    def __init__(self, size):
        self.size = size


class TestBuildGroupedReport:
    def test_totals_and_ordering(self):
        groups = [_FakeGroup(size=100), _FakeGroup(size=10)]
        decisions = [
            _FakeDecision("keeper-a", ["discard-a1", "discard-a2"]),  # reclaim 200
            _FakeDecision("keeper-b", ["discard-b1"]),  # reclaim 10
        ]

        grouped = report.build_grouped_report(groups, decisions, top_n=20)

        assert grouped.group_count == 2
        assert grouped.total_reclaimable_bytes == 210
        assert grouped.top_groups[0].keeper == "keeper-a"
        assert grouped.top_groups[0].reclaimable_bytes == 200
        assert grouped.top_groups[0].discarded == ("discard-a1", "discard-a2")
        assert grouped.top_groups[1].keeper == "keeper-b"

    def test_top_n_truncates(self):
        groups = [_FakeGroup(size=i + 1) for i in range(5)]
        decisions = [_FakeDecision(f"keeper-{i}", [f"d-{i}"]) for i in range(5)]

        grouped = report.build_grouped_report(groups, decisions, top_n=2)

        assert grouped.group_count == 5
        assert len(grouped.top_groups) == 2
        # Largest reclaim first: size 5 group, then size 4 group.
        assert grouped.top_groups[0].keeper == "keeper-4"
        assert grouped.top_groups[1].keeper == "keeper-3"

    def test_no_groups_gives_zero_totals(self):
        grouped = report.build_grouped_report([], [], top_n=20)
        assert grouped.group_count == 0
        assert grouped.total_reclaimable_bytes == 0
        assert grouped.top_groups == ()

    def test_group_with_no_discards_reclaims_zero(self):
        groups = [_FakeGroup(size=500)]
        decisions = [_FakeDecision("only-copy", [])]
        grouped = report.build_grouped_report(groups, decisions, top_n=20)
        assert grouped.total_reclaimable_bytes == 0
        assert grouped.top_groups[0].reclaimable_bytes == 0


class TestFormatGroupedReport:
    def test_includes_group_count_and_total(self):
        grouped = report.GroupedReport(
            total_reclaimable_bytes=2 * 1024 ** 3,
            group_count=7,
            top_groups=(
                report.GroupReportEntry(
                    keeper="D:/ARCHIVE/a.jpg", discarded=("D:/ARCHIVE/b.jpg",),
                    size=1024 ** 3, reclaimable_bytes=1024 ** 3,
                ),
            ),
        )
        text = report.format_grouped_report(grouped)
        assert "7" in text
        assert "2.00 GB" in text or "2,00 GB" in text
        assert "a.jpg" in text
        assert "b.jpg" in text

    def test_zero_groups_formats_cleanly(self):
        grouped = report.GroupedReport(
            total_reclaimable_bytes=0, group_count=0, top_groups=(),
        )
        text = report.format_grouped_report(grouped)
        assert isinstance(text, str)
        assert "0" in text
