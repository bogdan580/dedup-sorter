// Fetches the source-directory list from the server, lazily and then
// cached -- the list only changes when an apply creates a folder
// (refreshPlacementData() covers that case for the destination candidates,
// which is where a newly created folder actually matters) or when the
// archive itself changed under us, which is exactly what Rescan means. The
// rescan handler therefore clears this flag: without it the picker kept
// rendering the file counts captured at startup, so a batch dropped into
// `!NEW` between runs stayed invisible in the dropdown even though the
// server had already re-walked the disk.
//
// Shared by the placement AND compress tabs (same directory
// list, same picker element) -- each keeps its OWN chosen directory
// (state.placementSource / state.compressSource) so switching one tab's
// selection never moves the other's. Falling back to payload.default is
// right on each field's first load (still the built-in guess there) but
// wrong on a re-fetch after a rescan, which is why each is only reset when
// the fresh walk no longer reports it, exactly as before this tab existed.
let sourceDirsLoaded = false;
async function ensureSourceDirs() {
  if (sourceDirsLoaded) return;
  sourceDirsLoaded = true;
  const resp = await fetch("/api/source-dirs");
  const payload = await resp.json();
  state.placementDirs = payload.dirs || [];
  const stillTherePlacement = state.placementDirs.some((d) => d.rel === state.placementSource);
  if (!stillTherePlacement && payload.default) {
    state.placementSource = payload.default;
  }
  const stillThereCompress = state.placementDirs.some((d) => d.rel === state.compressSource);
  if (!stillThereCompress && payload.default) {
    state.compressSource = payload.default;
  }
}

// (Re)builds the shared <select id="source-dir">'s options from
// state.placementDirs and marks the ACTIVE tab's own source as selected --
// separate from the fetch above (ensureSourceDirs()) because switching
// between placement and compress must re-mark the selected option every
// time, even on the second, third, ... visit to a tab, when the one-shot
// fetch above is long since a no-op.
function renderSourceDirSelect() {
  const select = document.getElementById("source-dir");
  const current = state.tab === "compress" ? state.compressSource : state.placementSource;
  select.innerHTML = "";
  state.placementDirs.forEach((dir) => {
    const opt = document.createElement("option");
    opt.value = dir.rel;
    opt.textContent = dir.rel + " (" + dir.file_count + i18n("js_files_parens");
    if (dir.rel === current) opt.selected = true;
    select.appendChild(opt);
  });
  const hint = document.getElementById("source-hint");
  hint.textContent = state.placementDirs.length === 0
    ? i18n("js_no_source_dirs")
    : "";
  render();
}

document.getElementById("source-dir").addEventListener("change", (e) => {
  // Switching source resets the active tab to "idle" so ensureTabData()
  // starts a computation for the new directory. Selections are NOT
  // cleared: they are keyed by suggestion/candidate id, and a user who
  // flips to another directory and back should find their work intact --
  // the same guarantee switchTab() gives every tab.
  //
  // That only holds because ids are unique ACROSS computations
  // (ReviewState.next_placement_id_prefix()/next_compress_id_prefix()).
  // They were not, once: every placement computation numbered from p1, so
  // ensurePlacementSelection() -- which skips a group_id it already knows
  // -- handed the previous directory's ticked-file set and destination to
  // a different group of the same number. Cards outside that stale set
  // rendered unticked, which is how the user noticed. The same collision
  // reached the server, where it was moving the wrong files; the fix is
  // there, not here.
  if (state.tab === "compress") {
    state.compressSource = e.target.value;
    tabData.compress.status = "idle";
    tabData.compress.candidates = [];
    tabData.compress.skipped = [];
    tabData.compress.error = null;
    // Same reasoning as placement's own reset below: the bar belongs to
    // the directory being computed, and the new one starts its own
    // server-side counter at (0, 0).
    tabData.compress.progress = { done: 0, total: 0 };
    ensureTabData("compress");
    render();
    return;
  }
  state.placementSource = e.target.value;
  state.pages.placement = 1;
  tabData.placement.status = "idle";
  tabData.placement.folders = [];
  tabData.placement.notes = {};
  tabData.placement.error = null;
  // The bar belongs to the directory being computed, and the new one has
  // its own server-side counter starting at (0, 0). Without this reset the
  // first render after the switch shows the PREVIOUS directory's finished
  // bar -- a full "812 / 812" under i18n("js_comp_calc") -- until the first poll for
  // the new directory answers a second later.
  tabData.placement.progress = { done: 0, total: 0 };
  ensureTabData("placement");
  render();
});

// Wires switchTab() (defined above) to every tab
// button -- deliberately a plain click listener with no async/loadState()
// call anywhere in it, see switchTab()'s own comment for why.
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// Rescan: re-walk the archive, then drop every cached tab so each one
// recomputes from the fresh file list on next view. The button disables
// itself for the duration -- a second walk started on top of the first
// would fight it for the same SQLite cache and tell the user nothing new.
document.getElementById("rescan-btn").addEventListener("click", async () => {
  const btn = document.getElementById("rescan-btn");
  const status = document.getElementById("rescan-status");
  btn.disabled = true;
  status.textContent = i18n("js_scan_progress");
  try {
    // The dev-mode series thresholds ride along here -- this is the ONLY
    // request that carries them, which is what makes "zmiana zadziala po
    // kliknieciu Rescan" true rather than merely documented. Keys are
    // omitted while loadState() has not landed yet, and the server then
    // keeps whatever it already had.
    const tuning = chosenSeriesTuning();
    const rescanBody = {};
    if (typeof tuning.hamming === "number") {
      rescanBody.series_hamming_threshold = tuning.hamming;
    }
    if (typeof tuning.windowSeconds === "number") {
      rescanBody.series_window_seconds = tuning.windowSeconds;
    }
    const resp = await fetch("/api/rescan", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify(rescanBody),
    });
    const data = await resp.json();
    if (!resp.ok) {
      // A refused threshold leaves the pick in place (and the pending
      // marker up): the server scanned nothing, so nothing about the
      // client's state is stale.
      status.textContent = i18n("js_scan_err") + (data.error || resp.status);
      return;
    }
    // The server has taken the values -- go back to following the server so
    // the pending marker clears from what loadState() reports below, never
    // from this client's memory of what it asked for.
    state.seriesTuning = { hamming: null, windowSeconds: null };
    // tabData holds only the three computed tabs -- the duplicates tab's
    // data arrives with loadState() below instead.
    ["series", "placement", "compress"].forEach((tab) => {
      tabData[tab].status = "idle";
      tabData[tab].error = null;
      tabData[tab].notes = {};
      tabData[tab].progress = { done: 0, total: 0 };
    });
    tabData.series.groups = [];
    tabData.placement.folders = [];
    tabData.compress.candidates = [];
    tabData.compress.skipped = [];
    // Batches themselves are untouched by a rescan (it never walks
    // !MOVES/!REMOVE), but the server clears batch_list_cache anyway (a
    // rescan is the one moment stale caches get swept on principle), so the
    // client re-fetches too rather than trusting a now-possibly-stale list.
    tabData.restore.status = "idle";
    tabData.restore.error = null;
    tabData.restore.batches = [];
    ["dup", "series", "placement"].forEach((tab) => { state.pages[tab] = 1; });
    // Every selection is keyed by an id the rescan just renumbered, so
    // keeping any of it would apply the user's old choices to whichever
    // unrelated group inherited the number.
    state.selection = {};
    state.seriesSelection = {};
    state.placementSelection = {};
    state.compressSelection = {};
    // Both halves matter: the array is the data, `sourceDirsLoaded` is the
    // one-shot guard that would otherwise make ensureSourceDirs() return
    // without re-fetching, leaving the picker showing the file counts from
    // session start -- the whole point of pressing Rescan is that those
    // counts moved.
    state.placementDirs = [];
    sourceDirsLoaded = false;
    await loadState();
    // Awaited before ensureTabData() so the picker is rebuilt (and
    // state.placementSource/state.compressSource re-validated against the
    // fresh walk) before the placement/compress computation is started for
    // whichever one is on screen.
    if (state.tab === "placement" || state.tab === "compress") {
      await ensureSourceDirs();
      renderSourceDirSelect();
    }
    if (state.tab === "series" || state.tab === "placement" || state.tab === "compress") {
      ensureTabData(state.tab);
    }
    if (state.tab === "compress") {
      ensureCompressJobs();
    }
    if (state.tab === "restore") ensureRestoreData();
    render();
    status.textContent = i18n("js_scan_done") + data.candidates + " files, "
      + data.duplicate_groups + i18n("js_scan_dup_groups");
  } catch (err) {
    status.textContent = i18n("js_scan_err") + err;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("page-size").addEventListener("change", (e) => {
  state.pageSize = parseInt(e.target.value, 10);
  setCurrentPage(1);
  render();
});

document.getElementById("columns").addEventListener("change", (e) => {
  state.columns = parseInt(e.target.value, 10);
  render();
});

document.getElementById("thumb-zoom").addEventListener("change", (e) => {
  state.thumbZoom = parseInt(e.target.value, 10);
  render();
});

// Reveals the groups a persisted "pomin" is hiding. Resets to page 1 for
// the same reason the page-size control does -- the visible group list
// just changed length, and the current page number may no longer exist in
// it.
document.getElementById("show-skipped-series").addEventListener("change", (e) => {
  state.showSkippedSeries = e.target.checked;
  setCurrentPage(1);
  render();
});

// Both tuning selects write to `state` and do NOTHING else -- no fetch, no
// re-render of the group list. The value reaches the engine only via the
// next POST /api/rescan, which is why the hint turns into a reminder
// instead of the screen changing under the user.
document.getElementById("series-hamming").addEventListener("change", (e) => {
  state.seriesTuning.hamming = parseInt(e.target.value, 10);
  updateSeriesTuningControls();
});

document.getElementById("series-window").addEventListener("change", (e) => {
  state.seriesTuning.windowSeconds = parseInt(e.target.value, 10);
  updateSeriesTuningControls();
});

// Same toggle, for the placement screen's own persisted "pomin".
document.getElementById("show-skipped-placement").addEventListener("change", (e) => {
  state.showSkippedPlacement = e.target.checked;
  setCurrentPage(1);
  render();
});

// Same toggle, for the compress screen's own persisted "pomin" -- no pager
// on that tab, so no setCurrentPage().
document.getElementById("show-skipped-compress").addEventListener("change", (e) => {
  state.showSkippedCompress = e.target.checked;
  render();
});

document.getElementById("first-page").addEventListener("click", () => {
  setCurrentPage(1);
  render();
});

document.getElementById("prev-page").addEventListener("click", () => {
  setCurrentPage(currentPage() - 1);
  render();
});

document.getElementById("next-page").addEventListener("click", () => {
  setCurrentPage(currentPage() + 1);
  render();
});

document.getElementById("last-page").addEventListener("click", () => {
  const pageCount = pageCountFor(groupsForCurrentTab());
  setCurrentPage(pageCount);
  render();
});

document.getElementById("apply-all-btn").addEventListener("click", async () => {
  if (state.tab === "placement") {
    // The confirm() text names the destination, not !REMOVE -- this is the
    // one apply path in the tool that does not throw anything away, and
    // saying "do !REMOVE" here would be a lie about what the button does.
    // It still names --restore, because the reversal route is exactly the
    // same one.
    const selections = buildPlacementPayload(placementSuggestions());
    if (selections.length === 0) {
      document.getElementById("result").textContent = i18n("js_apply_no_placement");
      return;
    }
    const summary = computePlacementSummary(placementSuggestions());
    if (!confirm(
      i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
      ") z " + summary.groupCount + i18n("js_conf_sugg_all")
    )) {
      return;
    }
    await submitApplyPlacement(selections);
    return;
  }
  if (state.tab === "series") {
    // Series' own path -- buildSeriesSelectionsPayload()/
    // computeSeriesSelectionSummary()/submitApplySeries(), never the dup
    // screen's equivalents. The confirm() text is deliberately different:
    // series members are not byte-identical, unlike an exact-duplicate
    // group.
    // visibleSeriesGroups(), not the raw list: "Apply all" means everything
    // this screen is offering, and a group hidden by a persisted "pomin" is
    // by definition not being offered. buildSeriesSelectionsPayload() would
    // drop them anyway (their sel.skip is set from the server's flag), so
    // this is belt and braces.
    const groups = nonAppliedGroups(visibleSeriesGroups());
    const payload = buildSeriesSelectionsPayload(groups);
    if (payload.selections.length === 0) {
      document.getElementById("result").textContent = i18n("js_apply_no_series");
      return;
    }
    const summary = computeSeriesSelectionSummary(groups);
    if (!confirm(
      i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
      ") z " + summary.groupCount + i18n("js_conf_ser_all")
    )) {
      return;
    }
    await submitApplySeries(payload);
    return;
  }
  const groups = nonAppliedGroups(state.data ? state.data.groups : []);
  const payload = buildSelectionsPayload(groups);
  if (payload.selections.length === 0) {
    document.getElementById("result").textContent = i18n("js_apply_no_series");
    return;
  }
  const summary = computeSelectionSummary(groups);
  if (!confirm(
    i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
    ") z " + summary.groupCount + i18n("js_conf_dup_all")
  )) {
    return;
  }
  await submitApply(payload);
});

// The window is what scrolls (main sits in normal flow under a sticky
// topbar), so the whole page goes back to the top -- not one screen's grid.
document.getElementById("scroll-top-btn").addEventListener("click", () => {
  window.scrollTo({ top: 0, behavior: "smooth" });
});

document.getElementById("apply-page-btn").addEventListener("click", async () => {
  if (state.tab === "placement") {
    const pageItems = currentPageGroups(placementSuggestions());
    const selections = buildPlacementPayload(pageItems);
    if (selections.length === 0) {
      document.getElementById("result").textContent = i18n("js_apply_no_placement_page");
      return;
    }
    const summary = computePlacementSummary(pageItems);
    if (!confirm(
      i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
      ") z " + summary.groupCount + i18n("js_conf_sugg_page")
    )) {
      return;
    }
    await submitApplyPlacement(selections);
    return;
  }
  if (state.tab === "series") {
    // Must be visibleSeriesGroups():
    // currentPageGroups() slices whatever list it is handed, and
    // renderSeriesScreen() renders the FILTERED one -- paging the unfiltered
    // list here would submit groups the user is not looking at.
    const groups = nonAppliedGroups(currentPageGroups(visibleSeriesGroups()));
    const payload = buildSeriesSelectionsPayload(groups);
    if (payload.selections.length === 0) {
      document.getElementById("result").textContent = i18n("js_apply_no_series_page");
      return;
    }
    const summary = computeSeriesSelectionSummary(groups);
    if (!confirm(
      i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
      ") z " + summary.groupCount + i18n("js_conf_ser_page")
    )) {
      return;
    }
    await submitApplySeries(payload);
    return;
  }
  const groups = nonAppliedGroups(currentPageGroups(state.data ? state.data.groups : []));
  const payload = buildSelectionsPayload(groups);
  if (payload.selections.length === 0) {
    document.getElementById("result").textContent = i18n("js_apply_no_series_page");
    return;
  }
  const summary = computeSelectionSummary(groups);
  if (!confirm(
    i18n("js_conf_move") + summary.fileCount + i18n("js_conf_files") + fmtSize(summary.bytes) +
    ") z " + summary.groupCount + i18n("js_conf_dup_page")
  )) {
    return;
  }
  await submitApply(payload);
});

// Flips #result's visibility live, including
// for entries appendResult() already appended before the checkbox was
// touched -- toggling a CSS class on the container (not per-entry) is
// what makes that "already accumulated" case free, since every past and
// future <pre class="result-entry"> lives inside the same #result.
document.getElementById("dev-mode-toggle").addEventListener("change", (event) => {
  state.devMode = event.target.checked;
  document.getElementById("result").classList.toggle("dev-visible", state.devMode);
  // The series tuning hides behind the same switch. Called directly rather
  // than through render(): flipping this checkbox changes no data, and a
  // full re-render on a visibility toggle would rebuild every card.
  updateSeriesTuningControls();
});

// [?] help bubbles: a click toggles one open (closing the rest); a click
// anywhere else or Escape closes them. preventDefault keeps the enclosing
// <label> from moving focus to its <select>.
function closeTooltips(except) {
  document.querySelectorAll(".tooltip.open").forEach((t) => {
    if (t !== except) {
      t.classList.remove("open");
      t.setAttribute("aria-expanded", "false");
    }
  });
}
document.addEventListener("click", (e) => {
  const tip = e.target.closest(".tooltip");
  closeTooltips(tip);
  if (!tip) return;
  e.preventDefault();
  const open = tip.classList.toggle("open");
  tip.setAttribute("aria-expanded", String(open));
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeTooltips(null);
});

loadState();
