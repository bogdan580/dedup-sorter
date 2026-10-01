
// Theme switcher logic
function initTheme() {
  const savedTheme = localStorage.getItem("dedup_theme") || "light";
  document.documentElement.dataset.theme = savedTheme;
  const toggle = document.getElementById("theme-toggle");
  
  function updateIcon() {
    // If dark, show sun (click to go light). If light, show moon (click to go dark).
    toggle.textContent = document.documentElement.dataset.theme === "dark" ? "☀️" : "🌙";
    // Reuse the theme names from translations as titles
    toggle.title = document.documentElement.dataset.theme === "dark" ? i18n("theme_light") : i18n("theme_dark");
  }
  
  if (toggle) {
    updateIcon();
    toggle.addEventListener("click", () => {
      const newTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = newTheme;
      localStorage.setItem("dedup_theme", newTheme);
      updateIcon();
    });
  }
}

document.addEventListener("DOMContentLoaded", initTheme);

// Click handler for all three tab buttons -- flips
// state.tab, kicks off (or resumes) whichever background computation the
// tab needs, and calls render(). It must never call loadState(): that
// function calls initSelection(), which would wipe every keeper/skip/
// exclude choice the user already made on the "dup" screen. state.selection/
// state.page/state.columns are meant to survive any number of tab switches
// back and forth -- only this function's own tab, and the read-only
// series/placement caches above, ever change on a switch.
function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  if (tab === "series" || tab === "placement" || tab === "compress") {
    ensureTabData(tab);
  }
  if (tab === "placement" || tab === "compress") {
    ensureSourceDirs().then(renderSourceDirSelect);
  }
  if (tab === "compress") {
    ensureCompressEncoders();
    ensureCompressJobs();
  }
  // Lazy, one-shot load like the other two
  // tabs, but its own function -- GET /api/batches has no progress/polling
  // shape for ensureTabData()/pollTabData() to reuse (tabData.restore has
  // no `progress` field either).
  if (tab === "restore") {
    ensureRestoreData();
  }
  render();
}

async function loadState() {
  const resp = await fetch("/api/state");
  state.data = await resp.json();
  initSelection();
  // apply-all-btn/apply-page-btn's disabled state is computed
  // by updateApplyButtonsEnabled(), called from render() below -- it must
  // stay correct across tab switches too, not just right after this fetch.
  render();
}

// The results panel accumulates one entry per successful apply
// batch (state.applied_batches grows server-side, never overwritten) --
// appendChild(), never textContent =, so an earlier batch's outcome is
// never wiped out by a later one.
function appendResult(result) {
  const resultEl = document.getElementById("result");
  const entry = document.createElement("pre");
  entry.className = "result-entry";
  entry.textContent = JSON.stringify(result, null, 2);
  resultEl.appendChild(entry);
  // An apply result carries its own
  // batch_id -- offer to undo it right here, without a trip to the
  // "Restore" tab. `moved` is only present on an apply's own result (a
  // restore's result carries `restored` instead), so this never offers to
  // restore the very restore the user just triggered.
  if (result.batch_id && typeof result.moved === "number") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "apply-btn";
    btn.textContent = i18n("js_btn_restore_batch");
    btn.addEventListener("click", () => submitRestore(result.batch_id, result.moved));
    entry.appendChild(document.createElement("br"));
    entry.appendChild(btn);
  }
}

async function submitApply(payload) {
  const resp = await fetch("/api/apply", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await resp.json();
  appendResult(result);
  await loadState();
}

// Series' own submit path -- POST /api/apply-series, not
// /api/apply. Refreshes series data afterwards via refreshSeriesData()
// below, NOT loadState() (which only reloads the dup screen's own server
// state and would leave tabData.series showing groups the server has
// already applied).
async function submitApplySeries(payload) {
  const resp = await fetch("/api/apply-series", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await resp.json();
  appendResult(result);
  await refreshSeriesData();
}

// Re-fetches GET /api/series after a series apply. The server-side
// _LazyComputation is already "ready" at this point (a finished computation
// is never re-run), so this is a cheap re-fetch, not a new computation -- it
// exists purely to pick up the fresh "applied" flags handle_lazy() computes
// on every response from applied_series_group_ids (never cached inside the
// frozen result itself).
async function refreshSeriesData() {
  const resp = await fetch("/api/series");
  const payload = await resp.json();
  tabData.series.status = payload.status;
  tabData.series.groups = payload.groups || [];
  tabData.series.notes = payload.notes || {};
  tabData.series.error = payload.error;
  ensureSeriesSelection(tabData.series.groups);
  render();
}

// The placement screen's own submit path -- POST
// /api/apply-placement, which ends in applier.apply_moves(), not
// apply_batch(). Nothing on this path can send a file to !REMOVE.
async function submitApplyPlacement(selections) {
  const resp = await fetch("/api/apply-placement", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ selections: selections }),
  });
  const result = await resp.json();
  appendResult(result);
  await refreshPlacementData();
}

// Re-fetch after an apply, for the same reason refreshSeriesData() exists:
// the server-side computation is already "ready" and is never re-run, so
// this only picks up the fresh per-suggestion "applied" flags the handler
// stamps on every response. It also picks up a directory index that now
// includes any folder the apply just created.
async function refreshPlacementData() {
  const resp = await fetch("/api/placement?source=" + encodeURIComponent(state.placementSource));
  const payload = await resp.json();
  if (payload.source && payload.source !== state.placementSource) return;
  tabData.placement.status = payload.status;
  tabData.placement.folders = payload.folders || [];
  tabData.placement.notes = payload.notes || {};
  tabData.placement.error = payload.error;
  ensurePlacementSelection(tabData.placement.folders);
  render();
}

