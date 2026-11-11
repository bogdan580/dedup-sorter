function updateScreenVisibility() {
  document.getElementById("dup-screen").style.display = state.tab === "dup" ? "" : "none";
  document.getElementById("series-screen").style.display = state.tab === "series" ? "" : "none";
  document.getElementById("placement-screen").style.display = state.tab === "placement" ? "" : "none";
  document.getElementById("compress-screen").style.display = state.tab === "compress" ? "" : "none";
  // "Restore" is its own container, same pattern as the other four --
  // exactly one screen ever visible.
  document.getElementById("restore-screen").style.display = state.tab === "restore" ? "" : "none";
  // "restore" has its own per-row "Restore" buttons instead of a group apply
  // flow, so it gets neither the apply bar nor the pager -- "compress" gets
  // neither either, for the same reason: its own action panel
  // (makeCompressActionPanel()) has its own i18n("js_comp_btn_comp") button and its own
  // results-panel apply button, not the shared per-page/all-pages pair.
  document.getElementById("top-apply-controls").style.display =
    (state.tab === "restore" || state.tab === "compress") ? "none" : "";
  // The footer itself stays on every tab (it carries the view controls);
  // only its pager is hidden where there are no pages.
  document.getElementById("bottom-pager").style.display =
    (state.tab === "restore" || state.tab === "compress") ? "none" : "";
  document.getElementById("page-controls").style.display =
    (state.tab === "restore" || state.tab === "compress") ? "none" : "";
  // Kolumny is its own control, unlike page-size/pager above: "compress" has
  // no pages, but its candidate cards still lay out in a --cols grid
  // (.compress-candidate-list), so the control stays visible there too.
  // Only "restore" (a plain list, no grid at all) hides it.
  document.getElementById("columns-controls").style.display =
    state.tab === "restore" ? "none" : "";
  // The source picker is shared by placement and compress -- the
  // other two tabs work off the whole scanned pool and have no directory to
  // choose.
  document.getElementById("source-controls").style.display =
    (state.tab === "placement" || state.tab === "compress") ? "" : "none";
  ["compress-action-slot", "compress-apply-slot", "compress-progress-slot"].forEach((id) => {
    document.getElementById(id).style.display = state.tab === "compress" ? "" : "none";
  });
  // Each tab with a persisted "pomin" has its own "show skipped" toggle,
  // shown only on that tab.
  document.getElementById("dup-controls").style.display =
    state.tab === "dup" ? "" : "none";
  const skippedDupLabel = document.getElementById("show-skipped-dup-label");
  if (skippedDupLabel) {
    const hidden = skippedDupCount();
    skippedDupLabel.textContent = hidden > 0
      ? i18n("js_show_skipped_groups") + " (" + hidden + ")"
      : i18n("js_show_skipped_groups");
  }
  document.getElementById("series-controls").style.display =
    state.tab === "series" ? "" : "none";
  const skippedLabel = document.getElementById("show-skipped-label");
  if (skippedLabel) {
    const hidden = skippedSeriesCount();
    // The count is the whole reason this control is visible even when
    // nothing is hidden: it is how the user finds out that a previous run's
    // decision is still in effect.
    skippedLabel.textContent = hidden > 0
      ? i18n("js_show_skipped_groups") + " (" + hidden + ")"
      : i18n("js_show_skipped_groups");
  }
  // Same toggle, placement-only, for the same reason -- "pomin" is
  // persisted for placement suggestions too.
  document.getElementById("placement-controls").style.display =
    state.tab === "placement" ? "" : "none";
  const skippedPlacementLabel = document.getElementById("show-skipped-placement-label");
  if (skippedPlacementLabel) {
    const hidden = skippedPlacementCount();
    skippedPlacementLabel.textContent = hidden > 0
      ? i18n("js_show_skipped_sugg") + " (" + hidden + ")"
      : i18n("js_show_skipped_sugg");
  }
  // Same toggle, compress-only, for the compress screen's own persisted
  // "pomin".
  document.getElementById("compress-controls").style.display =
    state.tab === "compress" ? "" : "none";
  const skippedCompressLabel = document.getElementById("show-skipped-compress-label");
  if (skippedCompressLabel) {
    const hidden = skippedCompressCount();
    skippedCompressLabel.textContent = hidden > 0
      ? i18n("js_show_skipped_comp") + " (" + hidden + ")"
      : i18n("js_show_skipped_comp");
  }
  // Called from here rather than only from the dev-mode checkbox: its
  // visibility depends on the active tab too, and this is the one function
  // every tab switch and every poll tick already goes through.
  updateSeriesTuningControls();
}

// apply-all-btn/apply-page-btn's enabled state depends on which tab is
// active. Called from render() so it stays correct across every tab switch
// and every series/placement poll tick, not just after the initial page
// load.
function updateApplyButtonsEnabled() {
  const allBtn = document.getElementById("apply-all-btn");
  const pageBtn = document.getElementById("apply-page-btn");
  let disable = true;
  if (state.tab === "dup") {
    // visibleDupGroups(), same reasoning as the series branch below.
    const groups = visibleDupGroups();
    disable = !state.data || state.data.all_applied || groups.length === 0 ||
      groups.every((g) => g.applied || g.skipped);
  } else if (state.tab === "series") {
    // visibleSeriesGroups(), not the raw list: a scan where every group is
    // already skipped has nothing left to apply, and an enabled button that
    // submits an empty selection would just look broken.
    const groups = visibleSeriesGroups();
    disable = tabData.series.status !== "ready" || groups.length === 0 ||
      groups.every((g) => g.applied || g.skipped);
  } else if (state.tab === "placement") {
    // placementSuggestions() already excludes skipped items unless "Show
    // skipped suggestions" is on (same reasoning as the series branch above)
    // -- when it IS on, a visible-but-skipped item must not count as
    // something left to submit either.
    const items = placementSuggestions();
    disable = tabData.placement.status !== "ready" || items.length === 0 ||
      items.every((item) => item.suggestion.applied || item.suggestion.skipped);
  }
  allBtn.disabled = disable;
  pageBtn.disabled = disable;
}

// Shared "this whole screen is one message" renderer -- reuses the same
// .placeholder-screen class the dup screen uses for "No groups
// of duplicates...", so i18n("js_comp_calc"), an error, and "no results" all look
// consistent across every tab.
function makeStatusMessage(text) {
  const div = document.createElement("div");
  div.className = "placeholder-screen";
  div.textContent = text;
  return div;
}

// Same "whole screen is one message" shell as makeStatusMessage() above,
// plus a native <progress> element and a "done / total" line, shown only
// once `progress.total > 0` (the server hasn't set a real total yet on the
// very first poll or two, and (0, 0) must not render as a full/empty bar).
// A plain <progress> picks up the page's own color-scheme automatically in
// every modern browser, so this needs no extra CSS to look right in both
// themes. `barWidth` overrides the default bar width (the compress queue's
// bar in the action panel is wider than the full-screen i18n("js_comp_calc") one).
function makeProgressMessage(text, progress, barWidth) {
  const div = document.createElement("div");
  div.className = "placeholder-screen";
  // Inline in a toolbar row (barWidth given), not a whole-screen message.
  if (barWidth) div.style.padding = "0";
  const label = document.createElement("div");
  label.textContent = text;
  div.appendChild(label);
  if (progress && progress.total > 0) {
    const bar = document.createElement("progress");
    bar.max = progress.total;
    bar.value = progress.done;
    bar.style.width = barWidth || "min(320px, 80%)";
    bar.style.maxWidth = "100%";
    bar.style.display = "block";
    bar.style.margin = "0.75rem auto 0.25rem";
    div.appendChild(bar);
    const counts = document.createElement("div");
    counts.style.fontSize = "0.8rem";
    counts.style.opacity = "0.75";
    counts.textContent = progress.done + " / " + progress.total;
    div.appendChild(counts);
  }
  return div;
}

// max_gap_seconds is a plain number
// of seconds from the series engine; shown as minutes once it is large
// enough that raw seconds would be hard to read at a glance.
function fmtGapSeconds(seconds) {
  if (seconds >= 60) {
    return (seconds / 60).toFixed(1) + " min";
  }
  return Math.round(seconds) + " s";
}

// GET /api/series or /api/placement, called the first time
// a tab is opened (from switchTab() below) -- start_if_idle() server-side
// (and the tabData[tab].status !== "idle" guard here) together guarantee
// this fires at most once per resource per page load, no matter how many
// times the user switches back to this tab.
function ensureTabData(tab) {
  if (tabData[tab].status !== "idle") {
    return;
  }
  pollTabData(tab);
}

// "computing" reschedules itself via setTimeout(...) in
// about a second; "ready" or "error" stop the loop -- there is no
// unconditional recursive call here, so polling can never run forever.
// Switching away from this tab does NOT cancel the pending setTimeout --
// the computation (server-side) keeps running regardless of which tab is
// on screen, and this keeps polling it in the background so the cached
// result is already there the moment the user switches back; render() is
// only called when this tab is the one currently on screen.
function pollTabData(tab) {
  // The placement endpoint is parameterised by source directory -- the
  // value is one the server itself handed us in /api/source-dirs, and the
  // server re-validates membership on arrival, so a stale or hand-edited
  // value is rejected rather than followed.
  const endpoint = tab === "series"
    ? "/api/series"
    : tab === "placement"
    ? "/api/placement?source=" + encodeURIComponent(state.placementSource)
    : "/api/compress?source=" + encodeURIComponent(state.compressSource);
  tabData[tab].status = "computing";
  if (state.tab === tab) {
    render();
  }
  fetch(endpoint)
    .then((resp) => resp.json())
    .then((payload) => {
      tabData[tab].status = payload.status;
      tabData[tab].groups = payload.groups || [];
      tabData[tab].notes = payload.notes || {};
      tabData[tab].error = payload.error;
      tabData[tab].progress = payload.progress || { done: 0, total: 0 };
      if (tab === "placement") {
        // A response for a directory the user has since switched away from
        // must not overwrite the current one: the in-flight fetch for the
        // old source can land after the switch. The server echoes back which
        // source it answered for precisely so this check is possible.
        if (payload.source && payload.source !== state.placementSource) {
          return;
        }
        tabData.placement.folders = payload.folders || [];
        ensurePlacementSelection(tabData.placement.folders);
      }
      if (tab === "compress") {
        // Same stale-response guard as placement's own, above -- the
        // server echoes back which source it answered for.
        if (payload.source && payload.source !== state.compressSource) {
          return;
        }
        tabData.compress.candidates = payload.candidates || [];
        tabData.compress.skipped = payload.skipped || [];
        if (payload.unavailable_reason) {
          tabData.compress.unavailableReason = payload.unavailable_reason;
        }
        ensureCompressSelection(tabData.compress.candidates);
      }
      if (tab === "series") {
        // Seed each series group's keep/skip selection the
        // moment its data first arrives -- ensureSeriesSelection() is a
        // no-op for any group_id already seeded, so this is safe to call
        // on every poll tick, not just the first "ready" one.
        ensureSeriesSelection(tabData.series.groups);
      }
      if (payload.status === "computing") {
        setTimeout(() => pollTabData(tab), 1000);
      }
      if (state.tab === tab) {
        render();
      }
    });
}

