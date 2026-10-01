"""Served SPA: compress tab."""
from __future__ import annotations

import re

from .helpers import (
    _get,
)


class TestCompressFrontend:
    """Source-level assertions on the served page for the compress tab
    (docs/technical/components/review-ui.md) -- same
    approach as TestThreeTabsFrontendRunda2Krok4/TestRestoreFrontend above:
    no in-repo JS execution harness, so behaviour is pinned by regex/
    substring checks against the served page source rather than by driving
    a browser."""

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

    # -- markup: tab button, screen container, shared source picker --------

    def test_compress_tab_button_and_screen_container_exist(self, running_server):
        html = self._html(running_server)
        assert 'data-tab="compress"' in html
        assert "Sugestia kompresji" in html
        assert 'id="compress-screen"' in html

    def test_source_controls_shared_between_placement_and_compress_tabs(self, running_server):
        # Same picker element, same two server-side gates (docs/technical/components/review-ui.md) --
        # GET /api/source-dirs is reused as-is, never reimplemented.
        html = self._html(running_server)
        body = self._function_body(html, "updateScreenVisibility")
        assert 'state.tab === "placement" || state.tab === "compress"' in body

    def test_compress_controls_offer_a_show_skipped_toggle(self, running_server):
        html = self._html(running_server)
        assert 'id="compress-controls"' in html
        assert 'id="show-skipped-compress"' in html
        assert 'data-i18n="js_show_skipped_comp"' in html

    def test_render_source_dir_select_reflects_the_active_tabs_own_source(self, running_server):
        # One shared <select>, two independent chosen directories -- which
        # one the dropdown shows as selected depends on state.tab, not on
        # whichever tab last fetched the list.
        html = self._html(running_server)
        body = self._function_body(html, "renderSourceDirSelect")
        assert 'state.tab === "compress" ? state.compressSource : state.placementSource' in body

    def test_switch_tab_wires_compress_like_placement_and_series(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "switchTab")
        assert 'tab === "series" || tab === "placement" || tab === "compress"' in body
        assert 'tab === "placement" || tab === "compress"' in body
        assert "ensureCompressEncoders()" in body
        assert "ensureCompressJobs()" in body
        assert "loadState()" not in body

    # -- polling: GET /api/compress?source=<rel>, same lazy-computation shape

    def test_poll_tab_data_fetches_compress_with_its_own_source(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "pollTabData")
        assert '"/api/compress?source=" + encodeURIComponent(state.compressSource)' in body
        assert "ensureCompressSelection(tabData.compress.candidates)" in body

    def test_render_compress_screen_shows_progress_and_error_messages(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderCompressScreen")
        assert 'entry.status === "error"' in body
        assert "entry.error" in body
        assert 'entry.status !== "ready"' in body
        assert "makeProgressMessage(" in body

    def test_show_skipped_compress_checkbox_is_wired_to_state(self, running_server):
        # Regression: the checkbox and its "(N)" counter existed, but no
        # change listener ever set state.showSkippedCompress.
        html = self._html(running_server)
        m = re.search(
            r'getElementById\("show-skipped-compress"\)\.addEventListener\("change", \(e\) => \{(.*?)\}\);',
            html, re.S,
        )
        assert m, "no change listener on #show-skipped-compress"
        assert "state.showSkippedCompress = e.target.checked" in m.group(1)
        assert "render()" in m.group(1)

    def test_action_panel_sits_in_sticky_topbar_right_after_source_picker(self, running_server):
        html = self._html(running_server)
        topbar = html[html.index('<div class="topbar">'):html.index("<main>")]
        assert topbar.index('id="source-controls"') < topbar.index('id="compress-action-slot"')
        assert topbar.index('id="compress-action-slot"') < topbar.index('id="compress-controls"')
        body = self._function_body(html, "renderCompressScreen")
        assert "actionSlot.appendChild(panel.settingsRow)" in body
        assert "applySlot.appendChild(panel.actionsRow)" in body
        assert "screen.appendChild(makeCompressActionPanel())" not in body
        # The action buttons own the right-hand group; the view controls
        # live in the sticky footer instead.
        right = topbar[topbar.index('class="toolbar-right"'):]
        assert 'id="compress-apply-slot"' in right
        assert 'id="columns-controls"' not in topbar
        footer = html[html.index('id="bottom-bar"'):]
        assert footer.index('id="columns-controls"') < footer.index('id="bottom-pager"')

    # -- candidate selection: default-selected, skip persisted server-side --

    def test_ensure_compress_selection_defaults_every_candidate_selected(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "ensureCompressSelection")
        assert "selected: true" in body
        assert "skip: false" in body

    def test_toggle_compress_skip_posts_to_compress_skip_endpoint(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "toggleCompressSkip")
        assert '"/api/compress/skip"' in body
        assert "group_id: candidate.group_id" in body

    # -- estimates table: never fabricates a number for an uncalibrated profile

    def test_estimates_table_shows_question_mark_for_unmatched_encoder(self, running_server):
        # A profile this candidate's own estimates[] does not
        # cover (should not happen -- compress.py estimates every configured
        # profile -- but the table must not fabricate a number if it ever
        # did) must show "?" for both columns.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'sizeCell.textContent = "?"' in body
        assert 'timeCell.textContent = "?"' in body
        assert "est.estimated_size > 0" in body

    def test_estimates_table_prefixes_uncalibrated_values_with_approx_sign(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'est.calibrated ? "" : "' in body
        assert "≈" in body  # the seed-value "approximately" prefix, embedded as a literal UTF-8 char

    def test_estimates_table_header_is_quality_not_encoder(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'i18n("js_comp_col_q")' in body
        assert 'i18n("js_comp_col_sz")' in body
        assert 'i18n("js_comp_col_t")' in body
        assert "Encoder" not in body

    # -- "Quality (dB)" column: docs/technical/components/compression-mechanism.md ------------------------------

    def test_estimates_table_has_a_quality_diff_column(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'i18n("js_comp_col_qdb")' in body
        assert "est.xpsnr_diff" in body
        assert 'i18n("js_comp_ref")' in body

    def test_quality_diff_falls_back_to_question_mark(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'qualityCell.textContent = "?"' in body

    # -- "Zmierz" button and calibration status: docs/technical/components/compression-mechanism.md -----------

    def test_calibrate_button_exists_and_is_hidden_once_calibrated(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'calibrateBtn.id = "compress-calibrate-btn"' in body
        assert "!selectedEncoderInfoForCalibrate.calibrated" in body

    def test_calibrate_button_disabled_while_any_queue_runs(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "calibrateBtn.disabled = tabData.compress.jobsRunning || calibrationRunning;" in body

    def test_submit_calibrate_posts_file_id_and_device_keys(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitCalibrate")
        assert '"/api/compress/calibrate"' in body
        assert "file_id: candidates[0].id" in body
        assert "device_keys: [deviceKey]" in body
        assert "ensureCompressJobs();" in body

    def test_jobs_poll_stores_calibration_status(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "fetchCompressJobsOnce")
        assert "tabData.compress.calibration = payload.calibration || null;" in body

    def test_estimates_table_has_one_row_per_configured_profile(self, running_server):
        # All four profiles (x265, x265_medium, x265_high, av1) must show up
        # as rows, matched by profile_name -- not just the two that used to
        # be reachable through the old per-hardware-encoder lookup.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert "COMPRESS_PROFILE_LABELS.forEach" in body
        assert "e.profile_name === profileName" in body
        html_full = html
        for profile in ("x265_high", "x265_medium", "x265", "av1"):
            assert f'"{profile}"' in html_full

    def test_estimates_table_and_profile_select_share_one_label_list(self, running_server):
        # The "Quality" select (makeCompressActionPanel()) and the estimates
        # table rows must never describe the same profile differently.
        html = self._html(running_server)
        action_body = self._function_body(html, "makeCompressActionPanel")
        table_body = self._function_body(html, "makeCompressEstimatesTable")
        assert "COMPRESS_PROFILE_LABELS.forEach" in action_body
        assert "COMPRESS_PROFILE_LABELS.forEach" in table_body

    # -- single "Quality" selector (profile + device): docs/technical/components/review-ui.md

    def test_single_quality_select_pairs_profiles_with_detected_encoders(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'profileSelect.id = "compress-profile"' in body
        assert '"compress-device"' not in body
        assert "Urzadzenie" not in body
        assert "availableEncoders.filter((enc) => enc.name === encoderName)" in body
        assert 'opt.value = value + "|" + enc.device_key;' in body
        assert "opt.dataset.deviceKey = enc.device_key;" in body

    def test_device_name_appears_only_when_one_encoder_has_several_devices(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'devices.length > 1 ? label + " - " + enc.display_name : label' in body

    def test_quality_select_greys_out_while_queue_runs_and_remembers_submission(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "profileSelect.disabled = tabData.compress.jobsRunning;" in body
        assert "pickOption(tabData.compress.submittedProfile, tabData.compress.submittedDeviceKey);" in body

    def test_quality_choice_survives_rerender_while_idle(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "tabData.compress.chosenProfile = opt ? opt.dataset.profile : null;" in body
        assert "tabData.compress.chosenDeviceKey = opt ? opt.dataset.deviceKey : null;" in body
        assert "pickOption(tabData.compress.chosenProfile, tabData.compress.chosenDeviceKey);" in body

    def test_submit_omits_device_key_for_cpu_profiles_and_sends_it_for_hardware(self, running_server):
        html = self._html(running_server)
        submit_body = self._function_body(html, "submitApplyCompress")
        assert "profileOption.dataset.profile" in submit_body
        assert "e.device_key === profileOption.dataset.deviceKey" in submit_body
        assert "selectedEncoderInfo.is_hardware" in submit_body
        assert "tabData.compress.submittedDeviceKey = deviceKey;" in submit_body
        assert "if (deviceKey) body.device_key = deviceKey;" in submit_body

    def test_estimates_table_shows_source_size_row(self, running_server):
        # The candidate's current size used to sit in the block's own header
        # (see test_candidate_header_omits_size below); it now lives here.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressEstimatesTable")
        assert 'i18n("js_comp_src_orig")' in body
        assert "fmtSize(candidate.size)" in body

    # -- candidate block: header no longer duplicates the size or the reason

    def test_candidate_header_omits_size(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert "fmtSize(candidate.size)" not in body

    def test_candidate_header_omits_the_raw_heuristic_reason(self, running_server):
        # "bpp 0.452 > threshold 0.100 for hevc" is internal rule-8
        # plumbing, meaningless to someone deciding whether to compress a
        # file -- it stays in the skipped list at the bottom (where a
        # reason is the whole point) but no longer clutters the header.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert "infoSpan" not in body
        assert "textContent = candidate.reason" not in body

    # -- candidate block: badge shows the live job status, not a static label

    def test_candidate_badge_shows_live_job_status(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert "compressJobStatusLabel(job)" in body
        assert "j.group_id === candidate.group_id" in body
        assert '"zakolejkowano"' not in body

    def test_compress_job_status_label_translates_every_pipeline_stage(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "compressJobStatusLabel")
        for stage in ("queued", "encoding", "verifying", "encoded", "done", "cancelled", "failed"):
            assert f'case "{stage}"' in body

    def test_candidate_block_shows_estimated_and_elapsed_time_while_encoding(self, running_server):
        # Surfacing estimated vs. elapsed time makes the live "encoding"
        # wait visible instead of silent.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert 'job.status === "encoding" || job.status === "verifying" || job.status === "encoded"' in body
        assert "e.profile_name === job.profile_name" in body
        assert '"~" + estText + "/" + fmtSecondsShort(job.elapsed_seconds)' in body
        assert 'timeInfo.className = "compress-time-info"' in body

    def test_time_info_drops_the_encode_estimate_once_verifying(self, running_server):
        # compress.estimate_time() only ever models the ENCODE pass -- it
        # has no model for verify_output()'s extra decode+XPSNR passes (two
        # full decodes plus a combined XPSNR filter pass), which are real,
        # unmeasured wall-clock time. Comparing that encode-only estimate
        # against total elapsed-since-encode-started once verification has
        # begun would silently understate how much longer there is to go,
        # so only bare elapsed time is shown from "verifying" onward, never
        # a fabricated verify-time estimate (docs/technical/components/compression-mechanism.md:
        # an unmeasured number is never guessed, only marked "?").
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert 'if (job.status === "encoding") {' in body
        assert '"check " + fmtSecondsShort(job.elapsed_seconds)' in body

    def test_candidate_badge_highlights_queued_status_light_grey(self, running_server):
        # "w kolejce" (queued), and the no-job-yet fallback that means the
        # same thing, get a distinct modifier class -- CSS gives it a
        # light-grey highlight (the shared surface-2/muted tokens), kept
        # apart from the shared green "applied" color.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert '!job || job.status === "queued"' in body
        assert '(isQueued ? " applied-badge-queued" : "")' in body
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert ".applied-badge.applied-badge-queued" in css
        queued_rule = re.search(r'\.applied-badge\.applied-badge-queued\s*\{([^}]*)\}', css)
        assert queued_rule
        assert "var(--muted)" in queued_rule.group(1)
        assert "var(--surface-2)" in queued_rule.group(1)

    def test_candidate_badge_highlights_encoding_status_yellow(self, running_server):
        # job.status === "encoding" gets its OWN modifier class, distinct
        # from both the light-grey "queued" one above and the plain green
        # "applied" default -- an actively-encoding file must not look
        # identical to a finished one.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert 'const isEncoding = !!job && job.status === "encoding";' in body
        assert '(isEncoding ? " applied-badge-encoding" : "")' in body
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        encoding_rule = re.search(r'\.applied-badge\.applied-badge-encoding\s*\{([^}]*)\}', css)
        assert encoding_rule
        assert "var(--encoding-fg)" in encoding_rule.group(1)
        assert "var(--encoding-bg)" in encoding_rule.group(1)

    def test_candidate_badge_highlights_failed_status_red(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressCandidateBlock")
        assert 'const isFailed = !!job && job.status === "failed";' in body
        assert '(isFailed ? " applied-badge-failed" : "")' in body
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        failed_rule = re.search(r'\.applied-badge\.applied-badge-failed\s*\{([^}]*)\}', css)
        assert failed_rule
        assert "var(--danger-fg)" in failed_rule.group(1)
        assert "var(--danger-bg)" in failed_rule.group(1)
        # Both themes define the tokens.
        assert css.count("--danger-fg:") == 2

    def test_action_panel_shows_a_total_summary_once_the_queue_finishes(self, running_server):
        # "obok przycisku Kompresuj na koniec": next to the button, once
        # the queue is done (never while jobsRunning -- that is the
        # progress bar's job) -- total before/after size across every
        # DONE job, plus total time across every job (including a
        # failed/cancelled one's partial encoding time, since jobs never
        # run in parallel so summing each job's own elapsed time equals
        # the whole batch's wall-clock time).
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "} else {" in body
        assert 'const doneJobs = relevantJobs.filter((j) => j.status === "done");' in body
        assert "const totalBefore = doneJobs.reduce((sum, j) => sum + j.original_size, 0);" in body
        assert "const totalAfter = doneJobs.reduce((sum, j) => sum + j.result_size, 0);" in body
        assert "const totalElapsed = relevantJobs.reduce((sum, j) => sum + (j.elapsed_seconds || 0), 0);" in body
        assert 'i18n("js_comp_tot_comp")' in body
        assert "Czas kompresji: " in body
        assert 'summary.className = "compress-total-summary"' in body

    def test_action_panel_scopes_the_total_summary_to_the_current_directory(self, running_server):
        # Regression: tabData.compress.jobs is a single, global,
        # cross-directory queue (the queue keeps running in the background
        # regardless of which source directory's candidates are on
        # screen) -- switching the source-dir picker to a directory that
        # has never been compressed used to keep showing the PREVIOUS
        # directory's finished totals next to this, totally unrelated,
        # directory's own "Kompresuj" button. Ids are namespaced per
        # computation (next_compress_id_prefix()), so filtering jobs down
        # to only those whose group_id belongs to the CURRENTLY listed
        # candidates is exact, never a coincidental match.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert (
            "const currentGroupIds = new Set((tabData.compress.candidates || [])"
            ".map((c) => c.group_id));"
        ) in body
        assert (
            "const relevantJobs = (tabData.compress.jobs || [])"
            ".filter((j) => currentGroupIds.has(j.group_id));"
        ) in body

    def test_action_panel_shows_an_estimated_summary_before_the_first_compression(self, running_server):
        # "powinno sie przeliczac najpierw szacowane wartosci a po
        # kompresji realne": before anything in THIS directory has
        # actually finished, show an ESTIMATED preview instead of nothing
        # (or, before the scoping fix above, a stale REAL total left over
        # from a different directory) -- summed over exactly the
        # candidates submitApplyCompress() would submit right now
        # (selected, not skipped, not already applied) at whichever
        # profile is currently chosen, so it stays honest as either
        # changes.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "} else {" in body
        assert "for (const c of visibleCompressCandidates()) {" in body
        assert "if (c.applied) continue;" in body
        assert "if (!csel || csel.skip || !csel.selected) continue;" in body
        assert (
            "const est = (c.estimates || []).find((e) => e.profile_name === chosenProfileName);"
        ) in body
        # The select's value is "profile|device_key", never a bare profile name.
        assert "chosenProfileName = chosenOpt ? chosenOpt.dataset.profile : null;" in body
        assert "=== profileSelect.value" not in body
        assert 'i18n("js_comp_est_comp")' in body
        assert 'i18n("js_est_time")' in body

    def test_profile_change_re_renders_so_the_estimate_stays_in_sync(self, running_server):
        # No queue poll runs while idle, so nothing else re-renders when
        # only the profile dropdown changes -- without this, the
        # estimated-totals line would keep showing the PREVIOUSLY
        # selected profile's numbers until some unrelated state change
        # happened to trigger a render.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'profileSelect.addEventListener("change", () => {' in body
        assert "render();" in body.split('profileSelect.addEventListener("change"', 1)[1][:400]

    def test_total_summary_has_a_css_rule(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        assert ".compress-total-summary" in style_match.group(1)

    # -- columns: same --cols grid and control as the other three screens --

    def test_compress_candidate_list_uses_the_shared_cols_grid(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        css = style_match.group(1)
        assert re.search(
            r'\.compress-candidate-list\s*\{[^}]*grid-template-columns:\s*repeat\(var\(--cols',
            css,
        )

    def test_columns_control_stays_visible_on_compress_tab(self, running_server):
        # Kolumny used to live inside #page-controls, which is hidden on the
        # compress tab (it has no pages to size or page through) -- that hid
        # the columns picker too, even though compress candidate cards now
        # share the same --cols grid as the other three screens.
        html = self._html(running_server)
        body = self._function_body(html, "updateScreenVisibility")
        assert 'getElementById("columns-controls")' in body
        assert 'state.tab === "restore" ? "none" : ""' in body

    # -- the "remove originals" checkbox: the one field with no persistence -

    def test_remove_originals_checkbox_is_never_seeded_from_state(self, running_server):
        # This checkbox (docs/technical/components/review-ui.md) must default unchecked on every render
        # (including a poll-tick re-render while still selecting
        # candidates), because checking it moves files to !REMOVE without a
        # second click -- so the element it creates must never read a
        # `.checked =` from anywhere, and the panel builder that creates it
        # must never be seeded from `state`.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'removeBox.id = "compress-remove-originals"' in body
        assert "removeBox.checked" not in body
        assert "state.compressRemoveOriginals" not in html

    def test_submit_apply_compress_reads_remove_originals_off_the_dom_at_click_time(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitApplyCompress")
        assert 'document.getElementById("compress-remove-originals")' in body
        assert "removeBox.checked" in body
        assert '"/api/apply-compress"' in body
        assert "profile: profile" in body
        assert "remove_originals: removeOriginals" in body

    # -- the "allow lower quality" checkbox: same non-sticky contract -------

    def test_allow_lower_quality_checkbox_is_not_seeded_while_idle(self, running_server):
        # Same reasoning as the remove-originals checkbox above: a relaxed
        # quality bar chosen for one stuck file must never silently carry
        # into a later, unrelated apply via persisted state while idle.
        # It IS seeded from tabData.compress.submittedAllowLowerQuality
        # while a queue is actively running -- see the dedicated test
        # below (test_profile_and_relaxed_checkbox_show_the_running_
        # queues_own_settings) for why, and that the seeding is gated on
        # jobsRunning rather than unconditional.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'relaxedBox.id = "compress-allow-lower-quality"' in body
        assert "state.compressAllowLowerQuality" not in html
        assert (
            "if (tabData.compress.jobsRunning) {\n"
            "    relaxedBox.checked = tabData.compress.submittedAllowLowerQuality;\n"
            "  }"
        ) in body

    def test_submit_apply_compress_reads_allow_lower_quality_off_the_dom_at_click_time(
        self, running_server,
    ):
        html = self._html(running_server)
        body = self._function_body(html, "submitApplyCompress")
        assert 'document.getElementById("compress-allow-lower-quality")' in body
        assert "relaxedBox.checked" in body
        assert "allow_lower_quality: allowLowerQuality" in body

    def test_allow_lower_quality_label_has_a_css_rule(self, running_server):
        # Regression: the label had no matching .compress-allow-lower-
        # quality-label rule at all (unlike its siblings .compress-select-
        # label/.compress-remove-originals-label), so the checkbox and its
        # text rendered without the shared inline-flex/gap layout.
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert style_match
        assert ".compress-allow-lower-quality-label" in style_match.group(1)

    def test_profile_select_and_relaxed_checkbox_grey_out_while_queue_runs(self, running_server):
        # Both are read only at submitApplyCompress()'s click time -- once a
        # queue is already running, changing either implies a mid-queue
        # effect that does not exist, same reasoning as the "Kompresuj"
        # button's own existing btn.disabled.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "profileSelect.disabled = tabData.compress.jobsRunning;" in body
        assert "relaxedBox.disabled = tabData.compress.jobsRunning;" in body

    def test_profile_and_relaxed_checkbox_show_the_running_queues_own_settings(self, running_server):
        # Regression: this panel is rebuilt from scratch on every 1s GET
        # /api/compress/jobs poll (fetchCompressJobsOnce() -> render()).
        # Both controls used to be unconditionally unseeded, so the
        # visible value silently snapped back to the select's first
        # option / an unchecked box moments after clicking "Kompresuj",
        # even though the queue that had just started kept using
        # whatever was actually submitted. submitApplyCompress() now
        # captures what it sent into tabData.compress.submittedProfile/
        # submittedAllowLowerQuality, and the panel seeds the (disabled)
        # controls from those ONLY while a queue is running.
        html = self._html(running_server)
        submit_body = self._function_body(html, "submitApplyCompress")
        assert "tabData.compress.submittedProfile = profile;" in submit_body
        assert "tabData.compress.submittedAllowLowerQuality = allowLowerQuality;" in submit_body

        panel_body = self._function_body(html, "makeCompressActionPanel")
        assert (
            "if (tabData.compress.jobsRunning && tabData.compress.submittedProfile) {\n"
            "    pickOption(tabData.compress.submittedProfile, tabData.compress.submittedDeviceKey);\n"
            "  }"
        ) in panel_body
        assert (
            "if (tabData.compress.jobsRunning) {\n"
            "    relaxedBox.checked = tabData.compress.submittedAllowLowerQuality;\n"
            "  }"
        ) in panel_body

    # -- results panel: only a confirmed, verified row is selectable

    def test_results_panel_gives_every_removable_row_a_checkbox(self, running_server):
        # tabData.compress.removable only ever holds confirmed, verified
        # results (server-side: _compress_removable_candidates() only
        # offers a done+verified !MOVES row whose original AND compressed
        # file both still exist) -- so every row this function renders gets
        # one, and the queue's own per-file status (tabData.compress.jobs,
        # which CAN include failed/unverified entries) only ever surfaces
        # as a badge on each candidate card, never as a checkbox anywhere.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressResultsPanel")
        assert 'box.type = "checkbox"' in body
        assert '"/api/apply-compress-remove"' not in body  # posted by submitApplyCompressRemove(), not built here

    def test_results_panel_has_a_select_all_checkbox(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressResultsPanel")
        assert 'allBox.id = "compress-results-select-all"' in body
        assert 'i18n("js_comp_sel_all")' in body
        assert "removable.every((r) => compressRemoveSelection[r.file_id])" in body
        assert "removable.forEach((r) => { compressRemoveSelection[r.file_id] = e.target.checked; });" in body
        assert "syncAllBox();" in body.split("compressRemoveSelection[row.file_id] = e.target.checked;", 1)[1][:60]

    def test_results_select_all_has_a_css_rule(self, running_server):
        html = self._html(running_server)
        style_match = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
        assert ".compress-results-select-all" in style_match.group(1)

    def test_queue_progress_label_shows_estimated_and_elapsed_time(self, running_server):
        html = self._html(running_server)
        panel = self._function_body(html, "makeCompressActionPanel")
        assert 'i18n("js_comp_prog")' in panel
        body = self._function_body(html, "compressQueueTimeText")
        assert "e.profile_name === job.profile_name" in body
        assert "elapsed += job.elapsed_seconds || 0;" in body
        assert '"~" + (haveEstimate ? fmtSecondsShort(estSeconds) : "?") + "/" + fmtSecondsShort(elapsed)' in body

    def test_submit_apply_compress_remove_posts_to_the_remove_endpoint(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitApplyCompressRemove")
        assert '"/api/apply-compress-remove"' in body
        assert "buildCompressRemovePayload" in body

    def test_action_panel_has_no_checkbox(self, running_server):
        # The selectable list is the SEPARATE results panel above,
        # never this one -- the action panel itself is progress bar +
        # controls only.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert 'box.type = "checkbox"' not in body

    def test_action_panel_has_no_redundant_per_job_text_list(self, running_server):
        # Regression: this panel used to also render a plain-text <ul> of
        # every job's path+status+size below the progress bar -- purely
        # redundant with each candidate card's own live status badge
        # (compressJobStatusLabel()) and the progress bar already shown
        # above it, so it added a third copy of the same information with
        # no extra content.
        html = self._html(running_server)
        body = self._function_body(html, "makeCompressActionPanel")
        assert "compress-job-list" not in body
        assert "jobList" not in body

    # -- cancel wiring -------------------------------------------------------

    def test_cancel_compress_queue_posts_to_cancel_endpoint(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "cancelCompressQueue")
        assert '"/api/compress/cancel"' in body

    # -- rescan resets the compress tab like series/placement ---------------

    def test_rescan_handler_resets_compress_tab_data_and_selection(self, running_server):
        html = self._html(running_server)
        rescan_handler = html.split('"rescan-btn").addEventListener')[1]
        assert '"series", "placement", "compress"' in rescan_handler
        assert "tabData.compress.candidates = []" in rescan_handler
        assert "state.compressSelection = {}" in rescan_handler
