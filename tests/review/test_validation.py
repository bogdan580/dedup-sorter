"""Selection, destination-folder and series-parameter validation (pure)."""
from __future__ import annotations

import pytest
from tidy.review import computations as review_computations
from tidy.review import validation as review_validation

from .helpers import (
    _FILE_REGISTRY,
)


class TestValidateSelections:
    def test_valid_single_group_selection(self):
        groups, decisions, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1"]}], _FILE_REGISTRY,
        )
        assert error is None
        assert len(groups) == 1
        assert len(decisions) == 1
        assert decisions[0].keeper == "D:/A/a.jpg"
        assert decisions[0].discarded[0].path == "D:/A/b.jpg"
        assert decisions[0].discarded[0].reason == "dup-exact"
        assert groups[0].size == 10
        assert groups[0].full_hash == "h1"

    def test_selective_subset_of_groups_leaves_others_out(self):
        groups, decisions, error = review_validation._validate_selections(
            [{"group_id": "2", "keeper_id": "2:0", "discard_ids": ["2:1"]}], _FILE_REGISTRY,
        )
        assert error is None
        assert len(groups) == 1  # group 1 simply never appears in the output
        assert decisions[0].keeper == "D:/B/a.jpg"

    def test_rejects_unknown_id(self):
        groups, decisions, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["does-not-exist"]}], _FILE_REGISTRY,
        )
        assert groups == [] and decisions == []
        assert error is not None and "unknown id" in error

    def test_rejects_unknown_keeper_id(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "does-not-exist", "discard_ids": []}], _FILE_REGISTRY,
        )
        assert error is not None and "unknown id" in error

    def test_rejects_id_from_wrong_group(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["2:1"]}], _FILE_REGISTRY,
        )
        assert error is not None and "does not belong" in error

    def test_rejects_missing_keeper_id(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "discard_ids": ["1:1"]}], _FILE_REGISTRY,
        )
        assert error is not None and "keeper_id" in error

    def test_rejects_empty_keeper_id(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "", "discard_ids": ["1:1"]}], _FILE_REGISTRY,
        )
        assert error is not None

    def test_rejects_keeper_also_listed_as_discard(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:0", "1:1"]}], _FILE_REGISTRY,
        )
        assert error is not None and "duplicate ids" in error

    def test_rejects_duplicate_ids_within_selection(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1", "1:1"]}], _FILE_REGISTRY,
        )
        assert error is not None and "duplicate ids" in error

    def test_rejects_duplicate_group_id_across_selections(self):
        _, _, error = review_validation._validate_selections(
            [
                {"group_id": "1", "keeper_id": "1:0", "discard_ids": ["1:1"]},
                {"group_id": "1", "keeper_id": "1:0", "discard_ids": []},
            ],
            _FILE_REGISTRY,
        )
        assert error is not None and "more than once" in error

    def test_rejects_empty_selections_list(self):
        _, _, error = review_validation._validate_selections([], _FILE_REGISTRY)
        assert error is not None

    def test_rejects_non_list_selections(self):
        _, _, error = review_validation._validate_selections({"not": "a list"}, _FILE_REGISTRY)
        assert error is not None

    def test_rejects_discard_ids_not_a_list(self):
        _, _, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": "1:1"}], _FILE_REGISTRY,
        )
        assert error is not None

    def test_empty_discard_ids_is_valid_no_op_for_that_group(self):
        groups, decisions, error = review_validation._validate_selections(
            [{"group_id": "1", "keeper_id": "1:0", "discard_ids": []}], _FILE_REGISTRY,
        )
        assert error is None
        assert decisions[0].discarded == ()


# --- placement screen -----------------------------------------------------

class TestSanitizeDestFolder:
    """The destination is the first path in this server that comes from
    the client, so it gets its own gate before applier's containment check."""

    @pytest.mark.parametrize("raw,expected", [
        ("#Home", "#Home"),
        ("#Home\\2024 Gdansk", "#Home\\2024 Gdansk"),
        # Forward slashes normalise to the archive's own separator, so the
        # same folder typed either way is one folder, not two.
        ("#Home/2024 Gdansk", "#Home\\2024 Gdansk"),
        ("  #Home  ", "#Home"),
    ])
    def test_accepts_relative_archive_paths(self, raw, expected):
        assert review_validation.sanitize_dest_folder(raw) == (expected, None)

    @pytest.mark.parametrize("raw", [
        "",
        "   ",
        123,
        None,
        "..",
        "#Home\\..\\..\\Windows",
        "#Home\\.\\x",
        "C:\\Windows",
        "\\\\server\\share",
        "/etc/passwd",
        "#Pol<and",
        'a"b',
        "CON",
        "nul.jpg",
        "foo.",
        "a/b/c/d/e/f/g/h/i",
    ])
    def test_rejects_anything_that_could_leave_the_archive_or_break_windows(self, raw):
        value, error = review_validation.sanitize_dest_folder(raw)
        assert value is None
        assert error

    def test_control_characters_are_rejected(self):
        value, error = review_validation.sanitize_dest_folder("#Pol\x01and")
        assert value is None
        assert "control characters" in error


class TestValidatePlacementSelections:
    def _registry(self):
        return {
            "p1:0": {"path": "D:/A/!NEW/Gdansk/a.jpg", "group_id": "p1", "size": 10},
            "p1:1": {"path": "D:/A/!NEW/Gdansk/b.jpg", "group_id": "p1", "size": 20},
            "p2:0": {"path": "D:/A/!NEW/Other/c.jpg", "group_id": "p2", "size": 30},
        }

    def test_builds_one_move_plan_per_file(self):
        plans, error = review_validation._validate_placement_selections(
            [{"group_id": "p1", "file_ids": ["p1:0", "p1:1"], "dest": "#Home\\2024 Gdansk"}],
            self._registry(), "D:/A",
        )
        assert error is None
        assert [p.src_abs for p in plans] == ["D:/A/!NEW/Gdansk/a.jpg", "D:/A/!NEW/Gdansk/b.jpg"]
        assert plans[0].dst_abs == "D:/A/#Home/2024 Gdansk/a.jpg"
        assert plans[0].reason == "event-sort"
        assert plans[0].group_id == "p1"
        assert plans[1].size == 20

    @pytest.mark.parametrize("selections,fragment", [
        ([], "non-empty list"),
        ("nope", "non-empty list"),
        ([{"file_ids": ["p1:0"], "dest": "#Home"}], "group_id"),
        ([{"group_id": "p1", "file_ids": [], "dest": "#Home"}], "must not be empty"),
        ([{"group_id": "p1", "file_ids": "p1:0", "dest": "#Home"}], "list of id strings"),
        ([{"group_id": "p1", "file_ids": ["p1:0", "p1:0"], "dest": "#Home"}], "duplicate ids"),
        ([{"group_id": "p1", "file_ids": ["nope"], "dest": "#Home"}], "unknown id"),
        ([{"group_id": "p1", "file_ids": ["p2:0"], "dest": "#Home"}], "does not belong"),
        ([{"group_id": "p1", "file_ids": ["p1:0"], "dest": "..\\..\\x"}], "'..'"),
    ])
    def test_rejects_bad_shapes_all_or_nothing(self, selections, fragment):
        plans, error = review_validation._validate_placement_selections(
            selections, self._registry(), "D:/A",
        )
        assert plans == []
        assert error is not None and fragment in error

    def test_same_group_twice_is_rejected(self):
        plans, error = review_validation._validate_placement_selections(
            [
                {"group_id": "p1", "file_ids": ["p1:0"], "dest": "#Home"},
                {"group_id": "p1", "file_ids": ["p1:1"], "dest": "#Home"},
            ],
            self._registry(), "D:/A",
        )
        assert plans == []
        assert "more than once" in error

    def test_removal_only_selection_needs_no_destination(self):
        selections = [{"group_id": "p1", "file_ids": [], "remove_ids": ["p1:0", "p1:1"], "dest": ""}]
        plans, error = review_validation._validate_placement_selections(
            selections, self._registry(), "D:/A",
        )
        assert error is None
        assert plans == []
        groups, decisions = review_validation._placement_removals(selections, self._registry())
        assert len(decisions) == 1
        assert decisions[0].keeper == ""
        assert [d.path for d in decisions[0].discarded] == [
            "D:/A/!NEW/Gdansk/a.jpg", "D:/A/!NEW/Gdansk/b.jpg",
        ]
        assert {d.reason for d in decisions[0].discarded} == {"event-remove"}
        assert groups[0].paths == ("D:/A/!NEW/Gdansk/a.jpg", "D:/A/!NEW/Gdansk/b.jpg")

    def test_mixed_selection_splits_into_moves_and_removals(self):
        selections = [{"group_id": "p1", "file_ids": ["p1:0"], "remove_ids": ["p1:1"], "dest": "#Home"}]
        plans, error = review_validation._validate_placement_selections(
            selections, self._registry(), "D:/A",
        )
        assert error is None
        assert [p.src_abs for p in plans] == ["D:/A/!NEW/Gdansk/a.jpg"]
        _, decisions = review_validation._placement_removals(selections, self._registry())
        assert [d.path for d in decisions[0].discarded] == ["D:/A/!NEW/Gdansk/b.jpg"]

    @pytest.mark.parametrize("selection,fragment", [
        ({"group_id": "p1", "file_ids": ["p1:0"], "remove_ids": ["p1:0"], "dest": "#Home"}, "duplicate ids"),
        ({"group_id": "p1", "file_ids": [], "remove_ids": "p1:0", "dest": ""}, "remove_ids must be a list"),
        ({"group_id": "p1", "file_ids": [], "remove_ids": ["p2:0"], "dest": ""}, "does not belong"),
        ({"group_id": "p1", "file_ids": [], "remove_ids": ["nope"], "dest": ""}, "unknown id"),
        ({"group_id": "p1", "file_ids": [], "remove_ids": [], "dest": "#Home"}, "must not be empty"),
    ])
    def test_rejects_bad_remove_ids(self, selection, fragment):
        plans, error = review_validation._validate_placement_selections(
            [selection], self._registry(), "D:/A",
        )
        assert plans == []
        assert error is not None and fragment in error


class TestParseSeriesParams:
    """_parse_series_params(): the guard between the dev-mode selects and
    series.find_series().

    Refusal rather than clamping is the point -- a clamped value would leave
    the select showing a threshold the engine never used."""

    def test_a_body_without_tuning_asks_for_no_change(self):
        params, error = review_computations._parse_series_params({})
        assert error is None
        assert params == {}

    def test_unrelated_keys_are_ignored(self):
        # POST /api/rescan's body is shared: it carried nothing at all
        # before this feature and may carry more later.
        params, error = review_computations._parse_series_params({"something_else": 1})
        assert error is None
        assert params == {}

    def test_valid_values_pass_through(self):
        params, error = review_computations._parse_series_params({
            "series_hamming_threshold": 10,
            "series_window_seconds": 120,
        })
        assert error is None
        assert params == {
            "series_hamming_threshold": 10,
            "series_window_seconds": 120.0,
        }

    def test_either_knob_can_be_sent_alone(self):
        params, error = review_computations._parse_series_params(
            {"series_hamming_threshold": 4},
        )
        assert error is None
        assert params == {"series_hamming_threshold": 4}

    @pytest.mark.parametrize("value", [-1, 65, 1000])
    def test_threshold_outside_the_64_bit_range_is_refused(self, value):
        params, error = review_computations._parse_series_params(
            {"series_hamming_threshold": value},
        )
        assert params is None
        assert "series_hamming_threshold" in error

    @pytest.mark.parametrize("value", [True, "6", 6.5, None, [6]])
    def test_threshold_must_be_a_plain_int(self, value):
        # True is the interesting case: bool is an int subclass, so an
        # isinstance() check alone would accept it as threshold 1.
        params, error = review_computations._parse_series_params(
            {"series_hamming_threshold": value},
        )
        assert params is None
        assert "series_hamming_threshold" in error

    @pytest.mark.parametrize("value", [0, -30, 3601, 86400])
    def test_window_outside_the_bound_is_refused(self, value):
        # The upper bound is the one that matters: the window is the only
        # thing bounding _group_by_similarity()'s pairwise comparison.
        params, error = review_computations._parse_series_params(
            {"series_window_seconds": value},
        )
        assert params is None
        assert "series_window_seconds" in error

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_window_refuses_nan_and_infinities(self, value):
        params, error = review_computations._parse_series_params(
            {"series_window_seconds": value},
        )
        assert params is None
        assert "series_window_seconds" in error

    def test_window_accepts_a_float(self):
        params, error = review_computations._parse_series_params(
            {"series_window_seconds": 90.5},
        )
        assert error is None
        assert params == {"series_window_seconds": 90.5}

    def test_error_messages_are_ascii(self):
        # Not console output, but the same house rule -- and these travel
        # through JSON into the browser either way.
        for payload in (
            {"series_hamming_threshold": 99},
            {"series_hamming_threshold": "x"},
            {"series_window_seconds": 0},
            {"series_window_seconds": "x"},
        ):
            _params, error = review_computations._parse_series_params(payload)
            assert error is not None
            error.encode("ascii")
