// "Series and similar" reuses makeCard()/computeGroupPathHighlights() from the
// dup screen above with { multiKeep: true }: every card the user has kept
// gets the same .card.keeper outline the dup screen's single keeper gets,
// a click toggles membership instead of reassigning a single keeper, and
// there is no per-file "skip this file" checkbox (keepIds already
// expresses that). Paginated and with its own apply bar exactly like the
// dup screen, via the same #apply-bar/#page-controls DOM and the same
// updatePagerControls()/currentPageGroups() helpers, just fed
// tabData.series.groups instead of state.data.groups.
// POSTs the "pomin" decision instead of only flipping a client-side flag --
// the server writes it to the cache DB, so it is still there on the next
// run. The local flag is set only after the server confirms: a failed
// write that still hid the group on screen would be the worst outcome. On
// failure the checkbox is put back where it was and the error is reported
// like any other.
async function toggleSeriesSkip(group, wantSkip) {
  let resp;
  try {
    resp = await fetch("/api/series-skip", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: group.group_id, skip: wantSkip }),
    });
  } catch (err) {
    appendResult({ error: i18n("js_err_save_skip") + String(err) });
    render();
    return;
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    // Non-JSON body -- the status line below is all there is to report.
  }
  if (!resp.ok) {
    appendResult({
      error: i18n("js_err_save_skip") +
        ((body && body.error) || ("HTTP " + resp.status)),
    });
    render();
    return;
  }
  group.skipped = wantSkip;
  const sel = state.seriesSelection[group.group_id];
  if (sel) sel.skip = wantSkip;
  // A session without a db_path can still skip, it just cannot remember --
  // said once, per group, rather than silently downgrading the promise.
  if (body && body.persisted === false) {
    appendResult({
      error: i18n("js_warn_no_db"),
    });
  }
  render();
}

// The placement screen's own "pomin", persisted the same way -- POST
// /api/placement-skip instead of /api/series-skip, suggestion.group_id/
// skipped instead of group.group_id/skipped, otherwise identical shape and
// reasoning to toggleSeriesSkip() above (local flag set only after the
// server confirms, failure reported and the checkbox left as it was).
async function togglePlacementSkip(suggestion, wantSkip) {
  let resp;
  try {
    resp = await fetch("/api/placement-skip", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: suggestion.group_id, skip: wantSkip }),
    });
  } catch (err) {
    appendResult({ error: i18n("js_err_save_skip") + String(err) });
    render();
    return;
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    // Non-JSON body -- the status line below is all there is to report.
  }
  if (!resp.ok) {
    appendResult({
      error: i18n("js_err_save_skip") +
        ((body && body.error) || ("HTTP " + resp.status)),
    });
    render();
    return;
  }
  suggestion.skipped = wantSkip;
  const sel = state.placementSelection[suggestion.group_id];
  if (sel) sel.skip = wantSkip;
  if (body && body.persisted === false) {
    appendResult({
      error: i18n("js_warn_no_db"),
    });
  }
  render();
}

// The compress screen's own "pomin", POST /api/compress/skip instead of
// /api/placement-skip -- otherwise an exact mirror of togglePlacementSkip()
// above (same local-flag-after-server-confirms rule, same failure/no-db
// reporting).
async function toggleCompressSkip(candidate, wantSkip) {
  let resp;
  try {
    resp = await fetch("/api/compress/skip", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: candidate.group_id, skip: wantSkip }),
    });
  } catch (err) {
    appendResult({ error: i18n("js_err_save_skip") + String(err) });
    render();
    return;
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    // Non-JSON body -- the status line below is all there is to report.
  }
  if (!resp.ok) {
    appendResult({
      error: i18n("js_err_save_skip") +
        ((body && body.error) || ("HTTP " + resp.status)),
    });
    render();
    return;
  }
  candidate.skipped = wantSkip;
  const sel = state.compressSelection[candidate.group_id];
  if (sel) sel.skip = wantSkip;
  if (body && body.persisted === false) {
    appendResult({
      error: i18n("js_warn_no_db"),
    });
  }
  render();
}

// Seeds each candidate's compress selection the moment its data first
// arrives -- a no-op for any group_id already seeded, mirroring
// ensurePlacementSelection() exactly. Every file starts selected (same
// "yes, that's right is the common case" reasoning as placement's own
// fileIds seeding) -- unticking is how you opt a candidate out of the next
// i18n("js_comp_btn_comp") click. `skip` is re-synced from the server on every poll
// tick, same as everywhere else in this file: the cache DB is the single
// source of truth for it.
function ensureCompressSelection(candidates) {
  for (const c of candidates) {
    if (!state.compressSelection[c.group_id]) {
      state.compressSelection[c.group_id] = { selected: true, skip: false };
    }
    state.compressSelection[c.group_id].skip = !!c.skipped;
  }
}

// Skipped candidates are hidden here unless "Show skipped candidates" is
// ticked -- same feature, same reasoning as visibleSeriesGroups()/
// placementSuggestions(). Every caller (render, the apply payload) goes
// through this one function.
function visibleCompressCandidates() {
  const candidates = tabData.compress.candidates || [];
  if (state.showSkippedCompress) return candidates;
  return candidates.filter((c) => !c.skipped);
}

function skippedCompressCount() {
  return (tabData.compress.candidates || []).filter((c) => c.skipped).length;
}

// The "Skip this group" checkbox for a series group, rendered once in the
// group bar (a later request moved it out of the bottom-right corner it
// used to occupy). Label text sits to the left of the checkbox so it reads
// naturally right up against the box.
function makeSeriesSkipControl(group, sel) {
  const label = document.createElement("label");
  label.className = "skip-toggle";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "skip-group-checkbox";
  box.checked = !!sel.skip;
  box.addEventListener("change", (e) => {
    toggleSeriesSkip(group, e.target.checked);
  });
  label.appendChild(document.createTextNode(i18n("js_skip_group_saved")));
  label.appendChild(box);
  return label;
}

// The exact-duplicates screen's own "pomin", POST /api/dup-skip -- same
// local-flag-after-server-confirms rule as toggleSeriesSkip().
async function toggleDupSkip(group, wantSkip) {
  let resp;
  try {
    resp = await fetch("/api/dup-skip", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: group.group_id, skip: wantSkip }),
    });
  } catch (err) {
    appendResult({ error: i18n("js_err_save_skip") + String(err) });
    render();
    return;
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (err) {
    // Non-JSON body -- the status line below is all there is to report.
  }
  if (!resp.ok) {
    appendResult({
      error: i18n("js_err_save_skip") +
        ((body && body.error) || ("HTTP " + resp.status)),
    });
    render();
    return;
  }
  group.skipped = wantSkip;
  const sel = state.selection[group.group_id];
  if (sel) sel.skip = wantSkip;
  if (body && body.persisted === false) {
    appendResult({
      error: i18n("js_warn_no_db"),
    });
  }
  render();
}

// The dup group bar's "Skip this group" checkbox, same look as the series one.
function makeDupSkipControl(group, sel) {
  const label = document.createElement("label");
  label.className = "skip-toggle";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "skip-group-checkbox";
  box.checked = !!sel.skip;
  box.addEventListener("change", (e) => {
    toggleDupSkip(group, e.target.checked);
  });
  label.appendChild(document.createTextNode(i18n("js_skip_group_saved")));
  label.appendChild(box);
  return label;
}

