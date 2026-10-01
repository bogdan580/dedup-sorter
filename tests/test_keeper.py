"""Tests for keeper.py: keeper-selection scoring and
`config.json` loading.

All inputs are plain path strings + a path->mtime_ns dict, per the module's
own contract -- no FileSystem/FakeFileSystem needed here, since
`DuplicateGroup.paths` (tiers.py) carries no stat info and the caller is
expected to build the mtime mapping separately from `ScanResult.files`.
"""
import json
import random

import pytest

from tidy import config
from tidy import paths
from tidy.duplicates import keeper


def cfg(**overrides):
    defaults = dict(
        priority_dirs=("#Home", "#Trip-A", "#Travel", "#Trip-B"),
        copy_name_patterns=("(1)", "- kopia", "_1", "- Copy"),
        report_top_n=20,
    )
    defaults.update(overrides)
    return keeper.KeeperConfig(**defaults)


class TestRuleZeroNewVsArchive:
    """Same file in !NEW\\Camera (short path) and in
    #Travel\\2023 Mountains (long path, priority dir) -- archive must win,
    unconditionally, regardless of any other scoring criterion."""

    NEW_PATH = "D:/ARCHIVE/!NEW/Camera/IMG_1234.jpg"
    ARCHIVE_PATH = "D:/ARCHIVE/#Travel/2023 Mountains/IMG_1234.jpg"

    def test_archive_copy_wins_over_new(self):
        mtimes = {self.NEW_PATH: 100, self.ARCHIVE_PATH: 200}
        decision = keeper.select_keeper([self.NEW_PATH, self.ARCHIVE_PATH], mtimes, cfg())

        assert decision.keeper == self.ARCHIVE_PATH
        assert len(decision.discarded) == 1
        assert decision.discarded[0].path == self.NEW_PATH
        assert decision.discarded[0].reason == keeper.REASON_RULE_0

    def test_archive_copy_wins_with_reversed_mtimes(self):
        # !NEW file older than archive file -- rule 0 must still override
        # the "older mtime wins" tie-break criterion entirely.
        mtimes = {self.NEW_PATH: 1, self.ARCHIVE_PATH: 999999}
        decision = keeper.select_keeper([self.NEW_PATH, self.ARCHIVE_PATH], mtimes, cfg())

        assert decision.keeper == self.ARCHIVE_PATH
        assert decision.discarded[0].reason == keeper.REASON_RULE_0

    def test_archive_copy_wins_with_reversed_name_lengths(self):
        # Give the !NEW path a much longer basename than the archive path --
        # rule 0 must still override the "shorter path wins" criterion.
        new_path = "D:/ARCHIVE/!NEW/Camera/A_VERY_MUCH_LONGER_FILE_NAME_INDEED.jpg"
        archive_path = "D:/ARCHIVE/#Travel/2023 Mountains/x.jpg"
        mtimes = {new_path: 0, archive_path: 0}
        decision = keeper.select_keeper([new_path, archive_path], mtimes, cfg())

        assert decision.keeper == archive_path
        assert decision.discarded[0].path == new_path
        assert decision.discarded[0].reason == keeper.REASON_RULE_0

    def test_all_paths_in_new_falls_through_to_tie_break(self):
        # Both copies in !NEW (e.g. Camera vs a named folder) -- rule 0
        # doesn't apply, tie-break among the !NEW candidates instead.
        camera = "D:/ARCHIVE/!NEW/Camera/IMG_1.jpg"
        named = "D:/ARCHIVE/!NEW/Gdansk/IMG_1.jpg"
        mtimes = {camera: 0, named: 0}
        decision = keeper.select_keeper([camera, named], mtimes, cfg())

        assert decision.keeper in (camera, named)
        assert decision.discarded[0].reason == keeper.REASON_DUP_EXACT

    def test_all_paths_in_archive_falls_through_to_tie_break(self):
        a = "D:/ARCHIVE/#Travel/2023 Mountains/a.jpg"
        b = "D:/ARCHIVE/#Travel/2023 Mountains/b.jpg"
        mtimes = {a: 0, b: 0}
        decision = keeper.select_keeper([a, b], mtimes, cfg())

        assert decision.discarded[0].reason == keeper.REASON_DUP_EXACT

    def test_multiple_new_paths_all_discarded_as_rule_0(self):
        new1 = "D:/ARCHIVE/!NEW/Camera/x.jpg"
        new2 = "D:/ARCHIVE/!NEW/Gdansk/x.jpg"
        archive = "D:/ARCHIVE/#Home/2024 Trip/x.jpg"
        mtimes = {new1: 0, new2: 0, archive: 0}
        decision = keeper.select_keeper([new1, new2, archive], mtimes, cfg())

        assert decision.keeper == archive
        reasons = {d.path: d.reason for d in decision.discarded}
        assert reasons[new1] == keeper.REASON_RULE_0
        assert reasons[new2] == keeper.REASON_RULE_0


class TestTieBreakChain:
    def test_priority_dir_wins(self):
        priority = "D:/ARCHIVE/#Travel/x.jpg"
        other = "D:/ARCHIVE/#RandomPlace/x.jpg"
        mtimes = {priority: 0, other: 0}
        decision = keeper.select_keeper([priority, other], mtimes, cfg())
        assert decision.keeper == priority

    def test_priority_dir_rank_lower_index_wins(self):
        # index 0 in priority_dirs beats index 2.
        c = cfg(priority_dirs=("#Home", "#Trip-A", "#Travel"))
        higher_rank = "D:/ARCHIVE/#Home/x.jpg"
        lower_rank = "D:/ARCHIVE/#Travel/x.jpg"
        mtimes = {higher_rank: 0, lower_rank: 0}
        decision = keeper.select_keeper([higher_rank, lower_rank], mtimes, c)
        assert decision.keeper == higher_rank

    def test_priority_dir_matches_by_segment_not_prefix(self):
        # "#Travel" must match as a path *segment*, not just literal
        # string-prefix -- exercise a path where the priority dir is nested
        # deep inside, not at the start.
        nested = "D:/ARCHIVE/Some/Deep/Path/#Travel/2022/x.jpg"
        other = "D:/ARCHIVE/#RandomPlace/x.jpg"
        mtimes = {nested: 0, other: 0}
        decision = keeper.select_keeper([nested, other], mtimes, cfg())
        assert decision.keeper == nested

    def test_non_copy_name_wins_over_copy_name(self):
        original = "D:/ARCHIVE/#Home/IMG_1234.jpg"
        copy = "D:/ARCHIVE/#Home/IMG_1234 - kopia.jpg"
        mtimes = {original: 0, copy: 0}
        decision = keeper.select_keeper([original, copy], mtimes, cfg())
        assert decision.keeper == original
        assert decision.discarded[0].path == copy

    def test_copy_pattern_paren_1(self):
        original = "D:/ARCHIVE/#Home/IMG_1234.jpg"
        copy = "D:/ARCHIVE/#Home/IMG_1234(1).jpg"
        mtimes = {original: 0, copy: 0}
        decision = keeper.select_keeper([original, copy], mtimes, cfg())
        assert decision.keeper == original

    def test_shorter_path_wins_when_dir_and_copy_name_tied(self):
        short = "D:/ARCHIVE/#Home/a.jpg"
        long = "D:/ARCHIVE/#Home/a-much-longer-name.jpg"
        mtimes = {short: 0, long: 0}
        decision = keeper.select_keeper([short, long], mtimes, cfg())
        assert decision.keeper == short

    def test_older_mtime_wins_when_earlier_criteria_tied(self):
        older = "D:/ARCHIVE/#Home/a.jpg"
        newer = "D:/ARCHIVE/#Home/b.jpg"
        mtimes = {older: 100, newer: 200}
        decision = keeper.select_keeper([older, newer], mtimes, cfg())
        assert decision.keeper == older

    def test_alphabetical_final_tiebreak(self):
        a = "D:/ARCHIVE/#Home/a.jpg"
        b = "D:/ARCHIVE/#Home/b.jpg"
        mtimes = {a: 0, b: 0}
        decision = keeper.select_keeper([a, b], mtimes, cfg())
        assert decision.keeper == a


class TestReproducibility:
    def test_shuffled_input_order_gives_same_keeper(self):
        paths = [
            "D:/ARCHIVE/#Home/img1.jpg",
            "D:/ARCHIVE/#RandomPlace/img1 - kopia.jpg",
            "D:/ARCHIVE/!NEW/Camera/img1.jpg",
            "D:/ARCHIVE/#Trip-A/deep/nested/dir/img1.jpg",
        ]
        mtimes = {p: i * 1000 for i, p in enumerate(paths)}
        c = cfg()

        baseline = keeper.select_keeper(paths, mtimes, c)

        rng = random.Random(42)
        for _ in range(20):
            shuffled = list(paths)
            rng.shuffle(shuffled)
            result = keeper.select_keeper(shuffled, mtimes, c)
            assert result.keeper == baseline.keeper
            assert {d.path for d in result.discarded} == {d.path for d in baseline.discarded}
            assert {(d.path, d.reason) for d in result.discarded} == {
                (d.path, d.reason) for d in baseline.discarded
            }


class TestIsNewPath:
    def test_new_dir_detected_as_segment(self):
        assert keeper.is_new_path("D:/ARCHIVE/!NEW/Camera/x.jpg") is True

    def test_new_dir_casefold_matched(self):
        assert keeper.is_new_path("D:/ARCHIVE/!new/Camera/x.jpg") is True

    def test_non_new_path_not_detected(self):
        assert keeper.is_new_path("D:/ARCHIVE/#Home/x.jpg") is False

    def test_new_as_substring_not_a_segment_not_matched(self):
        # "!NEWSTUFF" must not match the "!NEW" segment check.
        assert keeper.is_new_path("D:/ARCHIVE/!NEWSTUFF/x.jpg") is False


class TestKeeperConfigDefaults:
    def test_default_config_has_no_priority_dirs(self):
        c = keeper.KeeperConfig()
        assert c.priority_dirs == ()
        assert c.report_top_n == 20

    def test_missing_file_returns_defaults(self, tmp_path):
        c = keeper.load_keeper_config(str(tmp_path / "does_not_exist.json"))
        assert c == keeper.KeeperConfig()

    def test_none_path_returns_defaults(self):
        assert keeper.load_keeper_config(None) == keeper.KeeperConfig()

    def test_loads_real_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({
                "priority_dirs": ["#Only"],
                "copy_name_patterns": ["_copy"],
                "report_top_n": 5,
            }),
            encoding="utf-8",
        )
        c = keeper.load_keeper_config(str(path))
        assert c.priority_dirs == ("#Only",)
        assert c.copy_name_patterns == ("_copy",)
        assert c.report_top_n == 5

    def test_partial_file_falls_back_to_defaults_for_missing_keys(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"priority_dirs": ["#Only"]}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.priority_dirs == ("#Only",)
        assert c.copy_name_patterns == keeper.DEFAULT_COPY_NAME_PATTERNS
        assert c.report_top_n == keeper.DEFAULT_REPORT_TOP_N

    def test_malformed_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not valid json", encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c == keeper.KeeperConfig()

    def test_json_array_instead_of_object_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(["#Home", "#Travel"]), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c == keeper.KeeperConfig()

    def test_from_dict(self):
        c = keeper.keeper_config_from_dict({"priority_dirs": ["#X"], "report_top_n": 3})
        assert c.priority_dirs == ("#X",)
        assert c.report_top_n == 3
        assert c.copy_name_patterns == keeper.DEFAULT_COPY_NAME_PATTERNS

    def test_priority_dirs_wrong_type_falls_back_instead_of_crashing(self, tmp_path):
        # A very plausible hand-editing typo: forgetting the [...] brackets.
        # tuple("#Home") would silently produce a per-character tuple
        # instead of raising or falling back -- must not happen.
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"priority_dirs": "#Home"}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.priority_dirs == keeper.DEFAULT_PRIORITY_DIRS

    def test_copy_name_patterns_wrong_type_falls_back_instead_of_crashing(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"copy_name_patterns": 5}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.copy_name_patterns == keeper.DEFAULT_COPY_NAME_PATTERNS

    def test_report_top_n_null_falls_back_instead_of_crashing(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"report_top_n": None}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.report_top_n == keeper.DEFAULT_REPORT_TOP_N

    def test_report_top_n_non_numeric_string_falls_back_instead_of_crashing(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"report_top_n": "not-a-number"}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.report_top_n == keeper.DEFAULT_REPORT_TOP_N

    def test_report_top_n_bool_falls_back_instead_of_becoming_one(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"report_top_n": True}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.report_top_n == keeper.DEFAULT_REPORT_TOP_N

    def test_priority_dirs_list_with_non_string_element_falls_back(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"priority_dirs": ["#Home", 5]}), encoding="utf-8")
        c = keeper.load_keeper_config(str(path))
        assert c.priority_dirs == keeper.DEFAULT_PRIORITY_DIRS

    def test_malformed_json_prints_warning(self, tmp_path, capsys):
        path = tmp_path / "config.json"
        path.write_text("{not valid json", encoding="utf-8")
        keeper.load_keeper_config(str(path))
        assert "Warning" in capsys.readouterr().out

    def test_wrong_field_type_prints_warning(self, tmp_path, capsys):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"priority_dirs": "#Home"}), encoding="utf-8")
        keeper.load_keeper_config(str(path))
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "priority_dirs" in out

    def test_missing_file_does_not_print_warning(self, tmp_path, capsys):
        # Missing config.json is an expected, unremarkable state (fresh
        # checkout, tests, etc.) -- only content problems should warn.
        keeper.load_keeper_config(str(tmp_path / "does_not_exist.json"))
        assert capsys.readouterr().out == ""


class TestExampleConfigJson:
    """The config.example.json template shipped in the repo root must load
    and show example priority folders."""

    def test_example_config_file_loads(self):
        import os
        example = os.path.join(str(paths.DEDUP_DIR), "config.example.json")
        c = keeper.load_keeper_config(example)
        for expected in ("#Travel", "#Family"):
            assert expected in c.priority_dirs

class TestSingleFileGroupIsDegenerate:
    def test_single_path_is_its_own_keeper_no_discards(self):
        decision = keeper.select_keeper(["D:/ARCHIVE/a.jpg"], {"D:/ARCHIVE/a.jpg": 0}, cfg())
        assert decision.keeper == "D:/ARCHIVE/a.jpg"
        assert decision.discarded == ()

    def test_empty_paths_raises(self):
        with pytest.raises(ValueError):
            keeper.select_keeper([], {}, cfg())
