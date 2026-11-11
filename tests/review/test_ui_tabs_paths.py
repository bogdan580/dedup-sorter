"""Served SPA: tabs, path-segment highlighting, notes accordion, dev mode, layout."""
from __future__ import annotations

import json
import re

from .helpers import (
    _get,
)


# --- per-group path segment highlighting ---------------------------------
#
# Same constraint as the pagination block above (TestIndexHtmlPaginationAnd
# Columns): this module ships one static HTML page with an embedded
# <script>, and there is still no browser/JS engine anywhere in this
# repo's test suite -- confirmed again here (playwright is pip-installed
# for unrelated reasons, but no test in this file drives a real browser,
# and this project deliberately avoids adding that as a new dependency for
# this step). So the DOM a browser would actually build (which spans end
# up with which CSS class) cannot be executed and asserted against from
# this plain-HTTP test client. These tests instead pin down, at the layers
# that genuinely are testable here:
#   - GET /api/state hands the client the real, unmodified `path` strings
#     for every scenario in the acceptance list, including the
#     "&"/"<" case -- proving the server never HTML-escapes them (plain
#     json.dumps doesn't, and it must not have to: rendering is
#     textContent/createTextNode-only, pinned below);
#   - the served JS implements the path-highlighting rule exactly: segment-by-segment
#     comparison from the left across the WHOLE group (keeper + every
#     discarded entry) at once, not just a pair; the shared prefix is
#     dimmed and only the true divergence is marked; the filename segment
#     is excluded from the directory comparison entirely; and a group
#     with identical directories yields the same "nothing to highlight"
#     result (highlightFrom == every path's own directory count) for
#     every member;
#   - path content is built exclusively via textContent/createTextNode --
#     never innerHTML -- matching the one pre-existing innerHTML use in
#     this file (the unrelated `screen.innerHTML = ""` clear, which never
#     carries untrusted data).
class TestGroupPathSegmentHighlighting:
    def _html(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _function_body(self, html, name):
        match = re.search(
            r'function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}' % re.escape(name),
            html, re.DOTALL,
        )
        assert match, f"expected a function named {name}()"
        return match.group(1)

    # -- raw material: /api/state must hand over real, unescaped paths ----

    def test_api_state_serves_the_four_scenario_paths_unmodified(self, running_server_path_scenarios):
        httpd, state = running_server_path_scenarios
        status, body, _ = _get(state.port, "/api/state")
        assert status == 200
        groups = {g["group_id"]: g for g in json.loads(body)["groups"]}

        assert groups["1"]["keeper"]["path"] == "D:/Archive/2019/Lato/IMG.bin"
        assert groups["1"]["discarded"][0]["path"] == "D:/Archive/2019/Lato_kopia/IMG.bin"

        assert groups["2"]["keeper"]["path"] == "D:/Archive/A/x.bin"
        discarded_paths_g2 = {d["path"] for d in groups["2"]["discarded"]}
        assert discarded_paths_g2 == {"D:/Archive/B/x.bin", "D:/Archive/C/x.bin"}

        assert groups["3"]["keeper"]["path"] == "D:/Archive/2020/a.bin"
        assert groups["3"]["discarded"][0]["path"] == "D:/Archive/2020/b.bin"

        assert groups["4"]["keeper"]["path"] == "D:/Archive/A&B/<x>/img.bin"
        assert groups["4"]["discarded"][0]["path"] == "D:/Archive/A&B/other/img.bin"

    def test_api_state_does_not_html_escape_ampersand_or_angle_bracket(self, running_server_path_scenarios):
        # A naive server-side html.escape() on the path (or a client that
        # ever used innerHTML) would turn "&" into "&amp;" and "<" into
        # "&lt;" -- assert the raw bytes on the wire never do that, so the
        # only remaining thing to pin is that the client renders this
        # verbatim string via textContent (done below).
        httpd, state = running_server_path_scenarios
        status, body, _ = _get(state.port, "/api/state")
        assert status == 200
        raw = body.decode("utf-8")
        assert "D:/Archive/A&B/<x>/img.bin" in raw
        assert "&amp;" not in raw
        assert "&lt;" not in raw

    # -- rendering is textContent/createTextNode only, never innerHTML ----

    def test_path_rendering_never_uses_innerhtml(self, running_server):
        # The only actual innerHTML USE (as opposed to the word appearing
        # in a comment, like the one right above splitPathSegments()
        # explaining why it's avoided) in the whole served page is a bare
        # `screen.innerHTML = ""` clear, which carries no path/entry data
        # at all -- if any assignment ever carries more than an empty
        # string, something new started assigning real content via
        # innerHTML, which is exactly the kind of use that must be avoided
        # for a value that comes off disk (see docs/technical/security.md).
        #
        # series/events (renderSeriesScreen()/
        # renderEventsScreen()) each clear their own screen the same way
        # dup-screen always has, so this now allows MULTIPLE such clears
        # (one per screen) instead of pinning an exact count/variable name
        # -- but every single one of them must still be a bare clear.
        html = self._html(running_server)
        assignments = re.findall(r'[\w.\[\]"]+\.innerHTML\s*=[^\n;]*', html)
        assert len(assignments) >= 3, (
            "expected at least the three screen clears (dup/series/events)"
        )
        for assignment in assignments:
            assert assignment.rstrip().endswith('.innerHTML = ""'), (
                f"found an innerHTML assignment that is not a bare clear: {assignment!r}"
            )

    def test_make_card_sets_every_path_segment_via_textcontent(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert "path.textContent = entry.path" not in body, (
            "the old single-blob assignment must be gone -- the path is "
            "now built from per-segment spans"
        )
        assert re.search(r'pathInfo\.segments\.forEach\(\s*\(seg,\s*i\)\s*=>\s*\{', body)
        assert "span.textContent = seg;" in body
        assert "path.appendChild(span);" in body

    # -- compare segment-by-segment from the left, across the group -------

    def test_split_path_segments_derives_segments_and_separator_from_the_path(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "splitPathSegments")
        assert re.search(r'const\s+sep\s*=\s*sepMatch\s*\?\s*sepMatch\[0\]\s*:\s*"\\\\"\s*;', body), (
            "separator must come from the path itself (first \\ or / found), "
            "not be hardcoded independently of what's actually in entry.path"
        )
        assert re.search(
            r'path\.split\(\s*/\[\\\\/\]\+/\s*\)\.filter\(\s*\(?s\)?\s*=>\s*s\s*!==\s*""\s*\)',
            body,
        )

    def test_first_diverging_dir_index_compares_segment_by_segment_from_left(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "firstDivergingDirIndex")
        # Loop starts at i = 0 -- "from the left", not from the end or the
        # middle.
        assert re.search(r'for\s*\(\s*let\s+i\s*=\s*0\s*;\s*i\s*<\s*maxLen\s*;\s*i\s*\+=\s*1\s*\)', body)
        # Every path in the group is checked at position i, not just the
        # first two -- this is what makes the rule hold for N > 2 members.
        assert re.search(
            r'dirSegmentsPerPath\.every\(\s*\(?d\)?\s*=>\s*d\.length\s*>\s*i\s*&&\s*d\[i\]\s*===\s*first\s*\)',
            body,
        ), "must require every path to still have a segment at i AND match it -- a shorter path is a divergence too"
        assert re.search(r'if\s*\(\s*!allSame\s*\)\s*return\s+i\s*;', body)

    def test_first_diverging_dir_index_falls_back_to_max_len_when_everything_agrees(self, running_server):
        # If the loop never finds a disagreement (every group member has
        # identical directories), the function must return maxLen -- which
        # is, by construction, each path's own directory count, so no
        # segment index can ever be >= it. This is the whole mechanism
        # behind "identical directories -> nothing highlighted" (acceptance
        # criterion 3).
        html = self._html(running_server)
        body = self._function_body(html, "firstDivergingDirIndex")
        assert re.search(r'return\s+maxLen\s*;\s*\}?\s*$', body.strip())

    def test_compute_group_path_highlights_excludes_filename_from_comparison(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "computeGroupPathHighlights")
        # Directories are "everything except the last segment" -- the
        # filename never enters firstDivergingDirIndex().
        assert re.search(r'p\.segments\.slice\(\s*0\s*,\s*-1\s*\)', body)
        assert re.search(r'firstDivergingDirIndex\(\s*dirsPerPath\s*\)', body)
        # ONE highlightFrom computed for the whole group and reused for
        # every entry (not recomputed per-path) -- this is what guarantees
        # every card in a group agrees on the same divergence point, and
        # that identical-directory groups highlight nothing for anybody.
        assert re.search(
            r'return\s+parsed\.map\(\s*\(?p\)?\s*=>\s*\(\s*\{\s*segments:\s*p\.segments\s*,\s*'
            r'sep:\s*p\.sep\s*,\s*highlightFrom\s*\}\s*\)\s*\)\s*;',
            body,
        )

    # -- computed once per group, fed into every card in that group -------

    def test_render_computes_highlights_once_per_group_and_indexes_into_them(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "render")
        assert re.search(r'const\s+pathHighlights\s*=\s*computeGroupPathHighlights\(\s*entries\s*\)\s*;', body), (
            "must be computed from the group's OWN entries (keeper + all "
            "discarded), once per group -- not per card, and not from a "
            "server endpoint"
        )
        assert re.search(
            r'entries\.forEach\(\s*\(entry,\s*i\)\s*=>\s*\{\s*'
            r'row\.appendChild\(\s*makeCard\(\s*entry\s*,\s*entry\.id\s*===\s*sel\.keeperId\s*,\s*'
            r'g\.group_id\s*,\s*pathHighlights\[i\]\s*\)\s*\)\s*;',
            body,
        ), "each card must receive the highlight info for its OWN entry, by matching index"

    def test_path_highlighting_adds_no_new_fetch_endpoint(self, running_server):
        # The whole comparison runs client-side over data already in
        # state.data (loaded by the pre-existing fetch("/api/state")) --
        # path highlighting itself must not add a new endpoint or any new
        # server data. Series/events legitimately add two: GET /api/series
        # (literal, in refreshSeriesData() -- pollTabData() reaches
        # /api/series through a variable, `fetch(endpoint)`, so it was
        # never in this set) and POST /api/apply-series
        # (submitApplySeries()). The rotate button legitimately adds a
        # third: POST /api/thumb/rotate (rotateThumb()).
        html = self._html(running_server)
        endpoints = set(re.findall(r'fetch\(\s*"([^"]+)"', html))
        # The placement screen adds three more: GET /api/source-dirs (the
        # source-folder picker), GET /api/placement (literal prefix -- the
        # source is appended, so it shows up here with its query
        # separator), and POST /api/apply-placement (the op=move submit
        # path).
        # The Rescan button adds POST /api/rescan: the one thing the
        # client genuinely cannot do with data it already holds is find
        # out that the DISK changed.
        # The restore tab adds two more: GET /api/batches (loadBatches(),
        # the "Cofnij" tab's own list) and POST /api/restore
        # (submitRestore(), shared by that tab's per-row button and
        # appendResult()'s inline one). Thumbnail preview on that tab adds
        # the last one: GET /api/batch-entries (literal prefix -- batch_id
        # is appended, same shape as /api/placement?source= above),
        # fetched ONLY from toggleBatchEntries() on first expansion.
        # Persisted series skip adds POST /api/series-skip
        # (toggleSeriesSkip()): "pomin" became a decision stored in the
        # cache DB rather than a client-side flag, and the client
        # genuinely cannot write that itself -- the same justification
        # /api/rescan has.
        # Persisted placement skip adds POST /api/placement-skip
        # (togglePlacementSkip()): the same feature, extended to the
        # placement screen's own "pomin ta grupe".
        # The compress tab adds seven more, all of them genuinely
        # new capability, none reachable through data the client already
        # holds: GET /api/compress?source= (literal prefix, same shape as
        # /api/placement?source= -- refreshCompressData()), GET
        # /api/compress/encoders (ensureCompressEncoders(), the hardware/
        # calibration listing), GET /api/compress/jobs
        # (fetchCompressJobsOnce(), the async encode queue's own progress/
        # results poll -- pollCompressJobsLoop() reaches it through the
        # same literal, so it does not add a second entry), POST
        # /api/compress/skip (toggleCompressSkip(), the compress screen's
        # own "pomin"), POST /api/compress/cancel (cancelCompressQueue()),
        # POST /api/apply-compress (submitApplyCompress(), queueing)
        # and POST /api/apply-compress-remove (submitApplyCompressRemove(),
        # results panel).
        # docs/technical/components/compression-mechanism.md adds one more: POST /api/compress/calibrate
        # (submitCalibrate(), the "Zmierz" button) -- a real measurement
        # only the server can produce, not data the client already holds.
        assert endpoints == {
            "/api/state", "/api/open", "/api/apply", "/api/series", "/api/apply-series",
            "/api/thumb/rotate", "/api/source-dirs", "/api/placement?source=",
            "/api/apply-placement", "/api/rescan", "/api/batches", "/api/restore",
            "/api/batch-entries?batch_id=", "/api/dup-skip", "/api/series-skip", "/api/placement-skip",
            "/api/compress?source=", "/api/compress/encoders", "/api/compress/jobs",
            "/api/compress/skip", "/api/compress/cancel", "/api/apply-compress",
            "/api/apply-compress-remove", "/api/compress/calibrate",
        }

    # -- rendering: dimmed prefix, highlighted divergence, filename apart -

    def test_make_card_marks_dir_segments_diff_only_from_highlight_from_onward(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert re.search(
            r'span\.className\s*=\s*"path-segment path-segment-dir "\s*\+\s*'
            r'\(\s*i\s*>=\s*pathInfo\.highlightFrom\s*\?\s*"path-segment-diff"\s*:\s*"path-segment-common"\s*\)\s*;',
            body,
        ), "directory segments before highlightFrom get -common (dimmed); from highlightFrom onward get -diff"

    def test_make_card_filename_segment_is_always_its_own_class_never_diff(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert re.search(
            r'if\s*\(\s*i\s*===\s*lastIndex\s*\)\s*\{\s*'
            r'span\.className\s*=\s*"path-segment path-segment-file"\s*;\s*\}',
            body,
        ), "the filename (last segment) must short-circuit into its own class before the dir/diff branch even runs"

    def test_make_card_reason_suffix_is_a_separate_element_not_glued_to_filename(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert re.search(
            r'reasonSpan\.className\s*=\s*"path-reason"\s*;\s*'
            r'reasonSpan\.textContent\s*=\s*"\s*\(\s*"\s*\+\s*'
            r'\(\s*entry\.reason\s*\|\|\s*"user-selected"\s*\)\s*\+\s*"\)"\s*;',
            body,
        )
        assert "path.appendChild(reasonSpan);" in body
        # Still only rendered for discarded entries, exactly like before --
        # this additionally guards it behind !options.readOnly
        # (series/events entries have no "reason" field at all, and their
        # cards must show none of dup's own selection-related UI).
        assert re.search(
            r'if\s*\(\s*!isKeeper\s*&&\s*!options\.readOnly\s*\)\s*\{\s*const\s+reasonSpan', body,
        )

    # -- CSS: new classes, legible on both plain and .card.keeper cards ---

    def test_css_defines_common_and_diff_path_segment_classes(self, running_server):
        html = self._html(running_server)
        assert ".path-segment-common { opacity: 0.55; }" in html
        assert re.search(
            r'\.card \.path-segment-dir\.path-segment-diff\s*\{[^}]*background:\s*Mark\s*;[^}]*color:\s*MarkText\s*;',
            html,
        ), "diff segments must use the Mark/MarkText system-color pair (theme-aware, like .tab.active's Highlight pair)"

    def test_css_diff_class_forces_full_opacity_over_the_dimmed_default(self, running_server):
        # .path-segment-diff and .path-segment-common are never applied to
        # the same span (see the ternary in makeCard above), but the diff
        # rule still explicitly resets opacity so it can never be
        # accidentally dimmed by a future shared ancestor rule -- keeping
        # it readable on top of .card.keeper's own outline.
        html = self._html(running_server)
        match = re.search(r'\.card \.path-segment-dir\.path-segment-diff\s*\{([^}]*)\}', html)
        assert match, "expected a dedicated rule for .path-segment-dir.path-segment-diff"
        assert "opacity: 1" in match.group(1)


# --- lazy series/events computation + registry split --------------------
#
# GET /api/series and GET /api/events start a background computation (at
# most once) and return {"status": "computing"|"ready"|"error", "groups",
# "error", "notes"} without ever blocking do_GET(). The single most
# important test in this class is the registry-separation one: a series/
# events file id must be resolvable by POST /api/open but MUST NOT be
# resolvable by POST /api/apply (open_registry is never handed to
# _validate_selections()) -- otherwise applier.apply_batch() could move a
# file the user only ever saw on a read-only tab.

# --- three switchable tabs (dup/series/events) --------------------------
#
# Series/events were previously disabled placeholder tabs; this step wires
# them up for real, reusing makeCard()/computeGroupPathHighlights() in a
# new read-only mode (options.readOnly) rather than copying the renderer,
# and polling the already-implemented GET /api/series / GET /api/events
# lazy-computation endpoints. Same JS-source-pinning approach as the rest
# of this file (no browser/JS execution harness in this repo's test
# suite): assertions are regex/substring checks against the served page
# source, mirroring TestIndexHtmlPaginationAndColumns/
# TestGroupPathSegmentHighlighting above.
class TestThreeTabsFrontendRunda2Krok4:
    def _html(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _function_body(self, html, name):
        match = re.search(
            r'function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}' % re.escape(name),
            html, re.DOTALL,
        )
        assert match, f"expected a function named {name}()"
        return match.group(1)

    # -- A: three real screens, no more disabled placeholder tabs ----------

    def test_no_tab_has_disabled_attribute(self, running_server):
        html = self._html(running_server)
        tabs_match = re.search(r'<div class="tabs">(.*?)</div>', html, re.DOTALL)
        assert tabs_match, "expected a .tabs container"
        assert "disabled" not in tabs_match.group(1)

    def test_five_tab_buttons_present_without_phase_suffixes(self, running_server):
        # Both old internal phase-name suffixes are dropped from the tab
        # labels together -- series became executable, so its own suffix
        # no longer fits, and the other is dropped alongside it for visual
        # consistency even though events stays read-only. Renamed from
        # "...three_tab_buttons..." when the compress tab became
        # the fifth: dup/series/placement/compress/restore.
        html = self._html(running_server)
        assert 'data-tab="dup"' in html
        assert 'data-tab="series"' in html
        assert 'data-tab="placement"' in html
        assert 'data-tab="compress"' in html
        assert 'data-tab="restore"' in html
        assert "Exact duplicates" in html
        assert "Series and similar" in html
        assert "Grouping suggestions" in html
        assert "Compression suggestion" in html
        assert "(Faza 5)" not in html
        assert "(Faza 6)" not in html

    def test_all_five_screen_containers_exist(self, running_server):
        html = self._html(running_server)
        assert 'id="dup-screen"' in html
        assert 'id="series-screen"' in html
        assert 'id="placement-screen"' in html
        assert 'id="compress-screen"' in html
        assert 'id="restore-screen"' in html

    def test_apply_bar_and_page_controls_toggled_alongside_screens(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "updateScreenVisibility")
        for element_id in ("dup-screen", "series-screen", "placement-screen", "compress-screen", "top-apply-controls", "page-controls"):
            assert f'getElementById("{element_id}")' in body
        # "compress", like "restore", gets neither the shared apply bar nor
        # the pager -- it has its own action panel / results panel instead
        # (renderCompressScreen()'s own makeCompressActionPanel()/
        # makeCompressResultsPanel()).
        assert 'state.tab === "restore" || state.tab === "compress"' in body

    # -- B: tab switch handler must NEVER call loadState() ------------------
    #
    # loadState() calls initSelection(), which would reset state.selection
    # -- wiping every keeper swap/exclude/skip choice the user made on the
    # dup screen -- so switching tabs must reach render() without ever
    # going through loadState().

    def test_switch_tab_never_calls_load_state(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "switchTab")
        assert "loadState()" not in body
        assert "state.tab = tab;" in body
        assert "render();" in body

    def test_tab_buttons_wired_to_switch_tab_not_load_state(self, running_server):
        html = self._html(running_server)
        assert re.search(
            r'querySelectorAll\(\s*"\.tab"\s*\)\.forEach\(\s*\(btn\)\s*=>\s*\{\s*'
            r'btn\.addEventListener\(\s*"click"\s*,\s*\(\)\s*=>\s*switchTab\(btn\.dataset\.tab\)\s*\)\s*;',
            html,
        ), "expected every .tab button wired to switchTab(btn.dataset.tab), not a custom handler"

    # -- C: computing polls via setTimeout and stops at ready/error --------

    def test_poll_tab_data_reschedules_via_settimeout_only_while_computing(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "pollTabData")
        assert 'payload.status === "computing"' in body
        assert re.search(r'setTimeout\(\s*\(\)\s*=>\s*pollTabData\(tab\)\s*,\s*1000\s*\)', body), (
            "expected the computing branch to reschedule itself roughly 1s later"
        )

    def test_ensure_tab_data_starts_at_most_once_per_idle_resource(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "ensureTabData")
        assert re.search(r'tabData\[tab\]\.status\s*!==\s*"idle"', body), (
            "must not re-trigger a fetch for a resource that's already computing/ready/error"
        )

    def test_series_and_placement_screens_render_an_error_message(self, running_server):
        html = self._html(running_server)
        for name in ("renderSeriesScreen", "renderPlacementScreen"):
            body = self._function_body(html, name)
            assert 'entry.status === "error"' in body
            assert "entry.error" in body

    # -- D: an empty ready result is a message, never an empty grid --------

    def test_series_empty_ready_result_shows_a_message_not_an_empty_grid(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderSeriesScreen")
        assert "entry.groups.length === 0" in body
        assert 'i18n("js_ser_no_grp")' in body
        assert "makeStatusMessage(" in body

    def test_placement_empty_ready_result_shows_a_message_not_an_empty_grid(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderPlacementScreen")
        assert "all.length === 0" in body
        assert 'i18n("js_plac_no_sugg_dir")' in body
        assert "makeStatusMessage(" in body

    def test_make_status_message_reuses_the_existing_placeholder_screen_class(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeStatusMessage")
        assert 'div.className = "placeholder-screen";' in body

    def test_placement_screen_wires_pager_in_every_state(self, running_server):
        # Bug report: 182 sugestii at 50/page (4 pages), "Nastepna"
        # did nothing -- renderPlacementScreen() built its four states (error,
        # not-ready, empty-ready, ready-with-suggestions) but never called
        # either pager helper, so next/prev kept whatever disabled state the
        # previously rendered tab left them in and the indicator showed that
        # other tab's page count. Mirrors renderSeriesScreen()'s branches,
        # which already call these on every path.
        #
        # A later addition added a fourth branch -- "every suggestion
        # for this directory is skipped" -- mirroring renderSeriesScreen()'s
        # own all-skipped message, so the expected count grew from 3 to 4.
        html = self._html(running_server)
        body = self._function_body(html, "renderPlacementScreen")
        assert body.count("disablePagerControls()") == 4, (
            "expected the error, not-ready, empty-ready, and all-skipped "
            "branches to all disable the pager, same as renderSeriesScreen()"
        )
        assert "updatePagerControls(all)" in body, (
            "the ready branch must refresh next/prev/first/last and the "
            "'Strona X z Y' indicator for the CURRENT tab's own group count"
        )

    def test_placement_screen_updates_pager_before_paging(self, running_server):
        # updatePagerControls() clamps currentPage() into [1, pageCount]
        # before currentPageGroups() reads it -- called in the other order,
        # a stale page number from a bigger tab would slice past the end of
        # this tab's (possibly shorter) list.
        html = self._html(running_server)
        body = self._function_body(html, "renderPlacementScreen")
        assert body.index("updatePagerControls(all)") < body.index("currentPageGroups(all)")

    # -- E: read-only cards -- no checkboxes, click never moves the keeper --

    def test_placement_renderer_never_builds_a_per_file_exclude_checkbox(self, running_server):
        # The placement screen became executable, so unlike the old
        # read-only events screen it DOES have a per-suggestion "pomin".
        # It still must not grow a per-file "exclude" checkbox: multiKeep
        # card selection already expresses per-file inclusion,
        # and a second mechanism for the same thing is just confusing --
        # the same reasoning that kept it off the series screen.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "exclude-checkbox" not in body
        assert "skip-label" in body

    def test_series_renderer_has_skip_group_but_never_exclude_checkbox(self, running_server):
        # Series gained its own "pomin ta grupe" checkbox
        # (same apply-bar UX as the dup screen), but never a per-file
        # "pomin ten plik" checkbox -- keepIds already expresses
        # per-file inclusion/exclusion, so a second, separate mechanism for
        # the same thing would just be confusing.
        #
        # The checkbox itself lives in makeSeriesSkipControl(). It was
        # briefly placed TWICE per group (the bar's, and a double-size one in
        # the bottom-right corner); a later request removed the top-left
        # copy, leaving only the corner control -- and a later request still
        # moved that corner control back into the group bar (deliberately
        # reversing the earlier one; the "leave this alone" gesture needed to
        # sit with the rest of the group's controls). renderSeriesScreen() is
        # therefore asserted to place the control exactly once, inside
        # .group-bar; the .skip-group-checkbox class is asserted where it is
        # now built.
        html = self._html(running_server)
        body = self._function_body(html, "renderSeriesScreen")
        assert "exclude-checkbox" not in body
        assert body.count("makeSeriesSkipControl(") == 1, (
            "a series group must offer the 'pomin' checkbox exactly once, "
            "in the group bar only"
        )
        assert re.search(r'makeSeriesSkipControl\(g,\s*sel\)', body)
        assert "skip-corner" not in body
        assert re.search(r'groupBar\.appendChild\(makeSeriesSkipControl\(g,\s*sel\)\)', body), (
            "the skip control must be appended into .group-bar, not a separate corner element"
        )
        control = self._function_body(html, "makeSeriesSkipControl")
        assert "skip-group-checkbox" in control
        assert "exclude-checkbox" not in control

    def test_make_card_gained_an_options_parameter_with_a_readonly_flag(self, running_server):
        html = self._html(running_server)
        assert re.search(r'function\s+makeCard\([^)]*options[^)]*\)', html)
        body = self._function_body(html, "makeCard")
        assert "options.readOnly" in body

    def test_make_card_click_handler_is_skipped_entirely_when_readonly(self, running_server):
        # The guard gained `&& !options.multiKeep` (a series card's
        # click toggles keepIds membership, in its own branch below, never
        # this one) -- but a fully read-only (events) card must still get
        # no keeper-reassigning click listener at all.
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert re.search(
            r'if\s*\(\s*!options\.readOnly\s*&&\s*!options\.multiKeep\s*\)\s*\{.*?'
            r'card\.addEventListener\(\s*"click"',
            body, re.DOTALL,
        ), "a read-only card must never get a click listener that reassigns the keeper"

    def test_make_card_dblclick_still_opens_the_file_when_readonly(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        dblclick_match = re.search(
            r'card\.addEventListener\(\s*"dblclick"\s*,\s*\(\)\s*=>\s*\{(.*?)\}\s*\)\s*;\s*return\s+card\s*;',
            body, re.DOTALL,
        )
        assert dblclick_match, "expected a dblclick listener as the last thing makeCard() wires up"
        assert "openFile(entry.id)" in dblclick_match.group(1)

    def test_placement_passes_multikeep_and_a_toggle_callback_to_make_card(self, running_server):
        # Placement cards behave like series cards (click toggles
        # membership, selected cards get the border) but keep their own set,
        # so they pass an explicit onToggle rather than letting makeCard
        # reach into state.seriesSelection.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "multiKeep: true" in body
        assert "onToggle:" in body
        assert "state.placementSelection" in body

    def test_series_passes_multikeep_true_to_make_card(self, running_server):
        # Series is no longer readOnly -- it passes its own
        # multiKeep flag instead, so every kept card can show the .card.keeper
        # outline, not just a single server-suggested one.
        html = self._html(running_server)
        body = self._function_body(html, "renderSeriesScreen")
        assert re.search(r'makeCard\([^)]*\{\s*multiKeep:\s*true\s*\}\s*\)', body), (
            "renderSeriesScreen() must render its cards via makeCard(..., {multiKeep: true})"
        )
        assert not re.search(r'makeCard\([^)]*\{\s*readOnly:\s*true\s*\}\s*\)', body), (
            "renderSeriesScreen() must not pass readOnly:true any more"
        )

    # -- F: group headers, and events' clock-drift notes are surfaced ------

    def test_series_group_header_shows_kind_gap_and_file_count(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderSeriesScreen")
        assert "g.kind" in body
        assert "g.max_gap_seconds" in body
        assert "g.entries.length" in body

    def test_placement_header_shows_destination_and_candidates(self, running_server):
        # The header gained an editable destination and the
        # existing-folder candidate list.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "suggestion.suggested_folder" in body or "sel.dest" in body
        # The existence question is asked through destExistsOnDisk(), which
        # reads dest_exists -- NOT events.py's matched_existing_dir, whose
        # "the TOP-LEVEL folder exists" was rendered against the full path
        # and labelled `#Home\2022 High Mountains`, a directory not on
        # disk at all, "istniejacy katalog".
        assert "destExistsOnDisk(" in body
        helper = self._function_body(html, "destExistsOnDisk")
        assert "suggestion.dest_exists" in helper
        assert "matched_existing_dir" not in helper
        assert "suggestion.candidates" in body

    def test_the_bar_no_longer_repeats_the_match_or_the_counts(self, running_server):
        # "pasuje datami: ..." and
        # "-- zdjecia: N, wideo: N" removed. The date match is now the
        # value in the destination field and carries its reason inside the
        # picker's own group; the files are listed right below the bar.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "pasuje datami: " not in body
        assert "zdjecia: " not in body

    def test_date_matches_are_a_separate_group_at_the_top_of_the_picker(self, running_server):
        # Sort order alone does not say WHY
        # those folders come first, and the two kinds of candidate answer
        # different questions: one already holds photos from these very
        # days, the other merely shares a word with the source name.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "optgroup" in body
        assert 'i18n("js_matches_dates")' in body
        assert 'i18n("js_existing_dirs")' in body
        # Split on the same field the reason text comes from, so a
        # candidate can never land in the dated group without one.
        # (The list is now built for a suggestion with NO
        # candidates too -- the popular-folders group can be its only
        # content -- so both filters run over a defaulted array.)
        assert re.search(r'\(suggestion\.candidates \|\| \[\]\)\.filter\(\(c\) => c\.why\)', body)
        assert re.search(
            r'\(suggestion\.candidates \|\| \[\]\)\.filter\(\(c\) => !c\.why && !c\.outside\)',
            body,
        )
        # Folders outside the suggestion's top level are carried
        # too, in their own group at the bottom -- see
        # TestDestCandidatesReachEveryFolder for why they must be there.
        assert 'i18n("js_other_archive_dirs")' in body
        assert body.index('i18n("js_existing_dirs")') < body.index(
            'i18n("js_other_archive_dirs")')
        assert body.index('i18n("js_matches_dates")') < body.index('i18n("js_existing_dirs")')

    # -- popular destinations ------------------------------------------------

    def test_popular_dirs_are_a_group_between_date_matches_and_existing_dirs(
        self, running_server,
    ):
        # The folders this session keeps landing in, offered
        # BEFORE the plain existing-directory list but never before a date
        # match -- a folder already holding photos from these very days is
        # an answer about THIS group, which outranks a habit of the run.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert 'i18n("js_popular_dirs")' in body
        assert (body.index('i18n("js_matches_dates")')
                < body.index('i18n("js_popular_dirs")')
                < body.index('i18n("js_existing_dirs")'))
        # the group is filled from the usage counter, not from the
        # server-sent candidates -- a popular folder may be one this
        # suggestion has no candidate for at all, which is the whole point
        assert re.search(
            r'addGroup\(i18n\("js_popular_dirs"\),\s*popularDests\(POPULAR_DEST_LIMIT\)',
            body,
        )

    def test_popular_dirs_are_the_top_five_by_count_then_recency(self, running_server):
        html = self._html(running_server)
        assert re.search(r'const\s+POPULAR_DEST_LIMIT\s*=\s*5\s*;', html)
        body = self._function_body(html, "popularDests")
        # count first, most-recent as the tiebreak: at the start of a
        # session every count is 1, so without the tiebreak the order
        # would carry no information at all
        assert re.search(
            r'sort\(\(a,\s*b\)\s*=>\s*\(b\.count\s*-\s*a\.count\)\s*\|\|\s*'
            r'\(b\.seq\s*-\s*a\.seq\)\)', body,
        )
        assert "slice(0, limit)" in body

    def test_only_destinations_the_user_sets_are_counted(self, running_server):
        # Picking from the list, "Nowy katalog" and typing all count; the
        # engine's pre-filled proposal does NOT. Counting the prefill would
        # fill the list with folders the user never chose.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "recordDestUsage(picker.value)" in body
        assert "recordDestUsage(suggestion.suggested_folder)" in body
        assert re.search(
            r'destInput\.addEventListener\("change",\s*\(\)\s*=>\s*\{\s*'
            r'recordDestUsage\(destInput\.value\);', body,
        ), "a typed destination counts once per edit, on change -- not per keystroke"
        seeding = self._function_body(html, "ensurePlacementSelection")
        assert "recordDestUsage" not in seeding

    def test_counter_keys_on_the_folder_and_ignores_the_empty_destination(
        self, running_server,
    ):
        html = self._html(running_server)
        body = self._function_body(html, "recordDestUsage")
        assert re.search(r'if\s*\(!rel\)\s*return;', body), (
            "an emptied destination field must not be counted as a choice"
        )
        assert "destUsage.seq += 1" in body
        assert re.search(r'count:\s*\(prev\s*\?\s*prev\.count\s*:\s*0\)\s*\+\s*1', body)

    def test_picker_is_offered_and_refilled_for_the_popular_list(self, running_server):
        # Two consequences of the list being session state rather than
        # server data: a suggestion with no candidates of its own still
        # gets a picker once the session has popular folders (that is the
        # group whose destination would otherwise be typed by hand), and
        # the options are rebuilt when the list is opened, so a folder set
        # two groups ago is there without waiting for a re-render.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert re.search(
            r'\|\|\s*popularDests\(POPULAR_DEST_LIMIT\)\.length\s*>\s*0', body,
        )
        assert 'picker.addEventListener("focus", fillPicker)' in body
        assert 'picker.addEventListener("mousedown", fillPicker)' in body

    def test_destination_is_prefilled_with_the_date_matched_folder(self, running_server):
        # When an existing folder already holds
        # photos from these same days, it IS the destination -- not a hint
        # beside a different one. Filing a trip next to the folder holding
        # half of it is the outcome this screen exists to prevent.
        html = self._html(running_server)
        body = self._function_body(html, "ensurePlacementSelection")
        assert "bestDateMatch(suggestion)" in body
        assert re.search(
            r'dest:\s*match\s*\?\s*match\.rel\s*:\s*suggestion\.suggested_folder', body,
        ), "the date match must win over the engine's invented folder name"

    def test_new_directory_button_restores_the_suggested_name(self, running_server):
        # The way back once the field is pre-filled with an existing folder.
        # It fills in the engine's "RRRR Miejsce" name rather than blanking
        # the field -- typing that convention by hand is what this screen
        # exists to save.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert 'newDirBtn.textContent = i18n("js_new_folder")' in body
        assert re.search(
            r'newDirBtn\.addEventListener\("click",\s*\(\)\s*=>\s*\{\s*'
            r'sel\.dest\s*=\s*suggestion\.suggested_folder;\s*'
            r'destInput\.value\s*=\s*suggestion\.suggested_folder;',
            body,
        )

    def test_current_directory_button_sets_the_source_directory_as_destination(self, running_server):
        # "Biezacy katalog" answers "leave these where I am sorting them" in
        # one click: the destination becomes the directory selected in the
        # "Katalog zrodlowy" picker. That value is a `rel` the server itself
        # handed out in /api/source-dirs, never a path composed in the client.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert 'sourceDirBtn.textContent = i18n("js_current_dir")' in body
        assert re.search(
            r'sourceDirBtn\.addEventListener\("click",\s*\(\)\s*=>\s*\{\s*'
            r'sel\.dest\s*=\s*state\.placementSource;\s*'
            r'destInput\.value\s*=\s*state\.placementSource;',
            body,
        )
        # and it sits in the destination row, next to the other two ways of
        # setting a destination -- not in the group bar
        assert "destRow.appendChild(sourceDirBtn)" in body
        assert "bar.appendChild(sourceDirBtn)" not in body

    def test_dest_state_tracks_the_current_destination_not_the_original_proposal(self, running_server):
        # The field is editable AND pre-filled, so a state pinned to the
        # engine's original proposal would drift into showing "new folder"
        # while the user is about to move photos into a folder already full
        # of them -- the same class of lie as the matched_existing_dir one
        # this replaced.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert "destExistsOnDisk(suggestion, sel.dest)" in body
        # and it must be recomputed on every way the destination can change
        assert body.count("refreshDestState()") >= 4

    def test_dest_state_is_shown_as_colour_on_the_label_and_the_field(self, running_server):
        # Replaces the "istniejacy katalog" /
        # "NOWY katalog" words next to the field. Green = already on disk,
        # orange = about to be created, carried by the label text and the
        # input's border so the answer sits on the control it describes.
        html = self._html(running_server)
        body = self._function_body(html, "makeSuggestionBlock")
        assert 'destLabel.className = "dest-label"' in body
        for el in ("destLabel", "destInput"):
            assert el + '.classList.toggle("dest-existing", exists)' in body
            assert el + '.classList.toggle("dest-new", !exists)' in body
        # the words themselves are gone from the block
        assert "istniejacy katalog" not in body
        assert "NOWY katalog" not in body
        # both states carry the same border width, so retyping a
        # destination cannot make the row jump
        assert ".dest-label.dest-existing { color: #2e8b57" in html
        assert ".dest-label.dest-new { color: #c26a00" in html
        assert ".dest-input.dest-existing { border: 2px solid #2e8b57; }" in html
        assert ".dest-input.dest-new { border: 2px solid #c26a00; }" in html

    def test_skip_toggle_appears_in_all_three_executable_group_bars(self, running_server):
        # The skip control used to be a per-screen bottom-right corner
        # (series, placement) or lived inline in render() (dup) with no
        # shared look. All three now append it into their own .group-bar
        # element, using the shared .skip-toggle class.
        html = self._html(running_server)
        render_body = self._function_body(html, "render")
        assert re.search(r'groupBar\.appendChild\(makeDupSkipControl\(g,\s*sel\)\)', render_body)
        dup_control = self._function_body(html, "makeDupSkipControl")
        assert re.search(r'label\.className\s*=\s*"skip-toggle"', dup_control)

        series_body = self._function_body(html, "renderSeriesScreen")
        assert re.search(r'groupBar\.appendChild\(makeSeriesSkipControl\(g,\s*sel\)\)', series_body)
        control = self._function_body(html, "makeSeriesSkipControl")
        assert re.search(r'label\.className\s*=\s*"skip-toggle"', control)

        block = self._function_body(html, "makeSuggestionBlock")
        assert re.search(r'skipLabel\.className\s*=\s*"skip-toggle[^"]*"', block)
        assert "bar.appendChild(skipLabel)" in block

    def test_dest_row_holds_the_destination_controls_not_the_group_bar(self, running_server):
        # The placement bar got too crowded (badge, label, input, picker,
        # button all in one flex row). The destination controls now live in
        # their own .dest-row below .group-bar; the group bar itself keeps
        # only the badge/rotate/skip cluster shared with dup and series.
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert re.search(r'\.dest-row\s*\{[^}]*display:\s*flex[^}]*gap:\s*var\(--sp-2\)', css)

        block = self._function_body(html, "makeSuggestionBlock")
        assert 'destRow.className = "dest-row"' in block
        assert "destRow.appendChild(destLabel)" in block
        assert "bar.appendChild(destLabel)" not in block
        assert "destRow.appendChild(picker)" in block
        assert "bar.appendChild(picker)" not in block
        assert "block.appendChild(destRow)" in block

    def test_placement_screen_reads_camera_drift_warnings_and_suspicious_notes(self, running_server):
        # Omitting these from the UI would hide from the user
        # that some photos may have landed in the wrong event because of a
        # camera clock that had drifted. Carried over from the old events
        # screen and more important here, not less: this screen proposes a
        # DESTINATION, so a drifted photo is about to be filed under someone
        # else's trip.
        html = self._html(running_server)
        body = self._function_body(html, "renderPlacementScreen")
        assert "entry.notes.camera_drift_warnings" in body
        assert "entry.notes.suspicious" in body


# "nie do grup akordeon, a tylko do bloku na
# gorze" -- the foldable part of the placement screen is the NOTES block, not
# the suggestion groups. It is the one part of that screen with no size limit
# ("Pominiete (nie-medialne)" can list every RAF/webp in a folder), so it used
# to push the suggestions themselves off the first screenful.
#
# Pure frontend -- no endpoint, no cache table, nothing that survives a
# reload -- so the same shape of test as TestSeriesSkipFrontend above: fetch
# "/" and pin the served markup and JS.
class TestPlacementNotesAccordion:
    def _html(self, running_server):
        _httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _function_body(self, html, name):
        match = re.search(r'function\s+' + name + r'\([^)]*\)\s*\{', html)
        assert match, f"expected a {name}() function"
        start = match.end()
        depth = 1
        i = start
        while i < len(html) and depth > 0:
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
            i += 1
        return html[start:i - 1]

    def test_fold_state_lives_on_state_and_starts_empty(self, running_server):
        # Holds the OPEN sections, so an empty dict means "everything folded"
        # -- the default the user asked for.
        html = self._html(running_server)
        assert re.search(r'placementNotesOpen:\s*\{\}\s*,', html)

    def test_every_notes_section_goes_through_one_foldable_helper(self, running_server):
        # The regression this pins: four hand-rolled heading+<ul> pairs, of
        # which only some would grow a fold. Each section names a FIXED key,
        # never an index -- a run with no camera-drift warnings must not shift
        # "Pominiete" onto the warnings' fold state.
        body = self._function_body(self._html(running_server), "renderPlacementScreen")
        for key in ('"warnings"', '"suspicious"', '"skipped"', '"emptyFolders"'):
            assert f"appendNotesSection(notesBox, {key}," in body
        # No section may still build its own <ul> outside the helper.
        assert 'document.createElement("ul")' not in body

    def test_sections_start_folded_and_the_heading_is_the_toggle(self, running_server):
        body = self._function_body(self._html(running_server), "appendNotesSection")
        assert "const open = !!state.placementNotesOpen[key];" in body
        assert 'heading.className = "notes-toggle"' in body
        assert 'list.style.display = open ? "" : "none";' in body
        assert "aria-expanded" in body

    def test_a_folded_heading_still_reports_its_size(self, running_server):
        # A folded section that says a problem exists without saying how big
        # it is gives the user nothing to decide on -- the count is what
        # decides whether the list is worth opening.
        body = self._function_body(self._html(running_server), "appendNotesSection")
        assert 'headingText + ": " + items.length' in body

    def test_toggling_a_section_does_not_re_render_the_screen(self, running_server):
        # The placement tab re-renders on every poll tick while a directory is
        # still computing; rebuilding a several-hundred-path list to flip one
        # section would throw away the scroll position each time.
        body = self._function_body(self._html(running_server), "appendNotesSection")
        handler = body[body.index('heading.addEventListener("click"'):]
        assert 'list.style.display = next ? "" : "none";' in handler
        assert "render()" not in handler

    def test_the_suggestion_groups_themselves_did_not_become_foldable(
        self, running_server,
    ):
        # Explicitly pinned, because the first cut of this feature folded the
        # groups and the folder headers instead, and the user asked for the
        # top block only.
        html = self._html(running_server)
        assert "placementCollapsed" not in html
        assert "placementFolderCollapsed" not in html
        assert "folder-toggle" not in html
        block = self._function_body(html, "makeSuggestionBlock")
        assert "collapse-btn" not in block


# "dodaj w lewym dolnym rogu maly checkbox bez
# tekstu, jezeli on wlaczony to jest tryb developera i widoczne
# podsumowanie: {...} Przywroc te paczke / domyslnie ten checkbox
# odznaczony". The "podsumowanie" is #result -- the raw apply/restore JSON
# plus the inline "Przywroc te paczke" undo button appendResult() builds.
class TestDevModeToggle:
    def _html(self, running_server):
        _httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def test_the_checkbox_is_unlabeled_and_starts_unchecked(self, running_server):
        html = self._html(running_server)
        match = re.search(r'<input[^>]*id="dev-mode-toggle"[^>]*>', html)
        assert match, "expected a dev-mode checkbox"
        tag = match.group(0)
        assert 'type="checkbox"' in tag
        # No visible caption, so the only thing naming this control is
        # title/aria-label -- without them it is an unexplained box.
        assert "title=" in tag
        assert "aria-label=" in tag
        assert "checked" not in tag, "the dev-mode checkbox must default to off"
        assert not re.search(r'<label[^>]*>[^<]*<input[^>]*id="dev-mode-toggle"', html)
        assert re.search(r'devMode:\s*false\s*,', html)

    def test_the_checkbox_is_pinned_to_the_bottom_left_corner(self, running_server):
        html = self._html(running_server)
        rule = re.search(r'#dev-mode-toggle\s*\{([^}]*)\}', html)
        assert rule, "expected a #dev-mode-toggle rule"
        css = rule.group(1)
        assert "position: fixed" in css
        assert "left:" in css
        assert "bottom:" in css

    def test_the_result_panel_is_hidden_until_dev_mode_is_on(self, running_server):
        html = self._html(running_server)
        base = re.search(r'#result\s*\{([^}]*)\}', html)
        assert base, "expected a #result rule"
        assert "display: none" in base.group(1)
        assert re.search(r'#result\.dev-visible\s*\{[^}]*display:\s*block', html)

    def test_the_toggle_reveals_results_that_were_already_appended(
        self, running_server,
    ):
        # The class goes on the #result CONTAINER, not on each entry: that is
        # what makes an apply done before the checkbox was touched show up the
        # moment it is, without appendResult() knowing about dev mode at all.
        html = self._html(running_server)
        handler = html[html.index('document.getElementById("dev-mode-toggle")'):]
        handler = handler[:handler.index("});")]
        assert "state.devMode = event.target.checked" in handler
        assert 'classList.toggle("dev-visible", state.devMode)' in handler
        append = re.search(r'function\s+appendResult\([^)]*\)\s*\{', html)
        assert append, "expected appendResult()"
        assert "devMode" not in html[append.end():append.end() + 1200]


class TestPageLayoutPhase3:
    """Phase 3 collapses the flat stack of full-width control rows into a
    sticky header (tabs + toolbar) and a sticky footer (apply bar), with
    the six .controls containers wrapping into one bar via a shared flex
    rule. As with the rest of this module, there is no JS/DOM runner here,
    so the layout is pinned down on the served markup and CSS source."""

    def _html(self, running_server):
        _httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def test_topbar_and_apply_bar_are_sticky_not_fixed(self, running_server):
        # A bare "position: fixed" on the apply bar would regress the
        # Przywracanie tab: updateScreenVisibility() sets display: none on
        # it there, and a fixed element with display:none is invisible but
        # a sticky one additionally takes up no layout space when shown
        # again elsewhere -- fixed would leave a dead strip behind.
        html = self._html(running_server)
        topbar_rule = re.search(r'\.topbar\s*\{([^}]*)\}', html)
        assert topbar_rule, "expected a .topbar rule"
        assert "position: sticky" in topbar_rule.group(1)

        # The apply controls now live inside the sticky .topbar
        # (#top-apply-controls), so they inherit its sticky positioning;
        # the footer #bottom-bar is the other sticky strip.
        topbar_pos = html.index('class="topbar"')
        main_pos = html.index("<main>")
        assert topbar_pos < html.index('id="top-apply-controls"') < main_pos
        assert "position: fixed" not in topbar_rule.group(1)
        bottom_rule = re.search(r'#bottom-bar\s*\{([^}]*)\}', html)
        assert bottom_rule, "expected a #bottom-bar rule"
        assert "position: sticky" in bottom_rule.group(1)
        assert "position: fixed" not in bottom_rule.group(1)

    def test_apply_bar_follows_the_screens_in_the_markup(self, running_server):
        # The sticky footer (#bottom-bar: view controls + pager) belongs
        # after the content, so it sticks to the bottom of the viewport; the
        # apply buttons sit in the sticky topbar, before the screens.
        html = self._html(running_server)
        dup_screen_pos = html.index('id="dup-screen"')
        assert html.index('id="bottom-bar"') > dup_screen_pos
        assert html.index('id="top-apply-controls"') < dup_screen_pos

    def test_controls_class_has_a_flex_rule(self, running_server):
        # This is the actual fix for the six-stacked-rows problem: each
        # .controls div was a plain block with no rule of its own. Losing
        # this rule silently restores the old layout.
        html = self._html(running_server)
        rule = re.search(r'\.controls\s*\{([^}]*)\}', html)
        assert rule, "expected a .controls rule"
        assert "display: flex" in rule.group(1)

    def test_rescan_btn_exists_but_is_not_a_tab(self, running_server):
        # switchTab() and its wiring only ever query .tab -- if rescan-btn
        # picked up that class it would be treated as a fourth screen tab
        # instead of an action button.
        html = self._html(running_server)
        match = re.search(r'<button[^>]*id="rescan-btn"[^>]*>', html)
        assert match, "expected a #rescan-btn button"
        tag = match.group(0)
        assert not re.search(r'class="[^"]*\btab\b[^"]*"', tag)
