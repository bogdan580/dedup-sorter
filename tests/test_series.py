"""Tests for series.py (time clustering + dHash visual grouping +
keeper-suggestion ranking).

Pure-logic pieces (_cluster_by_time, _group_by_similarity,
_select_suggested_keeper, _classify_kind) are tested directly against
hand-built data, mirroring scanner.py's own convention of unit-testing
private pure functions (e.g. _parent_dir) in isolation. find_series() itself
is exercised end-to-end against FakeFileSystem with real Pillow-encoded
JPEG bytes plus a real (tmp_path) SQLite Cache, matching test_thumbnails.py.
"""
import csv
import io
from datetime import datetime, timedelta

import pytest
from PIL import ExifTags, Image, ImageDraw

from tidy.analysis import series
from tidy.media import thumbnails
from tidy.media import timeline
from tidy.store.cache import Cache, CachedFile
from fakes import FakeFileSystem, make_stat
from tidy.progress import DEFAULT_WORKERS, ProgressCounter
from tidy.duplicates.scanner import ScannedFile

# HEIC end-to-end coverage ("regula oryginalu") needs a real
# pillow-heif-decodable fixture -- series.py imports thumbnails, which
# registers pillow-heif's HEIF opener at import time (see thumbnails.py),
# so this flag reflects whether that registration actually succeeded in
# this environment, mirroring test_thumbnails.py's own _heif_available.
_heif_available = pytest.mark.skipif(
    not thumbnails._HEIF_AVAILABLE, reason="pillow-heif not available in this environment"
)


# --- helpers -----------------------------------------------------------------

def _ts(when: datetime, source=timeline.SOURCE_EXIF) -> timeline.Timestamp:
    return timeline.Timestamp(when=when, source=source)


def _member(path, *, width=100, height=100, sharpness=1.0, size=1000, has_exif=True, when=None):
    return series.SeriesMember(
        path=path, timestamp=_ts(when or datetime(2021, 1, 1)), dhash=0,
        width=width, height=height, sharpness=sharpness, size=size, has_exif=has_exif,
    )


def _scanned(path, *, size=1000, mtime_ns=0):
    return ScannedFile(path=path, stat=make_stat(size=size, mtime_ns=mtime_ns), is_duplicate_candidate=True)


def _base_pattern_image(size=(320, 240)):
    image = Image.new("RGB", size, (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.ellipse([size[0] * 0.2, size[1] * 0.2, size[0] * 0.8, size[1] * 0.8], fill=(220, 200, 50))
    draw.rectangle([size[0] * 0.05, size[1] * 0.05, size[0] * 0.3, size[1] * 0.3], fill=(50, 120, 220))
    return image


def _checkerboard_image(size=(320, 240), cell=16):
    image = Image.new("RGB", size, (0, 0, 0))
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if (x // cell + y // cell) % 2 == 0:
                pixels[x, y] = (255, 255, 255)
    return image


def _jpeg_bytes(image: Image.Image, when: str) -> bytes:
    # Writes DateTimeOriginal into the real Exif SubIFD (like a real
    # camera), not the top-level IFD0 dict -- see test_timeline.py's
    # _jpeg_bytes_with_exif() docstring for why this distinction mattered.
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[ExifTags.IFD.Exif] = {36867: when}
    image.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _heic_bytes(image: Image.Image, when: str) -> bytes:
    # Same real-camera-like Exif SubIFD placement as _jpeg_bytes() above,
    # just encoded via pillow-heif's HEIF writer instead of Pillow's own
    # JPEG one -- self-contained (no binary fixture committed), matching
    # test_thumbnails.py's _heic_bytes() helper.
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[ExifTags.IFD.Exif] = {36867: when}
    image.save(buf, format="HEIF", exif=exif.tobytes())
    return buf.getvalue()


# --- _cluster_by_time --------------------------------------------------------

class TestClusterByTime:
    def test_entries_within_window_form_one_cluster(self):
        a = (_scanned("a.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 0)))
        b = (_scanned("b.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 2)))
        clusters = series._cluster_by_time([a, b], window_seconds=300)
        assert len(clusters) == 1
        assert {e[0].path for e in clusters[0]} == {"a.jpg", "b.jpg"}

    def test_entries_beyond_window_form_separate_clusters_and_singletons_dropped(self):
        a = (_scanned("a.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 0)))
        b = (_scanned("b.jpg"), _ts(datetime(2021, 1, 1, 10, 20, 0)))  # 20 min later
        clusters = series._cluster_by_time([a, b], window_seconds=300)
        assert clusters == []  # each is a singleton cluster -> dropped

    def test_three_entries_all_within_window_of_first_form_one_cluster(self):
        a = (_scanned("a.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 0)))
        b = (_scanned("b.jpg"), _ts(datetime(2021, 1, 1, 10, 2, 0)))  # 2 min after a
        c = (_scanned("c.jpg"), _ts(datetime(2021, 1, 1, 10, 4, 0)))  # 4 min after a
        clusters = series._cluster_by_time([a, b, c], window_seconds=300)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_cluster_span_is_bounded_by_window_not_chainable_indefinitely(self):
        # Regression: a pure consecutive-gap check would chain a, b, c into
        # one cluster (each 4 min after the previous) even though c is 8
        # min after a -- beyond the 5 min window. Total cluster span must
        # be bounded by window_seconds from the cluster's first member.
        a = (_scanned("a.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 0)))
        b = (_scanned("b.jpg"), _ts(datetime(2021, 1, 1, 10, 4, 0)))  # 4 min after a
        c = (_scanned("c.jpg"), _ts(datetime(2021, 1, 1, 10, 8, 0)))  # 8 min after a
        clusters = series._cluster_by_time([a, b, c], window_seconds=300)
        assert len(clusters) == 1
        assert {e[0].path for e in clusters[0]} == {"a.jpg", "b.jpg"}  # c starts a new (dropped) cluster

    def test_long_steady_chain_splits_into_multiple_bounded_clusters(self):
        # A continuous multi-hour shoot (one photo every 4 minutes) must
        # split into several window-bounded clusters instead of one
        # unboundedly large cluster -- the scenario the previous two tests
        # individually exercise, chained across many more entries.
        entries = [
            (_scanned(f"p{i}.jpg"), _ts(datetime(2021, 1, 1, 10, 0, 0) + timedelta(minutes=4 * i)))
            for i in range(20)
        ]
        clusters = series._cluster_by_time(entries, window_seconds=300)
        assert len(clusters) > 1
        assert all(
            (max(e[1].when for e in c) - min(e[1].when for e in c)).total_seconds() <= 300
            for c in clusters
        )


# --- _classify_kind -----------------------------------------------------------

class TestClassifyKind:
    def test_burst_boundary(self):
        assert series._classify_kind(3.0) == series.KIND_BURST
        assert series._classify_kind(3.01) == series.KIND_SAME_SHOT

    def test_same_shot_boundary(self):
        assert series._classify_kind(30.0) == series.KIND_SAME_SHOT
        assert series._classify_kind(30.01) == series.KIND_SAME_SCENE

    def test_same_scene_for_large_gap(self):
        assert series._classify_kind(299.0) == series.KIND_SAME_SCENE


# --- _group_by_similarity -----------------------------------------------------

class TestGroupBySimilarity:
    def test_close_dhashes_grouped_far_one_excluded(self):
        entries = [
            (_scanned("a.jpg"), _ts(datetime(2021, 1, 1)), 0b0000),
            (_scanned("b.jpg"), _ts(datetime(2021, 1, 1)), 0b0001),  # distance 1 from a
            (_scanned("c.jpg"), _ts(datetime(2021, 1, 1)), 0xFFFFFFFFFFFFFFFF),  # distance 64 from a
        ]
        groups = series._group_by_similarity(entries, hamming_threshold=6)
        assert len(groups) == 1
        assert set(groups[0]) == {0, 1}

    def test_transitive_chain_groups_all_three(self):
        # a~b (distance 4) and b~c (distance 4) but a~c (distance 8) is
        # over threshold=6 directly -- union-find still merges all three
        # transitively through b, which is the deliberate semantics here.
        entries = [
            (_scanned("a.jpg"), _ts(datetime(2021, 1, 1)), 0b00000000),
            (_scanned("b.jpg"), _ts(datetime(2021, 1, 1)), 0b00001111),
            (_scanned("c.jpg"), _ts(datetime(2021, 1, 1)), 0b11111111),
        ]
        groups = series._group_by_similarity(entries, hamming_threshold=4)
        assert len(groups) == 1
        assert set(groups[0]) == {0, 1, 2}

    def test_no_pairs_within_threshold_returns_no_groups(self):
        entries = [
            (_scanned("a.jpg"), _ts(datetime(2021, 1, 1)), 0b0000),
            (_scanned("b.jpg"), _ts(datetime(2021, 1, 1)), 0xFFFFFFFFFFFFFFFF),
        ]
        assert series._group_by_similarity(entries, hamming_threshold=6) == []


# --- _select_suggested_keeper --------------------------------------------------

class TestSelectSuggestedKeeper:
    def test_higher_resolution_wins(self):
        # size held equal so resolution (the next criterion after size) is
        # what decides here -- see test_size_wins_over_resolution_and_sharpness
        # below for size's own (higher-priority) tiebreak behaviour.
        low = _member("low.jpg", width=100, height=100, sharpness=999, size=1000)
        high = _member("high.jpg", width=200, height=200, sharpness=0.1, size=1000)
        assert series._select_suggested_keeper([low, high]) == "high.jpg"

    def test_sharpness_breaks_resolution_tie(self):
        # size and resolution held equal so sharpness is what decides here.
        blurry = _member("blurry.jpg", width=100, height=100, sharpness=1.0, size=1000)
        sharp = _member("sharp.jpg", width=100, height=100, sharpness=50.0, size=1000)
        assert series._select_suggested_keeper([blurry, sharp]) == "sharp.jpg"

    def test_size_wins_over_resolution_and_sharpness(self):
        # Size ranks second (right after original_rank) -- ahead of both
        # resolution and sharpness: the larger file wins even when it is
        # smaller in resolution and blurrier than the alternative.
        small_sharp = _member("small_sharp.jpg", width=400, height=400, sharpness=999.0, size=100)
        large_blurry = _member("large_blurry.jpg", width=100, height=100, sharpness=0.1, size=999999)
        assert series._select_suggested_keeper([small_sharp, large_blurry]) == "large_blurry.jpg"

    def test_size_breaks_sharpness_tie(self):
        small = _member("small.jpg", width=100, height=100, sharpness=5.0, size=100, has_exif=False)
        large = _member("large.jpg", width=100, height=100, sharpness=5.0, size=99999, has_exif=False)
        assert series._select_suggested_keeper([small, large]) == "large.jpg"

    def test_exif_presence_breaks_size_tie(self):
        no_exif = _member("no_exif.jpg", width=100, height=100, sharpness=5.0, size=100, has_exif=False)
        with_exif = _member("with_exif.jpg", width=100, height=100, sharpness=5.0, size=100, has_exif=True)
        assert series._select_suggested_keeper([no_exif, with_exif]) == "with_exif.jpg"

    def test_path_is_final_tiebreak(self):
        a = _member("a.jpg", width=100, height=100, sharpness=5.0, size=100, has_exif=True)
        b = _member("b.jpg", width=100, height=100, sharpness=5.0, size=100, has_exif=True)
        assert series._select_suggested_keeper([b, a]) == "a.jpg"

    def test_original_format_wins_over_converted_copy_regardless_of_other_criteria(self):
        # "regula oryginalu": IMG_1234.heic must win over
        # IMG_1234.jpg even though the JPG here has better resolution,
        # sharpness, size and EXIF presence -- the HEIC is the camera's own
        # source format, the JPG a same-scene export of it.
        source = _member(
            "D:/Archive/IMG_1234.heic", width=10, height=10, sharpness=0.01, size=1, has_exif=False,
        )
        converted = _member(
            "D:/Archive/IMG_1234.jpg", width=999, height=999, sharpness=999.0, size=999999, has_exif=True,
        )
        assert series._select_suggested_keeper([source, converted]) == "D:/Archive/IMG_1234.heic"

    def test_original_rule_does_not_apply_across_different_base_names(self):
        heic = _member("D:/Archive/IMG_0001.heic", width=10, height=10)
        unrelated_jpg = _member("D:/Archive/IMG_9999.jpg", width=999, height=999)
        assert series._select_suggested_keeper([heic, unrelated_jpg]) == "D:/Archive/IMG_9999.jpg"


# --- _get_dhash caching --------------------------------------------------------

class TestGetDhashCaching:
    def test_second_call_reuses_cache_without_reopening(self, tmp_path):
        fs = FakeFileSystem()
        image = _base_pattern_image()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes(image, "2021:01:01 10:00:00"))
        scanned = _scanned(
            "D:/Archive/photo.jpg",
            size=len(fs._contents["D:/Archive/photo.jpg"]),
            mtime_ns=123,
        )
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            first = series._get_dhash(fs, cache, scanned, clock=lambda: "2026-01-01T00:00:00")
            assert fs.open_read_calls == ["D:/Archive/photo.jpg"]

            second = series._get_dhash(fs, cache, scanned, clock=lambda: "2026-01-01T00:00:00")
            assert second == first
            assert fs.open_read_calls == ["D:/Archive/photo.jpg"]  # no second open
        finally:
            cache.close()

    def test_force_recompute_bypasses_cache_hit(self, tmp_path):
        # --rescan (tidy/cli) threads force_recompute=True through here --
        # it must actually re-decode instead of trusting the cached value.
        fs = FakeFileSystem()
        image = _base_pattern_image()
        fs.add_file("D:/Archive/photo.jpg", _jpeg_bytes(image, "2021:01:01 10:00:00"))
        scanned = _scanned(
            "D:/Archive/photo.jpg",
            size=len(fs._contents["D:/Archive/photo.jpg"]),
            mtime_ns=123,
        )
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            series._get_dhash(fs, cache, scanned, clock=lambda: "2026-01-01T00:00:00")
            assert fs.open_read_calls == ["D:/Archive/photo.jpg"]

            series._get_dhash(
                fs, cache, scanned, clock=lambda: "2026-01-01T00:00:00", force_recompute=True,
            )
            assert fs.open_read_calls == ["D:/Archive/photo.jpg", "D:/Archive/photo.jpg"]
        finally:
            cache.close()

    def test_preserves_existing_partial_and_full_hash(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            scanned = _scanned("D:/Archive/photo.jpg", size=100, mtime_ns=123)
            cache.put(CachedFile(
                path=scanned.path, display_path=scanned.path, size=100, mtime_ns=123,
                volume_id=1, file_index=1, attributes=0,
                partial_hash="partial-abc", full_hash="full-abc", dhash=None,
                status="ok", error=None, scanned_at="2026-01-01T00:00:00",
            ))

            series._put_dhash(cache, scanned, 0x1234, clock=lambda: "2026-01-01T00:00:01")

            row = cache.get(scanned.path)
            assert row.partial_hash == "partial-abc"
            assert row.full_hash == "full-abc"
            assert row.dhash == "0000000000001234"
        finally:
            cache.close()


# --- find_series (end to end) --------------------------------------------------

class TestFindSeriesEndToEnd:
    def test_same_scene_different_resolution_forms_one_group_and_suggests_higher_res(self, tmp_path):
        fs = FakeFileSystem()
        base = _base_pattern_image((320, 240))
        small = base.resize((80, 60))

        fs.add_file("D:/Archive/full.jpg", _jpeg_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/small.jpg", _jpeg_bytes(small, "2021:01:01 10:00:02"))

        candidates = [
            _scanned("D:/Archive/full.jpg", size=len(fs._contents["D:/Archive/full.jpg"])),
            _scanned("D:/Archive/small.jpg", size=len(fs._contents["D:/Archive/small.jpg"])),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()

        assert result.errors == []
        assert len(result.groups) == 1
        group = result.groups[0]
        assert {m.path for m in group.members} == {"D:/Archive/full.jpg", "D:/Archive/small.jpg"}
        assert group.suggested_keeper == "D:/Archive/full.jpg"
        assert group.kind == series.KIND_BURST

    def test_visually_different_photos_close_in_time_do_not_group(self, tmp_path):
        fs = FakeFileSystem()
        pattern_a = _base_pattern_image()
        pattern_b = _checkerboard_image()

        fs.add_file("D:/Archive/a.jpg", _jpeg_bytes(pattern_a, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/b.jpg", _jpeg_bytes(pattern_b, "2021:01:01 10:00:01"))

        candidates = [
            _scanned("D:/Archive/a.jpg", size=len(fs._contents["D:/Archive/a.jpg"])),
            _scanned("D:/Archive/b.jpg", size=len(fs._contents["D:/Archive/b.jpg"])),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()

        assert result.groups == []

    def test_similar_photos_far_apart_in_time_do_not_group(self, tmp_path):
        fs = FakeFileSystem()
        base = _base_pattern_image((320, 240))
        small = base.resize((80, 60))

        fs.add_file("D:/Archive/full.jpg", _jpeg_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/small.jpg", _jpeg_bytes(small, "2021:01:01 10:20:00"))  # 20 min later

        candidates = [
            _scanned("D:/Archive/full.jpg", size=len(fs._contents["D:/Archive/full.jpg"])),
            _scanned("D:/Archive/small.jpg", size=len(fs._contents["D:/Archive/small.jpg"])),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache, window_seconds=300)
        finally:
            cache.close()

        assert result.groups == []

    def test_non_photo_extensions_are_ignored(self, tmp_path):
        fs = FakeFileSystem()
        fs.add_file("D:/Archive/clip.mp4", b"not-an-image")
        candidates = [_scanned("D:/Archive/clip.mp4", size=1)]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()
        assert result.groups == []
        assert result.errors == []
        assert fs.open_read_calls == []

    def test_undecodable_member_recorded_as_error_without_crashing(self, tmp_path):
        fs = FakeFileSystem()
        base = _base_pattern_image((320, 240))
        small = base.resize((80, 60))

        fs.add_file("D:/Archive/full.jpg", _jpeg_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/small.jpg", _jpeg_bytes(small, "2021:01:01 10:00:02"))
        fs.add_file("D:/Archive/broken.jpg", b"this is not a real jpeg")

        # broken.jpg has no EXIF and no filename pattern, so its timestamp
        # falls all the way to mtime -- give it one inside the same
        # cluster as full.jpg/small.jpg so it's actually attempted (and
        # fails to decode) instead of landing in an unrelated, dropped
        # singleton time cluster.
        broken_mtime_ns = int(datetime(2021, 1, 1, 10, 0, 1).timestamp() * 1_000_000_000)

        candidates = [
            _scanned("D:/Archive/full.jpg", size=len(fs._contents["D:/Archive/full.jpg"])),
            _scanned("D:/Archive/small.jpg", size=len(fs._contents["D:/Archive/small.jpg"])),
            _scanned("D:/Archive/broken.jpg", size=len(fs._contents["D:/Archive/broken.jpg"]), mtime_ns=broken_mtime_ns),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()

        assert "D:/Archive/broken.jpg" in result.errors
        assert len(result.groups) == 1
        assert {m.path for m in result.groups[0].members} == {"D:/Archive/full.jpg", "D:/Archive/small.jpg"}


class TestFindSeriesParallelWorkers:
    """find_series()'s max_workers
    parallelizes the two per-candidate decode phases (timestamp extraction,
    dHash) via ThreadPoolExecutor, but must produce BYTE-FOR-BYTE the same
    SeriesResult as the sequential path (max_workers<=1) for the same
    input -- see _dhash_for_cluster_parallel()'s own docstring for why this
    is a hard requirement, not just a nice-to-have."""

    def _three_member_cluster_fs_and_candidates(self, tmp_path):
        fs = FakeFileSystem()
        base = _base_pattern_image((320, 240))
        small = base.resize((80, 60))
        other_scene = _checkerboard_image()

        fs.add_file("D:/Archive/full.jpg", _jpeg_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/small.jpg", _jpeg_bytes(small, "2021:01:01 10:00:02"))
        fs.add_file("D:/Archive/other.jpg", _jpeg_bytes(other_scene, "2021:01:01 10:00:04"))

        candidates = [
            _scanned("D:/Archive/full.jpg", size=len(fs._contents["D:/Archive/full.jpg"])),
            _scanned("D:/Archive/small.jpg", size=len(fs._contents["D:/Archive/small.jpg"])),
            _scanned("D:/Archive/other.jpg", size=len(fs._contents["D:/Archive/other.jpg"])),
        ]
        return fs, candidates

    def _result_shape(self, result: series.SeriesResult):
        # A plain dict/tuple projection, comparable with == regardless of
        # SeriesGroup/SeriesMember's own field order or object identity.
        return (
            sorted(result.errors),
            [
                (
                    g.kind, round(g.max_gap_seconds, 6), g.suggested_keeper,
                    tuple(sorted(m.path for m in g.members)),
                )
                for g in result.groups
            ],
        )

    def test_parallel_and_sequential_agree_on_a_fresh_cache(self, tmp_path):
        fs, candidates = self._three_member_cluster_fs_and_candidates(tmp_path)
        cache_seq = Cache(str(tmp_path / "seq.sqlite3"))
        cache_par = Cache(str(tmp_path / "par.sqlite3"))
        try:
            sequential = series.find_series(fs, candidates, cache_seq, max_workers=1)
            parallel = series.find_series(fs, candidates, cache_par, max_workers=4)
        finally:
            cache_seq.close()
            cache_par.close()

        assert self._result_shape(sequential) == self._result_shape(parallel)
        assert len(parallel.groups) == 1  # sanity: the fixture actually forms a group

    def test_parallel_and_sequential_agree_on_a_warm_cache(self, tmp_path):
        # Second pass over an ALREADY-cached dhash column exercises the
        # cache-hit branch of _dhash_for_cluster_parallel() (as opposed to
        # the fresh-cache test above, which only exercises cache misses).
        fs, candidates = self._three_member_cluster_fs_and_candidates(tmp_path)
        db_path = str(tmp_path / "warm.sqlite3")
        cache = Cache(db_path)
        try:
            series.find_series(fs, candidates, cache, max_workers=4)
        finally:
            cache.close()

        cache_seq = Cache(db_path)
        cache_par = Cache(db_path)
        try:
            sequential = series.find_series(fs, candidates, cache_seq, max_workers=1)
            parallel = series.find_series(fs, candidates, cache_par, max_workers=4)
        finally:
            cache_seq.close()
            cache_par.close()

        assert self._result_shape(sequential) == self._result_shape(parallel)

    def test_progress_counter_reaches_done_equals_total(self, tmp_path):
        fs, candidates = self._three_member_cluster_fs_and_candidates(tmp_path)
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        counter = ProgressCounter()
        try:
            series.find_series(fs, candidates, cache, max_workers=4, progress=counter)
        finally:
            cache.close()

        done, total = counter.snapshot()
        # 3 candidates timestamped (phase 1) + 3 cluster members dhashed
        # (phase 2, all 3 land in the one cluster the fixture forms).
        assert total == 6
        assert done == 6

    def test_progress_counter_set_even_with_no_candidates(self, tmp_path):
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        counter = ProgressCounter()
        try:
            series.find_series(FakeFileSystem(), [], cache, progress=counter)
        finally:
            cache.close()
        assert counter.snapshot() == (0, 0)

    def test_default_max_workers_matches_progress_default_workers(self):
        import inspect
        sig = inspect.signature(series.find_series)
        assert sig.parameters["max_workers"].default == DEFAULT_WORKERS


@_heif_available
class TestFindSeriesEndToEndHeic:
    """The "regula oryginalu -- HEIC kontra JPG" rule, exercised for
    real: before thumbnails.py registered pillow-heif's HEIF opener,
    thumbnails.kind() returned None for ".heic", so find_series()'s own
    `thumbnails.kind(c.path) == "image"` candidate filter (see
    find_series()'s docstring/comment) silently dropped every HEIC file --
    _select_suggested_keeper()'s original-format rule could only ever be
    unit-tested directly against hand-built SeriesMember objects (see
    TestSelectSuggestedKeeper above), never actually reached via a real
    HEIC file flowing through find_series() end-to-end. These tests are
    that missing end-to-end path, gated on pillow-heif actually being
    installed.
    """

    def test_heic_source_beats_converted_jpg_as_suggested_keeper(self, tmp_path):
        # Same base image encoded to both formats -- pillow-heif's lossy
        # HEIF compression at this size still round-trips with dhash
        # distance 0 from the JPEG re-encode (verified empirically), so
        # the pair reliably lands in one similarity group without needing
        # a hand-tuned hamming_threshold.
        base = _base_pattern_image((320, 240))

        fs = FakeFileSystem()
        fs.add_file("D:/Archive/IMG_1234.heic", _heic_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/IMG_1234.jpg", _jpeg_bytes(base, "2021:01:01 10:00:02"))

        candidates = [
            _scanned("D:/Archive/IMG_1234.heic", size=len(fs._contents["D:/Archive/IMG_1234.heic"])),
            _scanned("D:/Archive/IMG_1234.jpg", size=len(fs._contents["D:/Archive/IMG_1234.jpg"])),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()

        assert result.errors == []
        assert len(result.groups) == 1
        group = result.groups[0]
        assert {m.path for m in group.members} == {"D:/Archive/IMG_1234.heic", "D:/Archive/IMG_1234.jpg"}
        # The real archive's actual HEIC+JPG pairs have the JPG as the
        # LARGER file at the same resolution ("po rozmiarze wskazalby
        # JPG") -- confirming that here (rather than
        # forcing the HEIC to also be the biggest file) proves the
        # original-format rule, not file size, is what decided the keeper.
        heic_member = next(m for m in group.members if m.path == "D:/Archive/IMG_1234.heic")
        jpg_member = next(m for m in group.members if m.path == "D:/Archive/IMG_1234.jpg")
        assert jpg_member.size > heic_member.size
        assert group.suggested_keeper == "D:/Archive/IMG_1234.heic"
        # Real decoded values (not mocked/hand-built), proving the HEIC
        # file genuinely reached Image.open() via find_series()'s own
        # pipeline rather than being skipped by the kind() gate.
        assert heic_member.width == 320 and heic_member.height == 240
        assert heic_member.has_exif is True

    def test_two_similar_heic_files_form_a_group_without_any_jpg(self, tmp_path):
        # A HEIC-only pair (no converted-copy sibling at all) must still
        # cluster and group on its own -- proves HEIC candidates flow
        # through the ordinary (non-original-rule) ranking path too.
        base = _base_pattern_image((320, 240))
        smaller = base.resize((80, 60))

        fs = FakeFileSystem()
        fs.add_file("D:/Archive/full.heic", _heic_bytes(base, "2021:01:01 10:00:00"))
        fs.add_file("D:/Archive/small.heic", _heic_bytes(smaller, "2021:01:01 10:00:02"))

        candidates = [
            _scanned("D:/Archive/full.heic", size=len(fs._contents["D:/Archive/full.heic"])),
            _scanned("D:/Archive/small.heic", size=len(fs._contents["D:/Archive/small.heic"])),
        ]
        cache = Cache(str(tmp_path / "cache.sqlite3"))
        try:
            result = series.find_series(fs, candidates, cache)
        finally:
            cache.close()

        assert result.errors == []
        assert len(result.groups) == 1
        assert {m.path for m in result.groups[0].members} == {"D:/Archive/full.heic", "D:/Archive/small.heic"}
        assert result.groups[0].suggested_keeper == "D:/Archive/full.heic"  # higher resolution wins


# --- write_series_csv / format_series_summary ----------------------------------

def _sample_result():
    keeper = _member("D:/Archive/full.jpg", width=320, height=240, sharpness=12.3, size=50000)
    discard = _member("D:/Archive/small.jpg", width=80, height=60, sharpness=1.1, size=5000)
    group = series.SeriesGroup(
        members=(keeper, discard), suggested_keeper=keeper.path,
        max_gap_seconds=2.0, kind=series.KIND_BURST,
    )
    return series.SeriesResult(groups=[group], errors=["D:/Archive/broken.jpg"])


class TestWriteSeriesCsv:
    def test_writes_header_and_one_row_per_member(self, tmp_path):
        out_path = str(tmp_path / "raport_serie.csv")
        series.write_series_csv(out_path, _sample_result())

        with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))

        assert rows[0] == list(series._CSV_COLUMNS)
        assert len(rows) == 3  # header + 2 members
        paths = {row[3] for row in rows[1:]}
        assert paths == {"D:/Archive/full.jpg", "D:/Archive/small.jpg"}
        roles = {row[3]: row[2] for row in rows[1:]}
        assert roles["D:/Archive/full.jpg"] == series.ROLE_SUGGESTED_KEEPER
        assert roles["D:/Archive/small.jpg"] == series.ROLE_SUGGESTED_DISCARD

    def test_formula_leading_char_in_path_is_escaped(self, tmp_path):
        # A full archive path always starts with a drive letter, but the
        # guard operates on whatever ends up in the `path` cell -- exercise
        # it directly with a bare filename that starts with a trigger char
        # (mirrors test_report.py's TestCsvFormulaInjectionGuard).
        keeper = _member("=cmd.jpg", width=100, height=100)
        group = series.SeriesGroup(
            members=(keeper,), suggested_keeper=keeper.path, max_gap_seconds=0.0, kind=series.KIND_BURST,
        )
        out_path = str(tmp_path / "raport_serie.csv")
        series.write_series_csv(out_path, series.SeriesResult(groups=[group]))

        with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))
        assert rows[1][3] == "'=cmd.jpg"


class TestFormatSeriesSummary:
    def test_includes_group_count_and_keeper_path(self):
        text = series.format_series_summary(_sample_result())
        assert "Groups: 1" in text
        assert "full.jpg" in text
        assert "Decode errors: 1" in text

    def test_empty_result_does_not_raise(self):
        text = series.format_series_summary(series.SeriesResult())
        assert "Groups: 0" in text


class TestSkipIdentity:
    """The stable identity of a skipped group:
    "jezeli zaznacze pomin to zapisz gdzies w cache lub bazie zeby nie
    pokazywac znowu nawet przy nastepnym uruchomieniu"."""

    def test_normalization_is_order_separator_and_case_insensitive(self):
        a = series.normalize_member_paths([r"D:\Foto\B.JPG", r"D:\Foto\a.jpg"])
        b = series.normalize_member_paths(["d:/foto/a.jpg", "d:/foto/b.jpg"])
        assert a == b == ("d:/foto/a.jpg", "d:/foto/b.jpg")

    def test_normalization_drops_duplicates(self):
        assert series.normalize_member_paths(
            ["D:/A/1.jpg", "d:/a/1.JPG"]
        ) == ("d:/a/1.jpg",)

    def test_key_is_stable_across_spelling_and_order(self):
        # The point of the whole design: the SAME photos must produce the
        # same key on a later run, whatever order find_series() happens to
        # return them in and however the drive letter was typed.
        assert series.skip_key([r"D:\Foto\b.jpg", "d:/FOTO/a.jpg"]) == \
            series.skip_key(["d:/foto/a.jpg", "d:/foto/b.jpg"])

    def test_key_differs_for_different_members(self):
        assert series.skip_key(["d:/a/1.jpg"]) != series.skip_key(["d:/a/2.jpg"])

    def test_exact_same_group_stays_skipped(self):
        stored = [frozenset(series.normalize_member_paths(["d:/a/1.jpg", "d:/a/2.jpg"]))]
        assert series.group_is_skipped([r"D:\A\1.jpg", r"D:\A\2.jpg"], stored) is True

    def test_a_shrunk_group_stays_skipped(self):
        # One member was deleted (or moved to !REMOVE by an apply on another
        # screen). The user's verdict was about the photos that remain too,
        # so re-showing the leftovers would be exactly the nagging this
        # feature removes.
        stored = [frozenset(series.normalize_member_paths(
            ["d:/a/1.jpg", "d:/a/2.jpg", "d:/a/3.jpg"]
        ))]
        assert series.group_is_skipped(["d:/a/1.jpg", "d:/a/2.jpg"], stored) is True

    def test_a_grown_group_comes_back(self):
        # A new photo matched into the same series -- there is now something
        # in this group the user has never looked at, and hiding that is the
        # one way this feature could lose a file.
        stored = [frozenset(series.normalize_member_paths(["d:/a/1.jpg", "d:/a/2.jpg"]))]
        assert series.group_is_skipped(
            ["d:/a/1.jpg", "d:/a/2.jpg", "d:/a/9.jpg"], stored
        ) is False

    def test_an_unrelated_group_is_not_skipped(self):
        stored = [frozenset(series.normalize_member_paths(["d:/a/1.jpg", "d:/a/2.jpg"]))]
        assert series.group_is_skipped(["d:/b/1.jpg", "d:/b/2.jpg"], stored) is False

    def test_no_stored_skips_means_nothing_is_skipped(self):
        assert series.group_is_skipped(["d:/a/1.jpg"], []) is False

    def test_an_empty_group_is_never_skipped(self):
        # Guards the degenerate case: the empty set is a subset of every
        # stored set, so without the explicit check an unknown/empty group
        # would report itself as skipped by ANY stored decision.
        stored = [frozenset(series.normalize_member_paths(["d:/a/1.jpg"]))]
        assert series.group_is_skipped([], stored) is False
