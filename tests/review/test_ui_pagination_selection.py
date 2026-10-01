"""Served SPA: client-side pagination, columns and selection model (asserted on the served source)."""
from __future__ import annotations

import json
import re

from .helpers import (
    _get,
)


# --- client-side pagination + column-layout ------------------------------
#
# This module serves static HTML with an embedded <script> -- there is no
# browser or JS engine anywhere else in this repo's test suite (checked:
# no Selenium/Playwright/Node-based harness precedent exists), so
# render()'s actual DOM output (how many cards land on screen) cannot be
# executed and asserted against from this plain-HTTP test client without
# fabricating a new, unprecedented dependency. These tests instead pin
# down, at the layer that genuinely is testable here:
#   - the served markup carries the required controls (page-size select,
#     columns select, pager buttons + indicator) with the right
#     options/defaults;
#   - the served JS source contains the specific state-driven arithmetic
#     the design calls for (state shape, pageCount = clamp(ceil(...)),
#     slice, the --cols custom property) and that pagination/column
#     controls never re-issue fetch("/api/state");
#   - /api/state still returns the *complete*, unpaginated group list even
#     with 120 groups -- pagination is client-side only, which we CAN
#     confirm for real over the socket.
class TestIndexHtmlPaginationAndColumns:
    def _html(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def test_state_object_has_expected_shape(self, running_server):
        # state.page (a single global page number) became
        # state.pages = {dup: 1, series: 1} -- a series tab with fewer
        # pages than the dup tab's current page must not land on an empty
        # page. seriesSelection is state's own new field, alongside
        # selection, since a series group's keep/skip choice is exactly as
        # long-lived as a dup group's keeper/skip choice.
        html = self._html(running_server)
        assert re.search(
            r'const\s+state\s*=\s*\{\s*data:\s*null,\s*tab:\s*"dup",',
            html,
        ), "expected JS state object starting with data/tab"
        assert re.search(r'pages:\s*\{\s*dup:\s*1,\s*series:\s*1,\s*placement:\s*1\s*\}', html)
        assert re.search(r'pageSize:\s*50,\s*columns:\s*2,\s*selection:\s*\{\}', html)
        assert re.search(r'seriesSelection:\s*\{\}\s*,', html)
        # The placement screen's own selection/source fields, added
        # alongside seriesSelection for the same reason it was added
        # alongside selection -- its lifetime is a tab switch, not a fetch.
        assert re.search(r'placementSelection:\s*\{\}\s*,', html)
        assert re.search(r'placementSource:\s*"!NEW"\s*,', html)
        # Thumbnail zoom: shared state field alongside pageSize/columns
        # above.
        assert re.search(r'thumbZoom:\s*2\s*,?\s*\};', html)

    def test_page_size_select_present_with_options_and_default(self, running_server):
        html = self._html(running_server)
        select_match = re.search(r'<select id="page-size">(.*?)</select>', html, re.DOTALL)
        assert select_match, "expected a page-size <select>"
        options_html = select_match.group(1)
        for value in ("20", "50", "100", "250", "500", "1000"):
            assert f'value="{value}"' in options_html, f"missing page-size option {value}"
        assert re.search(r'<option value="50"[^>]*\bselected\b', options_html), (
            "50 must be the default (selected) page size"
        )

    def test_columns_select_present_with_options_and_default(self, running_server):
        html = self._html(running_server)
        select_match = re.search(r'<select id="columns">(.*?)</select>', html, re.DOTALL)
        assert select_match, "expected a columns <select>"
        options_html = select_match.group(1)
        for value in ("1", "2", "3", "4"):
            assert f'value="{value}"' in options_html, f"missing columns option {value}"
        assert re.search(r'<option value="2"[^>]*\bselected\b', options_html), (
            "2 must be the default (selected) column count"
        )

    def test_thumb_zoom_select_present_with_options_and_default(self, running_server):
        html = self._html(running_server)
        select_match = re.search(r'<select id="thumb-zoom">(.*?)</select>', html, re.DOTALL)
        assert select_match, "expected a thumb-zoom <select>"
        options_html = select_match.group(1)
        for value in ("1", "2"):
            assert f'value="{value}"' in options_html, f"missing thumb-zoom option {value}"
        assert re.search(r'<option value="2"[^>]*\bselected\b', options_html), (
            "2x must be the default (selected) zoom"
        )

    def test_thumb_zoom_control_is_outside_page_controls(self, running_server):
        # Zoom must stay usable on the read-only "events"
        # tab too, where #page-controls is hidden -- so it can't
        # live inside that container. #page-controls now lives inside
        # .toolbar rather than as the immediate sibling of #dup-screen, so
        # this matches the container by its own contents (no nested <div>
        # ever appears inside it) instead of anchoring on what follows it.
        html = self._html(running_server)
        page_controls_match = re.search(
            r'<div class="controls" id="page-controls">(.*?)</div>',
            html, re.DOTALL,
        )
        assert page_controls_match, "expected a #page-controls container"
        assert 'id="thumb-zoom"' not in page_controls_match.group(1)
        assert 'id="thumb-zoom"' in html

    def test_thumb_zoom_change_handler_updates_state_and_renders(self, running_server):
        html = self._html(running_server)
        handler = re.search(
            r'getElementById\("thumb-zoom"\)\.addEventListener\("change",\s*.*?\{(.*?)\}\s*\)\s*;',
            html, re.DOTALL,
        )
        assert handler, "expected a change listener on #thumb-zoom"
        body = handler.group(1)
        assert "state.thumbZoom" in body
        assert "render()" in body

    def test_render_sets_thumb_scale_css_custom_property(self, running_server):
        html = self._html(running_server)
        assert re.search(r'setProperty\(\s*["\']--thumb-scale["\']\s*,', html), (
            "render() must set --thumb-scale from state.thumbZoom"
        )

    def test_card_sizes_scale_with_thumb_scale_variable(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert "var(--thumb-scale)" in css
        # The base (1x) pixel sizes must still be present unchanged, just
        # wrapped in calc(... * var(--thumb-scale)) rather than replaced.
        assert "140px" in css and "128px" in css and "96px" in css

    def test_rotate_btn_css_overlays_top_right_of_its_own_thumb_wrap(self, running_server):
        # Revised after a report of no
        # button on "Serie i podobne": rotate is PER CARD (each card has
        # its own thumb_id there, unlike dup's one-per-group), so the
        # button is an absolutely-positioned overlay anchored to
        # .thumb-wrap (position: relative), not the group-bar.
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert re.search(r'\.thumb-wrap\s*\{[^}]*position:\s*relative', css)
        assert re.search(r'\.rotate-btn\s*\{[^}]*position:\s*absolute', css)

    def test_rotate_btn_created_per_card_only_when_it_has_a_thumb_and_posts_rotate(self, running_server):
        html = self._html(running_server)
        make_card_match = re.search(
            r'function makeCard\(entry, isKeeper, groupId, pathInfo, options\)\s*\{(.*?)\nfunction ',
            html, re.DOTALL,
        )
        assert make_card_match, "expected a makeCard() function"
        body = make_card_match.group(1)
        assert 'thumbWrap.className = "thumb-wrap"' in body
        assert 'className = "rotate-btn"' in body
        assert "rotateThumb(entry.thumb_id)" in body
        # Must be inside the `if (entry.thumb_id) { ... }` branch, not the
        # placeholder (else) branch -- a card with no thumbnail has nothing
        # to rotate.
        thumb_branch = re.search(r'if \(entry\.thumb_id\)\s*\{(.*?)\}\s*else\s*\{', body, re.DOTALL)
        assert thumb_branch, "expected an `if (entry.thumb_id) { ... } else { ... }` branch in makeCard()"
        assert 'className = "rotate-btn"' in thumb_branch.group(1)

    def test_rotate_thumb_function_posts_to_rotate_endpoint_with_token(self, running_server):
        html = self._html(running_server)
        fn_match = re.search(
            r'async function rotateThumb\(thumbId\)\s*\{(.*?)\n\}', html, re.DOTALL,
        )
        assert fn_match, "expected an async rotateThumb(thumbId) function"
        body = fn_match.group(1)
        assert '"/api/thumb/rotate"' in body
        assert '"X-Dedup-Token": TOKEN' in body
        assert "thumb_id: thumbId" in body
        assert "render()" in body

    def test_thumb_img_src_includes_cache_busting_version(self, running_server):
        html = self._html(running_server)
        assert "thumbVersions[entry.thumb_id]" in html

    def test_rotate_group_thumbs_function_dedupes_and_posts_in_parallel(self, running_server):
        # Follow-up to the per-card button: a whole-group rotate, for when
        # an entire burst/series was shot sideways together.
        html = self._html(running_server)
        fn_match = re.search(
            r'async function rotateGroupThumbs\(thumbIds\)\s*\{(.*?)\n\}', html, re.DOTALL,
        )
        assert fn_match, "expected an async rotateGroupThumbs(thumbIds) function"
        body = fn_match.group(1)
        assert "new Set(thumbIds)" in body, "must de-duplicate ids (dup groups share one thumb_id)"
        assert "Promise.all(" in body, "must fire all rotations in parallel, not sequentially"
        assert '"/api/thumb/rotate"' in body
        assert '"X-Dedup-Token": TOKEN' in body
        assert "render()" in body

    def test_rotate_group_btn_present_on_dup_series_and_placement_screens(self, running_server):
        html = self._html(running_server)
        assert html.count('className = "rotate-group-btn"') == 3, (
            "expected one rotate-group-btn creation site per screen (dup/series/placement)"
        )
        assert "rotateGroupThumbs([g.thumb_id])" in html  # dup screen: one shared id per group
        assert "rotateGroupThumbs(seriesThumbIds)" in html
        assert "rotateGroupThumbs(thumbIds)" in html

    def test_rotate_group_btn_css_pins_it_to_group_bar_top_right(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert re.search(r'\.group-bar\s*\{[^}]*display:\s*flex', css)
        assert re.search(r'\.rotate-group-btn\s*\{[^}]*margin-left:\s*auto', css)
        assert re.search(r'img\.src\s*=\s*"/api/thumb/"\s*\+\s*encodeURIComponent\(entry\.thumb_id\)\s*\+', html)

    def test_pager_controls_present(self, running_server):
        html = self._html(running_server)
        for control_id in ("first-page", "prev-page", "next-page", "last-page", "page-indicator"):
            assert f'id="{control_id}"' in html, f"missing pager control #{control_id}"

    def test_render_function_computes_clamped_page_count_and_slices(self, running_server):
        html = self._html(running_server)
        assert "function render(" in html
        assert "Math.ceil(groups.length / state.pageSize)" in html
        assert "Math.max(1," in html
        assert ".slice(" in html

    def test_render_sets_cols_css_custom_property(self, running_server):
        html = self._html(running_server)
        assert re.search(r'setProperty\(\s*["\']--cols["\']\s*,', html), (
            "render() must set --cols from state.columns"
        )

    def test_pager_text_format(self, running_server):
        html = self._html(running_server)
        # Pager text is localized: built from the js_page / js_of keys.
        assert 'i18n("js_page")' in html and 'i18n("js_of")' in html
        assert '"Page "' in html and '" of "' in html

    def test_loadstate_has_no_dom_building_loop(self, running_server):
        html = self._html(running_server)
        load_state_match = re.search(r'async function loadState\(\)\s*\{(.*?)\n\}', html, re.DOTALL)
        assert load_state_match, "expected an async loadState() function"
        body = load_state_match.group(1)
        assert "for (const g of" not in body, (
            "loadState() must delegate group rendering to render(), not loop itself"
        )
        assert "render()" in body, "loadState() must call render() after loading state.data"

    def test_pagesize_and_columns_change_handlers_do_not_fetch(self, running_server):
        html = self._html(running_server)
        # /api/state must be referenced exactly once in the whole script --
        # inside loadState() -- proving no pagination/column control wired
        # here triggers a second fetch.
        assert html.count("/api/state") == 1

        page_size_handler = re.search(
            r'getElementById\("page-size"\)\.addEventListener\("change",\s*.*?\{(.*?)\}\s*\)\s*;',
            html, re.DOTALL,
        )
        assert page_size_handler, "expected a change listener on #page-size"
        handler_body = page_size_handler.group(1)
        assert "fetch(" not in handler_body
        assert "state.pageSize" in handler_body
        # Resets the CURRENT TAB's page, not a global one.
        assert "setCurrentPage(1)" in handler_body
        assert "render()" in handler_body

        columns_handler = re.search(
            r'getElementById\("columns"\)\.addEventListener\("change",\s*.*?\{(.*?)\}\s*\)\s*;',
            html, re.DOTALL,
        )
        assert columns_handler, "expected a change listener on #columns"
        columns_body = columns_handler.group(1)
        assert "fetch(" not in columns_body
        assert "state.columns" in columns_body
        assert "render()" in columns_body

    def test_pager_button_handlers_navigate_without_fetch(self, running_server):
        html = self._html(running_server)
        for button_id in ("first-page", "prev-page", "next-page"):
            handler = re.search(
                r'getElementById\("%s"\)\.addEventListener\("click",\s*.*?\{(.*?)\}\s*\)\s*;'
                % re.escape(button_id),
                html, re.DOTALL,
            )
            assert handler, f"expected a click listener on #{button_id}"
            body = handler.group(1)
            assert "fetch(" not in body
            assert "render()" in body

        # Per-tab page number via setCurrentPage()/currentPage(),
        # not a global state.page.
        assert "setCurrentPage(1)" in html  # first-page handler
        assert "setCurrentPage(currentPage() - 1)" in html  # prev-page handler
        assert "setCurrentPage(currentPage() + 1)" in html  # next-page handler

        last_handler = re.search(
            r'getElementById\("last-page"\)\.addEventListener\("click",\s*.*?\{(.*?)\}\s*\)\s*;',
            html, re.DOTALL,
        )
        assert last_handler, "expected a click listener on #last-page"
        last_body = last_handler.group(1)
        assert "fetch(" not in last_body
        assert "pageCount" in last_body
        assert "render()" in last_body

    def test_dup_screen_uses_css_grid_with_cols_variable(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        # Bug fix: the grid rule (and --cols) must apply to
        # ALL THREE screens via one shared selector -- previously it was
        # scoped to #dup-screen alone, so changing "Kolumny" had no visible
        # effect on the "Serie i podobne"/"Sugestie grupowania" tabs.
        rule_match = re.search(
            r'#dup-screen\s*,\s*#series-screen\s*,\s*#placement-screen\s*\{([^}]*)\}', css,
        )
        assert rule_match, "expected dup/series/placement screens to share one grid rule"
        rule = rule_match.group(1)
        assert "display: grid" in rule
        assert "grid-template-columns" in rule and "var(--cols" in rule

    def test_inner_row_within_group_stays_flex(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        css = style_match.group(1)
        row_rule = re.search(r'\.row\s*\{([^}]*)\}', css)
        assert row_rule, "expected a .row CSS rule"
        assert "flex" in row_rule.group(1)


# --- client-side keeper/skip selection ----------------------------------
#
# Same constraint as the pagination tests above: no browser/JS-execution harness
# exists in this repo, so we cannot actually click a card and observe a
# fetch() body. Instead these tests pin down, at the only testable layer
# (the served JS source text):
#   - the exact shape of the new per-group selection state and the exact
#     literal expressions used to build the apply payload and the summary
#     counter from it -- not just "some function exists", per the step-2
#     review's own warning against assertions loose enough to pass against
#     broken code (e.g. checking for `.slice(` without the index math).
#   - that the *only* two inputs buildSelectionsPayload() reads per group
#     are `sel.keeperId` and `sel.excludedIds`, and that initSelection()
#     seeds those from `g.keeper.id` / an empty Set -- together these two
#     facts are a structural proof that "no clicks" reproduces exactly
#     today's payload (server keeper + all discarded ids, no groups
#     skipped), since nothing else can have changed those two fields yet.
class TestSelectionModelStep4:
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

    # -- default selection state seeding ----------------------------------

    def test_init_selection_function_exists_and_seeds_defaults(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "initSelection")
        assert "state.selection = {};" in body
        assert re.search(
            r'state\.selection\[g\.group_id\]\s*=\s*\{\s*'
            r'keeperId:\s*g\.keeper\.id,\s*'
            r'excludedIds:\s*new Set\(\),\s*'
            r'skip:\s*false\s*,?\s*\};',
            body,
        ), "default keeper must be the server's g.keeper.id, no exclusions, group not skipped"

    def test_load_state_calls_init_selection_before_render(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "loadState")
        init_pos = body.find("initSelection()")
        render_pos = body.find("render()")
        assert init_pos != -1, "loadState() must call initSelection()"
        assert render_pos != -1
        assert init_pos < render_pos, "selection must be (re)initialized before the first render"

    # -- payload building: the only two inputs are sel.keeperId/excludedIds --

    def test_build_selections_payload_reads_only_current_per_group_selection(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "buildSelectionsPayload")
        # Skipped groups are omitted entirely from the payload.
        assert "if (!sel || sel.skip) continue;" in body
        # keeper_id comes from sel.keeperId, not from g.keeper.id directly --
        # this is what makes a clicked card change the payload.
        assert re.search(r'const\s+keeperId\s*=\s*sel\.keeperId\s*;', body)
        # discard_ids = every id in the group except the current keeper and
        # anything individually excluded -- pinned to the literal filter,
        # not just "some filter exists".
        assert re.search(
            r'\.filter\(\s*\(?id\)?\s*=>\s*id\s*!==\s*keeperId\s*&&\s*'
            r'!sel\.excludedIds\.has\(id\)\s*\)',
            body,
        ), "discard_ids must exclude the current keeperId and anything in excludedIds"
        assert "group_id: g.group_id" in body
        assert "keeper_id: keeperId" in body
        assert re.search(r'discard_ids:\s*discardIds', body)

    def test_build_selections_payload_result_key_is_selections(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "buildSelectionsPayload")
        assert re.search(r'return\s*\{\s*selections\s*\}\s*;', body), (
            "buildSelectionsPayload() must return {selections: [...]}, matching "
            "the step-3 POST /api/apply contract"
        )

    def test_group_file_ids_includes_keeper_and_all_discarded(self, running_server):
        # groupFileIds(g) is what buildSelectionsPayload() filters -- pin
        # down that it is [keeper, ...discarded], the complete id universe
        # for a group, not some other subset.
        html = self._html(running_server)
        body = self._function_body(html, "groupFileIds")
        assert re.search(
            r'return\s*\[\s*g\.keeper\.id\s*,\s*\.\.\.\s*g\.discarded\.map\(\s*'
            r'\(?d\)?\s*=>\s*d\.id\s*\)\s*\]\s*;',
            body,
        )

    # -- card click moves the keeper ---------------------------------------

    def test_make_card_click_handler_sets_keeper_id_from_clicked_entry(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        click_handler = re.search(
            r'card\.addEventListener\(\s*"click"\s*,\s*\(\)\s*=>\s*\{(.*?)\}\s*\)\s*;',
            body, re.DOTALL,
        )
        assert click_handler, "expected a click listener on the card itself"
        handler_body = click_handler.group(1)
        assert "state.selection[groupId]" in handler_body
        assert re.search(r'sel\.keeperId\s*=\s*entry\.id\s*;', handler_body), (
            "clicking a card must set that card's own id as the new keeper "
            "for its group -- this is the line whose absence would silently "
            "break 'clicking a different card changes the payload'"
        )
        assert "render()" in handler_body

    # -- card double-click opens the file in its OS default app -----------

    def test_make_card_dblclick_handler_sets_keeper_and_opens_file(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        dblclick_handler = re.search(
            r'card\.addEventListener\(\s*"dblclick"\s*,\s*\(\)\s*=>\s*\{(.*?)\}\s*\)\s*;',
            body, re.DOTALL,
        )
        assert dblclick_handler, "expected a dblclick listener on the card itself"
        handler_body = dblclick_handler.group(1)
        # Double-click must promote that card's entry to keeper the same
        # way a single click already does (reusing the same selection
        # mechanism, not a second parallel one).
        assert "state.selection[groupId]" in handler_body
        assert re.search(r'sel\.keeperId\s*=\s*entry\.id\s*;', handler_body)
        assert "render()" in handler_body
        # ...and it must ask the server to open that same file.
        assert re.search(r'openFile\(\s*entry\.id\s*\)\s*;', handler_body)

    def test_open_file_posts_to_api_open_with_required_headers_and_file_id(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "openFile")
        assert re.search(r'fetch\(\s*"/api/open"\s*,\s*\{', body)
        assert 'method: "POST"' in body
        assert '"X-Dedup-Token": TOKEN' in body
        assert '"Content-Type": "application/json"' in body
        assert re.search(r'body:\s*JSON\.stringify\(\s*\{\s*file_id:\s*fileId\s*\}\s*\)', body)

    def test_make_card_sets_data_id_and_keeper_class_from_selection(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert "card.dataset.id = entry.id;" in body
        # isKeeper is passed in by the caller (render()), not recomputed
        # here from server data -- render() is checked separately below for
        # the actual "entry.id === sel.keeperId" comparison.
        assert 'card.className = "card" + (isKeeper ? " keeper" : "");' in body

    def test_render_determines_keeper_by_comparing_entry_id_to_current_selection(self, running_server):
        html = self._html(running_server)
        render_body = self._function_body(html, "render")
        assert re.search(
            r'entry\.id\s*===\s*sel\.keeperId',
            render_body,
        ), "render() must decide which card is the keeper from current selection state, not from g.keeper alone"

    # -- per-file exclude checkbox -------------------------------------------

    def test_exclude_checkbox_toggles_membership_in_excluded_ids(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert "exclude-checkbox" in body
        change_handler = re.search(
            r'checkbox\.addEventListener\(\s*"change"\s*,\s*\(e\)\s*=>\s*\{(.*?)\}\s*\)\s*;',
            body, re.DOTALL,
        )
        assert change_handler, "expected a change listener on the exclude checkbox"
        handler_body = change_handler.group(1)
        assert "sel.excludedIds.add(entry.id)" in handler_body
        assert "sel.excludedIds.delete(entry.id)" in handler_body
        assert "e.target.checked" in handler_body
        assert "render()" in handler_body
        # Must not let a click on the checkbox also fire the card's own
        # click handler (which would immediately re-promote this file back
        # to keeper and silently undo the exclusion).
        assert re.search(r'checkbox\.addEventListener\(\s*"click"\s*,\s*\(?e\)?\s*=>\s*e\.stopPropagation\(\)',
                          body)

    def test_exclude_checkbox_only_rendered_for_non_keeper_cards(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        # This additionally guards it behind !options.readOnly
        # (series/events cards never get an exclude checkbox at all, keeper
        # or not) -- the non-keeper condition itself is unchanged.
        assert "if (!isKeeper && !options.readOnly)" in body

    # -- per-group skip control ----------------------------------------------

    def test_render_creates_skip_group_checkbox_wired_to_sel_skip(self, running_server):
        html = self._html(running_server)
        render_body = self._function_body(html, "render")
        assert "skip-group-checkbox" in render_body
        change_handler = re.search(
            r'skipCheckbox\.addEventListener\(\s*"change"\s*,\s*\(e\)\s*=>\s*\{(.*?)\}\s*\)\s*;',
            render_body, re.DOTALL,
        )
        assert change_handler, "expected a change listener on the skip-group checkbox"
        handler_body = change_handler.group(1)
        assert re.search(r'sel\.skip\s*=\s*e\.target\.checked\s*;', handler_body)
        assert "render()" in handler_body

    # -- summary reflects current selection, not server totals --------------

    def test_compute_selection_summary_sums_only_non_skipped_groups(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "computeSelectionSummary")
        assert "if (!sel || sel.skip) continue;" in body
        assert re.search(
            r'\.filter\(\s*\(?id\)?\s*=>\s*id\s*!==\s*sel\.keeperId\s*&&\s*'
            r'!sel\.excludedIds\.has\(id\)\s*\)',
            body,
        )
        assert "fileCount += discardIds.length;" in body
        assert "bytes += g.size * discardIds.length;" in body

    def test_update_summary_uses_current_selection_summary_not_server_totals(self, running_server):
        html = self._html(running_server)
        # The old, step-3-and-earlier summary line read straight off
        # state.data.group_count / state.data.total_reclaimable_bytes.
        # This must stop doing that -- the whole point is that the
        # counter tracks the *current* selection instead.
        assert "state.data.group_count + " not in html
        assert "fmtSize(state.data.total_reclaimable_bytes)" not in html
        body = self._function_body(html, "updateSummary")
        # computeSelectionSummary() is parameterized (each apply
        # button's confirm() dialog needs a different subset) -- the
        # overall #summary bar still passes it the *whole* dataset.
        assert re.search(r'computeSelectionSummary\(\s*groups\s*\)', body)
        assert "fmtSize(bytes)" in body
        assert 'getElementById("summary")' in body

    def test_render_calls_update_summary(self, running_server):
        html = self._html(running_server)
        render_body = self._function_body(html, "render")
        assert "updateSummary()" in render_body

    # -- "always exactly one keeper by construction" -------------------------

    def test_keeper_id_is_never_assigned_a_falsy_value_anywhere(self, running_server):
        # Prefer-by-construction design:
        # there is no code path that clears keeperId, so "a group with no
        # keeper" cannot arise client-side -- initSelection() always seeds
        # it, and the only other writer (the card click handler) always
        # assigns a real entry.id.
        html = self._html(running_server)
        assert "keeperId = null" not in html
        assert "keeperId = undefined" not in html
        assert "keeperId: null" not in html
        assert "keeperId: undefined" not in html
        # Writes through `sel.keeperId = ...` (as opposed to reads like
        # `const keeperId = sel.keeperId`) must only ever come from a real
        # clicked card's id.
        write_assignments = re.findall(r'sel\.keeperId\s*=\s*([^,;\n]+)', html)
        assert write_assignments, "expected at least one sel.keeperId write (the card click handler)"
        for rhs in write_assignments:
            assert rhs.strip() == "entry.id", f"unexpected sel.keeperId assignment source: {rhs!r}"
        # Object-literal seeding (initSelection()'s default, and render()'s
        # defensive fallback) must only ever come from the server's own
        # g.keeper.id.
        literal_assignments = re.findall(r'keeperId:\s*([^,;\n}]+)', html)
        assert literal_assignments, "expected at least one keeperId: ... object-literal seed"
        for rhs in literal_assignments:
            assert rhs.strip() == "g.keeper.id", f"unexpected keeperId literal seed: {rhs!r}"


    # -- apply button wiring moved to TestApplyButtonsKrok5 below: it
    # splits the single #apply-btn into #apply-all-btn
    # / #apply-page-btn, each with its own subset-of-groups/summary/confirm.


# --- two apply buttons, applied-group -------
# marking, and an accumulating results panel -- pinned at the JS-source
# level like the pagination/selection tests above, for the same reason (no
# browser/JS execution harness in this repo's test suite).
class TestApplyButtonsKrok5:
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

    def _click_handler_body(self, html, element_id):
        handler = re.search(
            r'getElementById\("%s"\)\.addEventListener\("click",\s*async\s*\(\)\s*=>\s*\{(.*?)\n\}\s*\)\s*;'
            % re.escape(element_id),
            html, re.DOTALL,
        )
        assert handler, f"expected an async click listener on #{element_id}"
        return handler.group(1)

    # -- both buttons exist, replacing the old single #apply-btn -----------

    def test_old_single_apply_btn_id_is_gone_replaced_by_two_buttons(self, running_server):
        html = self._html(running_server)
        assert 'id="apply-btn"' not in html
        assert 'id="apply-all-btn"' in html
        assert 'id="apply-page-btn"' in html

    def test_scroll_to_top_button_sits_in_the_bottom_bar_after_apply_page(self, running_server):
        # A long page of cards leaves the tabs far above; the sticky footer
        # (#bottom-bar) is on screen, so the way back up rides along with it,
        # away from the apply buttons in the topbar.
        html = self._html(running_server)
        assert 'id="scroll-top-btn"' in html
        bar_pos = html.index('id="bottom-bar"')
        assert html.index('id="scroll-top-btn"') > bar_pos
        assert html.index('id="apply-page-btn"') < bar_pos
        handler = re.search(
            r'getElementById\("scroll-top-btn"\)\.addEventListener\("click",\s*\(\)\s*=>\s*\{(.*?)\}\s*\)\s*;',
            html, re.DOTALL,
        )
        assert handler, "expected a click listener on #scroll-top-btn"
        assert re.search(r'window\.scrollTo\(\s*\{\s*top:\s*0', handler.group(1))

    # -- nonAppliedGroups() / currentPageGroups() helpers -------------------

    def test_non_applied_groups_filters_out_applied_flag(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "nonAppliedGroups")
        assert re.search(r'return\s+groups\.filter\(\s*\(?g\)?\s*=>\s*!g\.applied\s*\)\s*;', body)

    def test_current_page_groups_uses_same_arithmetic_as_render(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "currentPageGroups")
        assert "pageCountFor(groups)" in body
        assert re.search(r'\(page\s*-\s*1\)\s*\*\s*state\.pageSize', body)
        assert ".slice(" in body

    # -- apply-all-btn: submits nonAppliedGroups(state.data.groups) --------

    def test_apply_all_button_builds_payload_from_non_applied_groups_of_whole_dataset(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-all-btn")
        assert re.search(
            r'nonAppliedGroups\(\s*state\.data\s*\?\s*state\.data\.groups\s*:\s*\[\]\s*\)', body,
        )
        assert re.search(r'buildSelectionsPayload\(\s*groups\s*\)', body)
        assert re.search(r'computeSelectionSummary\(\s*groups\s*\)', body)

    def test_apply_all_button_posts_selections_payload_with_required_headers(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-all-btn")
        assert "submitApply(payload)" in body

    def test_apply_all_button_still_confirms_before_posting(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-all-btn")
        assert "confirm(" in body

    def test_apply_all_button_guards_against_empty_selections(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-all-btn")
        assert re.search(r'payload\.selections\.length\s*===\s*0', body)

    # -- apply-page-btn: submits nonAppliedGroups(currentPageGroups(...)) --

    def test_apply_page_button_builds_payload_from_non_applied_groups_of_current_page(self, running_server):
        # currentPageGroups() now takes an explicit `groups`
        # argument (it pages whichever tab is active, not just "dup"), so
        # the dup branch calls it with state.data.groups -- still narrowed
        # to the current page, still not the whole dataset.
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-page-btn")
        assert re.search(
            r'nonAppliedGroups\(\s*currentPageGroups\(\s*state\.data\s*\?\s*state\.data\.groups\s*:\s*\[\]\s*\)\s*\)',
            body,
        ), (
            "Zastosuj biezaca strone (dup) must narrow to currentPageGroups(...), "
            "not the whole dataset -- this is the specific line that makes "
            "it only move what's visible on the current page at click time"
        )
        assert re.search(r'buildSelectionsPayload\(\s*groups\s*\)', body)
        assert re.search(r'computeSelectionSummary\(\s*groups\s*\)', body)
        # And the series branch mirrors it with the series-specific helpers.
        # It pages visibleSeriesGroups(), NOT the
        # raw tabData.series.groups it used to -- a group hidden by a
        # persisted "pomin" is not on the page, so paging the unfiltered list
        # would submit groups the user is not looking at. That is the exact
        # bug this assertion now pins against.
        assert re.search(
            r'nonAppliedGroups\(\s*currentPageGroups\(\s*visibleSeriesGroups\(\)\s*\)\s*\)',
            body,
        )
        assert re.search(r'buildSeriesSelectionsPayload\(\s*groups\s*\)', body)
        assert re.search(r'computeSeriesSelectionSummary\(\s*groups\s*\)', body)

    def test_apply_page_button_posts_selections_payload_with_required_headers(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-page-btn")
        assert "submitApply(payload)" in body

    def test_apply_page_button_still_confirms_before_posting(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-page-btn")
        assert "confirm(" in body

    def test_apply_page_button_guards_against_empty_selections(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-page-btn")
        assert re.search(r'payload\.selections\.length\s*===\s*0', body)

    # -- submitApply(): shared POST + accumulate-results + refresh ----------

    def test_submit_apply_posts_with_required_headers_and_body(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitApply")
        assert re.search(r'fetch\(\s*"/api/apply"\s*,\s*\{', body)
        assert 'method: "POST"' in body
        assert '"X-Dedup-Token": TOKEN' in body
        assert '"Content-Type": "application/json"' in body
        assert re.search(r'body:\s*JSON\.stringify\(\s*payload\s*\)', body)

    def test_submit_apply_appends_result_and_reloads_state(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitApply")
        assert "appendResult(result)" in body
        assert "loadState()" in body

    # -- appendResult(): accumulates into #result, never replaces it -------

    def test_append_result_uses_append_child_not_text_content_assignment(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "appendResult")
        assert 'resultEl.appendChild(entry)' in body
        assert re.search(r'resultEl\.textContent\s*=', body) is None, (
            "appendResult() must accumulate results, not replace #result's "
            "previous content on every apply"
        )

    # -- render(): applied groups get a visual marker -----------------------

    def test_render_adds_applied_class_and_badge_for_applied_groups(self, running_server):
        html = self._html(running_server)
        render_body = self._function_body(html, "render")
        assert re.search(
            r'"group"\s*\+\s*\(\s*g\.applied\s*\?\s*"\s*applied"\s*:\s*""\s*\)', render_body,
        )
        assert "applied-badge" in render_body

    def test_css_defines_applied_group_style(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert re.search(r'\.group\.applied\s*\{', css)

    # -- updateApplyButtonsEnabled(): disables both buttons together -------
    #
    # This logic moved out of loadState() (which only ever ran
    # once per page load) into updateApplyButtonsEnabled(), called from
    # render() on every render -- because the "dup" tab's own condition is
    # now only ONE of two branches (the other being "series"), and it must
    # stay correct across tab switches and series-computation poll ticks,
    # not just right after the initial /api/state fetch.

    def test_update_apply_buttons_enabled_disables_both_buttons_together_for_dup(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "updateApplyButtonsEnabled")
        assert re.search(
            r'!state\.data\s*\|\|\s*state\.data\.all_applied\s*\|\|\s*state\.data\.group_count\s*===\s*0', body,
        )
        assert "allBtn.disabled = disable" in body
        assert "pageBtn.disabled = disable" in body

    def test_render_calls_update_apply_buttons_enabled(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "render")
        assert "updateApplyButtonsEnabled()" in body


# --- fmtSize(): replaces fmtGB(), picks a unit instead of hardcoding GB --
#
# Same constraint as the other JS-source-pinning classes in this file (no
# browser/JS execution harness) -- these tests pin the exact literal
# expressions fmtSize() must contain (the unit table, the 1024-based
# division loop, and the "integer bytes vs 1-decimal everything else"
# split) rather than executing it, plus confirm fmtGB is gone from the
# served page entirely and every former fmtGB() call site now calls
# fmtSize() instead.
class TestFmtSize:
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

    def _click_handler_body(self, html, element_id):
        handler = re.search(
            r'getElementById\("%s"\)\.addEventListener\("click",\s*async\s*\(\)\s*=>\s*\{(.*?)\n\}\s*\)\s*;'
            % re.escape(element_id),
            html, re.DOTALL,
        )
        assert handler, f"expected an async click listener on #{element_id}"
        return handler.group(1)

    def test_fmt_gb_is_gone_entirely_no_hardcoded_gb_anywhere(self, running_server):
        html = self._html(running_server)
        assert "fmtGB" not in html
        assert '" GB"' not in html

    def test_fmt_size_function_exists(self, running_server):
        html = self._html(running_server)
        assert re.search(r'function\s+fmtSize\s*\(\s*bytes\s*\)\s*\{', html)

    def test_fmt_size_picks_unit_via_1024_based_division_loop(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "fmtSize")
        assert re.search(
            r'\[\s*"B"\s*,\s*"KB"\s*,\s*"MB"\s*,\s*"GB"\s*,\s*"TB"\s*\]', body,
        ), "expected an ordered B/KB/MB/GB/TB unit table"
        assert re.search(
            r'while\s*\(\s*value\s*>=\s*1024\s*&&\s*unitIndex\s*<\s*units\.length\s*-\s*1\s*\)',
            body,
        ), "expected a 1024-based division loop that stops at the last unit (TB)"
        assert re.search(r'value\s*/=\s*1024\s*;', body)
        assert re.search(r'unitIndex\s*\+=\s*1\s*;', body)

    def test_fmt_size_uses_one_decimal_for_kb_and_up_plain_integer_for_bytes(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "fmtSize")
        # Acceptance: "3.0 MB" / "512.0 KB" / "1.5 TB" (1 decimal), but a
        # bytes-range value must render as a plain integer -- "0 B", never
        # "0.0 B" -- so the two cases need visibly different formatting
        # calls, not a single unconditional toFixed(1).
        assert "toFixed(1)" in body
        assert "Math.round(value)" in body
        assert re.search(r'unitIndex\s*===\s*0', body), (
            "expected the bytes-range (unitIndex 0) case to be handled "
            "separately from the toFixed(1) branch"
        )

    def test_summary_bar_calls_fmt_size(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "updateSummary")
        assert "fmtSize(bytes)" in body

    def test_apply_all_confirm_dialog_calls_fmt_size(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-all-btn")
        assert "fmtSize(summary.bytes)" in body

    def test_apply_page_confirm_dialog_calls_fmt_size(self, running_server):
        html = self._html(running_server)
        body = self._click_handler_body(html, "apply-page-btn")
        assert "fmtSize(summary.bytes)" in body


# --- per-card size display -------------------------------------------------
#
# Every card (keeper and each discard) shows its OWN size, above its path
# -- sourced from the "size" field build_group_views() now puts on both
# g.keeper and every g.discarded entry (see TestBuildGroupViews above),
# never a shared/group-level figure. Same JS-source-pinning constraint as
# the rest of this file.
class TestCardSizeDisplay:
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

    def test_make_card_renders_size_from_entry_size_via_fmt_size(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        assert re.search(r'size\.textContent\s*=\s*fmtSize\(\s*entry\.size\s*\)\s*;', body), (
            "each card's size must come from its OWN entry.size (keeper or "
            "discard), not a shared group-level value"
        )
        assert 'card.appendChild(size)' in body

    def test_make_card_size_element_appears_above_the_path_element(self, running_server):
        # "Above" == appended to the card before the path div, so it lands
        # earlier in the DOM/visual flow.
        html = self._html(running_server)
        body = self._function_body(html, "makeCard")
        size_pos = body.find("size.className")
        path_pos = body.find("path.className")
        assert size_pos != -1 and path_pos != -1
        assert size_pos < path_pos, "size element must be created/appended before the path element"

    def test_entries_built_in_render_carry_through_server_supplied_size(self, running_server):
        # render() builds `entries` by spreading g.keeper / each g.discarded
        # item -- since build_group_views() now puts "size" on both, no
        # extra plumbing is needed here; this pins that render() doesn't
        # strip it back out with an explicit field allowlist.
        html = self._html(running_server)
        body = self._function_body(html, "render")
        assert re.search(r'\{\s*\.\.\.\s*g\.keeper\s*,\s*thumb_id:\s*g\.thumb_id\s*,\s*thumb_kind:\s*g\.thumb_kind\s*\}', body)
        assert re.search(r'\.\.\.\s*g\.discarded\.map\(\s*\(?d\)?\s*=>\s*\(\s*\{\s*\.\.\.\s*d\s*,', body)

    def test_api_state_group_carries_size_on_keeper_and_discarded(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/api/state")
        assert status == 200
        group = json.loads(body)["groups"][0]
        assert group["keeper"]["size"] == 100
        assert group["discarded"][0]["size"] == 100
