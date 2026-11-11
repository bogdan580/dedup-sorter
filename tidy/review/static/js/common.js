const TOKEN = "__DEDUP_TOKEN__";
function i18n(key) {
  if (window.I18N && window.I18N[key]) {
    return window.I18N[key];
  }
  return key;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = i18n(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-title]").forEach(el => {
    el.title = i18n(el.getAttribute("data-i18n-title"));
  });
  document.querySelectorAll("[data-i18n-aria]").forEach(el => {
    el.setAttribute("aria-label", i18n(el.getAttribute("data-i18n-aria")));
  });
});


// Value lists for the dev-mode series tuning. DISPLAY lists, not defaults:
// the value in force (which may have come from --series-hamming-threshold on
// the command line) arrives with loadState(), and buildTuningSelect()
// appends it when it is not one of these -- a select unable to show the
// active value would misdescribe the groups on screen. The window list stops
// at 60 min because the window is the only bound on the pairwise Hamming
// comparison; the server refuses more regardless of what is sent.
const SERIES_HAMMING_CHOICES = [
  { value: 2, label: i18n("js_sim_2") },
  { value: 4, label: i18n("js_sim_4") },
  { value: 6, label: "6" },
  { value: 8, label: i18n("js_sim_8") },
  { value: 10, label: "10" },
  { value: 12, label: i18n("js_sim_12") },
  { value: 16, label: i18n("js_sim_16") },
];
const SERIES_WINDOW_CHOICES = [
  { value: 30, label: "30" + i18n("js_win_sec") },
  { value: 60, label: "1" + i18n("js_win_min") },
  { value: 120, label: "2" + i18n("js_win_min") },
  { value: 300, label: "5" + i18n("js_win_min") },
  { value: 600, label: "10" + i18n("js_win_min") },
  { value: 1200, label: "20" + i18n("js_win_min") },
  { value: 1800, label: "30" + i18n("js_win_min") },
  { value: 3600, label: "60" + i18n("js_win_min") },
];

const state = {
  data: null, tab: "dup",
  // Page number is per tab -- a global page number would land on an empty
  // page when switching from a deep page on one tab to a shallower one.
  // pageSize/columns stay shared across tabs.
  pages: { dup: 1, series: 1, placement: 1 },
  pageSize: 50, columns: 2, selection: {},
  // Per-series-group selection: {keepIds: Set<fileId>, skip}. Kept separate
  // from `selection` above because a series group can keep more than one
  // file, so the shape is genuinely different.
  seriesSelection: {},
  // Per-suggestion placement selection: {fileIds: Set<fileId> (blue, move),
  // removeIds: Set<fileId> (red, !REMOVE), dest: string, skip: bool}. `dest` lives in the selection rather than being read off
  // the DOM at submit time: the placement tab re-renders on every poll tick
  // while a directory is still computing, and the user's edit must survive.
  placementSelection: {},
  // Which directory the placement tab is looking at. Defaults to !NEW; the
  // option list comes from the server.
  placementSource: "!NEW",
  placementDirs: [],
  // Per-candidate compress selection: {selected: bool, skip: bool}. Kept
  // separate from placementSelection (different fields, different
  // group_id namespace -- "c1_..." vs "p...") for the same reason
  // seriesSelection is its own object rather than reusing `selection`.
  compressSelection: {},
  // Which directory the compress tab is looking at -- its OWN source, not
  // placementSource: the two tabs measure different things against the
  // same directory list, and a user comparing them (placement on
  // `#Home`, compress on `!NEW`) must not have picking one reset the
  // other. Shares the same underlying directory list (placementDirs) and
  // the same <select id="source-dir"> -- only which of these
  // two fields the dropdown's selected option reflects differs by tab
  // (renderSourceDirSelect()).
  compressSource: "!NEW",
  // Same toggle as showSkippedPlacement, for the compress screen's own
  // persisted "pomin".
  showSkippedCompress: false,
  // Series groups the user has skipped are hidden by default and
  // remembered in the cache DB across runs; this is the "show them anyway"
  // toggle. A view preference, not persisted server-side: the decision
  // belongs in the database, looking at it does not.
  showSkippedSeries: false,
  // Same toggle, for the placement screen's own persisted skip.
  showSkippedPlacement: false,
  // Holds the OPEN accordion sections of the placement notes block (closed
  // is the default), keyed by a fixed section name rather than any id a
  // rescan could renumber, and never sent to the server.
  placementNotesOpen: {},
  // Dev-mode flag, driven by the unlabeled bottom-left checkbox -- gates
  // visibility of #result and of #series-tuning. Kept on `state` so it
  // persists like every other view preference here, though it is never sent
  // to the server.
  devMode: false,
  // The user's PICK for the series thresholds, null until they touch the
  // control. null means "follow the server", which is what makes the
  // pending marker clear itself from the state loadState() fetched after a
  // Rescan instead of from the client's own memory of what it asked for.
  seriesTuning: { hamming: null, windowSeconds: null },
  // Thumbnail zoom (1 or 2), shared across all three tabs since makeCard()
  // is the same renderer everywhere.
  thumbZoom: 2,
};

function currentPage() {
  return state.pages[state.tab] || 1;
}

function setCurrentPage(page) {
  state.pages[state.tab] = page;
}

// Series/placement/restore RESULTS live in their own object, deliberately
// not as fields on `state` above -- `state`'s shape is pinned by an
// existing test. One entry per tab so switching tabs never re-triggers a
// fetch for a resource that is already computing/ready/error -- see
// ensureTabData()/pollTabData() below.
const tabData = {
  series: { status: "idle", groups: [], notes: {}, error: null, progress: { done: 0, total: 0 } },
  // The placement tab holds `folders` (each with its own suggestions), not
  // a flat `groups` list, since a folder can itself be one of three cases.
  placement: { status: "idle", folders: [], notes: {}, error: null, progress: { done: 0, total: 0 } },
  // The "Restore" tab's own batch list. No `progress`: GET /api/batches
  // answers synchronously, unlike series/placement's background
  // computations.
  // `expanded`/`entriesByBatch`/`visibleCounts` are keyed by batch_id,
  // separate from `batches` above -- expanding one row must never
  // fetch/store every other batch's files. `entriesByBatch` survives a
  // collapse (re-expanding the same batch does not refetch); `loadBatches()`
  // resets all three, since a fresh GET /api/batches can mean the
  // underlying files changed.
  restore: {
    status: "idle", batches: [], error: null,
    expanded: {}, entriesByBatch: {}, visibleCounts: {},
  },
  // The compress tab holds `candidates` (one-file "groups", like
  // placement's `folders` but flat -- see build_compress_views()'s own
  // docstring) plus `skipped` (files that didn't qualify at all, never
  // shown as candidates). The `jobs*`/`removable` fields are the async
  // encode queue's own state (GET /api/compress/jobs), polled separately
  // from the candidate list (see ensureCompressJobs()/pollCompressJobsLoop()
  // below) since the queue keeps running in the background regardless of
  // which source directory's candidates are on screen. `encoders*` is the
  // one-shot GET /api/compress/encoders result (ensureCompressEncoders()).
  compress: {
    status: "idle", candidates: [], skipped: [], error: null, progress: { done: 0, total: 0 },
    encoders: [], encodersAvailable: false, machineId: null, ffmpegVersion: null,
    unavailableReason: null,
    jobsRunning: false, jobs: [], jobsProgress: { done: 0, total: 0 }, removable: [],
    // What submitApplyCompress() actually sent for the queue CURRENTLY
    // running -- makeCompressActionPanel() seeds the (disabled) select/
    // checkbox from these while jobsRunning, instead of leaving them at
    // their non-sticky defaults, which used to visibly reset to "first
    // option"/"unchecked" on every 1s poll re-render even though the
    // running queue's own settings never changed.
    submittedProfile: null, submittedDeviceKey: null, submittedAllowLowerQuality: false,
    // The user's current idle choice in the i18n("js_comp_col_q") select (a profile
    // plus the device that runs it): the panel is rebuilt on every
    // render(), so without these a pick snapped straight back to the
    // first option.
    chosenDeviceKey: null, chosenProfile: null,
    // POST /api/compress/calibrate's own job status (docs/technical/components/compression-mechanism.md),
    // polled the same way as `jobs`/`removable` above (GET /api/compress/
    // jobs' `calibration` field) -- null until the first successful poll.
    calibration: null,
  },
};

// Rotate button: thumb_id -> a counter bumped on every successful POST
// /api/thumb/rotate for that id, appended to the <img> src as a
// cache-busting query string, since the browser would otherwise keep
// showing the pre-rotation bytes at the same URL. Kept outside `state` for
// the same reason tabData is: `state`'s shape is pinned by an existing test.
const thumbVersions = {};

// Own per-row checkbox state for the compress results
// panel: file_id -> bool. Kept outside `state`/`tabData` for the same
// reason thumbVersions/destUsage are -- it is pure UI bookkeeping over
// server data, not server data itself, and `state`'s shape is pinned by a
// test. Unlike the "remove originals" checkbox on the action panel, THIS
// selection is deliberately sticky across renders (results panel -- the
// user picks results to discard only after reviewing them, not as a side
// effect of anything else happening on screen).
const compressRemoveSelection = {};

// Picks the appropriate unit (B, KB, MB, GB, TB) via repeated 1024-based
// division, instead of always assuming "GB" -- a plain integer for the
// bytes range (e.g. "0 B", never "0.0 B"), 1 decimal place for KB and up
// (e.g. "3.0 MB", "512.0 KB", "1.5 TB").
function fmtSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return unitIndex === 0
    ? Math.round(value) + " " + units[unitIndex]
    : value.toFixed(1) + " " + units[unitIndex];
}

// No internal phase or step label may appear anywhere in the served page
// (tests/review/ asserts against it), so this comment
// deliberately names none.
// Per-group selection state, seeded fresh
// every time server state is (re)loaded. Default state -- keeperId is the
// server's own keeper id, excludedIds is empty, skip is false -- must
// reproduce exactly today's apply payload for a user who clicks nothing
// (see buildSelectionsPayload() below, which reads ONLY these two fields
// plus groupFileIds()'s server-supplied id list).
function initSelection() {
  state.selection = {};
  for (const g of state.data.groups) {
    state.selection[g.group_id] = { keeperId: g.keeper.id, excludedIds: new Set(), skip: false };
  }
}

// The complete id universe for a group -- what buildSelectionsPayload()
// and computeSelectionSummary() both filter against.
function groupFileIds(g) {
  return [g.keeper.id, ...g.discarded.map((d) => d.id)];
}

// Builds the step-3 POST /api/apply contract
// ({"selections": [{group_id, keeper_id, discard_ids}]}) purely from
// current per-group selection state, over exactly the `groups` passed in
// -- skipped groups are omitted entirely, and discard_ids is every id in
// the group except the current keeper and anything individually excluded
// via its checkbox. `groups` is the caller's
// job to narrow down (nonAppliedGroups(state.data.groups) for "Apply
// all", nonAppliedGroups(currentPageGroups()) for "Apply current
// page") -- this function itself has no notion of "all" vs "this page".
function buildSelectionsPayload(groups) {
  const selections = [];
  for (const g of groups) {
    const sel = state.selection[g.group_id];
    if (!sel || sel.skip) continue;
    const keeperId = sel.keeperId;
    const discardIds = groupFileIds(g).filter((id) => id !== keeperId && !sel.excludedIds.has(id));
    selections.push({ group_id: g.group_id, keeper_id: keeperId, discard_ids: discardIds });
  }
  return { selections };
}

// The "how many files / how many GB" counter tracks the *current*
// selection, not the server's original totals -- a skipped group and an
// individually-excluded file must both disappear from this count.
// Parameterized over `groups` for the same reason as
// buildSelectionsPayload() above -- each apply button's confirm() dialog
// needs the summary for exactly what THAT button will submit.
function computeSelectionSummary(groups) {
  let fileCount = 0;
  let bytes = 0;
  let groupCount = 0;
  for (const g of groups) {
    const sel = state.selection[g.group_id];
    if (!sel || sel.skip) continue;
    groupCount += 1;
    const discardIds = groupFileIds(g).filter((id) => id !== sel.keeperId && !sel.excludedIds.has(id));
    fileCount += discardIds.length;
    bytes += g.size * discardIds.length;
  }
  return { fileCount, bytes, groupCount };
}

// The series-screen equivalent of groupFileIds() -- a
// series group's file list lives in g.entries (one dict per file), not
// g.keeper/g.discarded like a dup group.
function seriesGroupFileIds(g) {
  return g.entries.map((e) => e.id);
}

// Builds the POST /api/apply-series contract
// ({"selections": [{group_id, keep_ids, discard_ids}]}) purely from
// current per-group seriesSelection state, over exactly the `groups`
// passed in -- mirrors buildSelectionsPayload() above, except keep_ids is
// a list (a series can keep more than one file) instead of a single
// keeper_id. A group contributes nothing if skipped, or if the current
// selection would discard nothing (keeping everything is a no-op, not an
// error -- silently omitted rather than sent as an empty discard_ids
// selection).
function buildSeriesSelectionsPayload(groups) {
  const selections = [];
  for (const g of groups) {
    const sel = state.seriesSelection[g.group_id];
    if (!sel || sel.skip) continue;
    const allIds = seriesGroupFileIds(g);
    const keepIds = allIds.filter((id) => sel.keepIds.has(id));
    const discardIds = allIds.filter((id) => !sel.keepIds.has(id));
    if (keepIds.length === 0 || discardIds.length === 0) continue;
    selections.push({ group_id: g.group_id, keep_ids: keepIds, discard_ids: discardIds });
  }
  return { selections };
}

// The series-screen equivalent of computeSelectionSummary() -- a series
// group has no single shared `size` (unlike a byte-identical duplicate
// group), so bytes must be summed from each discarded entry's own
// entry.size, never a `groupSize * count` shortcut.
function computeSeriesSelectionSummary(groups) {
  let fileCount = 0;
  let bytes = 0;
  let groupCount = 0;
  for (const g of groups) {
    const sel = state.seriesSelection[g.group_id];
    if (!sel || sel.skip) continue;
    const discardIds = new Set(seriesGroupFileIds(g).filter((id) => !sel.keepIds.has(id)));
    if (discardIds.size === 0) continue;
    groupCount += 1;
    fileCount += discardIds.size;
    for (const entry of g.entries) {
      if (discardIds.has(entry.id)) {
        bytes += entry.size;
      }
    }
  }
  return { fileCount, bytes, groupCount };
}

function updateSummary() {
  const summaryEl = document.getElementById("summary");
  if (state.tab === "series") {
    // visibleSeriesGroups() for the same reason the apply buttons use it:
    // the bar counts what this screen is offering, and a group hidden by a
    // persisted "pomin" is not part of that.
    const { fileCount, bytes, groupCount } = computeSeriesSelectionSummary(visibleSeriesGroups());
    summaryEl.textContent = groupCount + i18n("js_summary_groups") + fileCount + i18n("js_summary_del") + fmtSize(bytes);
    return;
  }
  if (state.tab === "placement") {
    const { fileCount, bytes, groupCount, removeCount, removeBytes } = computePlacementSummary(placementSuggestions());
    summaryEl.textContent = groupCount + i18n("js_summary_sugg") + fileCount + i18n("js_summary_move") + fmtSize(bytes) +
      (removeCount > 0 ? "; " + removeCount + i18n("js_summary_del") + fmtSize(removeBytes) : "");
    return;
  }
  const groups = state.data ? state.data.groups : [];
  const { fileCount, bytes, groupCount } = computeSelectionSummary(groups);
  summaryEl.textContent = groupCount + i18n("js_summary_groups") + fileCount + i18n("js_summary_del") + fmtSize(bytes);
}

// Groups already moved by a previous apply are excluded from future
// submissions client-side (the server rejects them anyway via
// applied_group_ids -- see _find_already_applied_group_id() -- this is
// just so the UI doesn't bother sending ids it knows will be rejected).
// Reused as-is for series groups too -- both carry the same "applied" flag.
function nonAppliedGroups(groups) {
  return groups.filter((g) => !g.applied);
}

// Per-group series selection state, seeded once per group (never re-seeded
// on a later poll tick, so a user's in-progress keep/skip choices survive
// the periodic background-computation status polling).
function ensureSeriesSelection(groups) {
  for (const g of groups) {
    if (!state.seriesSelection[g.group_id]) {
      state.seriesSelection[g.group_id] = { keepIds: new Set([g.suggested_keeper_id]), skip: false };
    }
    // Unlike keepIds above (seeded once, then owned by the user's clicks),
    // skip is re-synced from the server every time data arrives: the
    // database is the single source of truth for it.
    state.seriesSelection[g.group_id].skip = !!g.skipped;
  }
}

// The series groups this tab actually shows. Skipped ones are hidden --
// that IS the feature ("so it is not shown again even on the next
// run") -- unless "Show skipped groups" is ticked, which is the
// only way to reach one and un-skip it. Everything that pages, counts or
// submits goes through here, so a hidden group can never end up on a page
// nobody can see or in a payload nobody reviewed.
function visibleSeriesGroups() {
  const groups = tabData.series.groups || [];
  if (state.showSkippedSeries) return groups;
  return groups.filter((g) => !g.skipped);
}

function skippedSeriesCount() {
  return (tabData.series.groups || []).filter((g) => g.skipped).length;
}

// The `groups` visible on the current page for the current tab, per
// currentPage()/state.pageSize -- same
// arithmetic render() uses, factored out so "Apply current page"
// submits exactly what's on screen at click time. `groups` is the
// caller's job to pick (state.data.groups for "dup", tabData.series.groups
// for "series") -- this function itself has no notion of which tab it's
// paging.
function currentPageGroups(groups) {
  if (groups.length === 0) return [];
  const pageCount = pageCountFor(groups);
  const page = Math.min(Math.max(currentPage(), 1), pageCount);
  const start = (page - 1) * state.pageSize;
  return groups.slice(start, start + state.pageSize);
}

// Which groups the current tab pages through.
function groupsForCurrentTab() {
  if (state.tab === "dup") return state.data ? state.data.groups : [];
  if (state.tab === "series") return visibleSeriesGroups();
  // The placement tab pages over suggestions, not folders -- one mixed
  // folder can hold hundreds, so paging by folder would put an unbounded
  // number of cards on a single page.
  if (state.tab === "placement") return placementSuggestions();
  return [];
}

// Shared pager UI update (page indicator text + first/prev/next/last
// button disabled state), used by both the "dup" and "series" branches of
// render() so the two screens' pagers behave identically. Clamps
// currentPage() into [1, pageCount] first.
function updatePagerControls(groups) {
  const firstBtn = document.getElementById("first-page");
  const prevBtn = document.getElementById("prev-page");
  const nextBtn = document.getElementById("next-page");
  const lastBtn = document.getElementById("last-page");
  const indicator = document.getElementById("page-indicator");
  const pageCount = pageCountFor(groups);
  if (currentPage() < 1) {
    setCurrentPage(1);
  }
  if (currentPage() > pageCount) {
    setCurrentPage(pageCount);
  }
  indicator.textContent = i18n("js_page") + currentPage() + i18n("js_of") + pageCount;
  firstBtn.disabled = currentPage() === 1;
  prevBtn.disabled = currentPage() === 1;
  nextBtn.disabled = currentPage() === pageCount;
  lastBtn.disabled = currentPage() === pageCount;
}

// Pager state for an empty/not-ready screen -- i18n("js_page") + "1" + i18n("js_of") + "1", every button
// disabled. Mirrors the dup screen's empty-state handling, factored out so
// renderSeriesScreen() can reuse it for its i18n("js_comp_calc")/error/empty-result
// states too.
function disablePagerControls() {
  document.getElementById("first-page").disabled = true;
  document.getElementById("prev-page").disabled = true;
  document.getElementById("next-page").disabled = true;
  document.getElementById("last-page").disabled = true;
  document.getElementById("page-indicator").textContent = i18n("js_page") + "1" + i18n("js_of") + "1";
}

