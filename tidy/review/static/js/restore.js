// GET /api/batches, the first time the
// "Restore" tab is opened -- synchronous on the server (batches.py's own
// docstring: a dozen-odd directories at most), so this is a one-shot fetch,
// not a poll loop like ensureTabData()/pollTabData() above.
async function loadBatches() {
  tabData.restore.status = "loading";
  // A fresh batch list can mean the files themselves changed (an apply, a
  // restore, a rescan) -- any per-batch preview already fetched must not
  // be shown as still-current, so this clears it rather than leaving
  // stale entries behind a row whose restorable count just changed.
  tabData.restore.expanded = {};
  tabData.restore.entriesByBatch = {};
  tabData.restore.visibleCounts = {};
  render();
  try {
    const resp = await fetch("/api/batches");
    const payload = await resp.json();
    tabData.restore.batches = payload.batches || [];
    tabData.restore.status = "ready";
    tabData.restore.error = null;
  } catch (err) {
    tabData.restore.status = "error";
    tabData.restore.error = String(err);
  }
  render();
}

// Fetches one batch's
// entries the first time its row is expanded -- never on tab load, and
// never for a batch the user does not open (GET /api/batches itself stays
// a flat count). Toggling closed again just hides the grid; the fetched list
// stays cached in entriesByBatch so re-opening the same batch is free.
async function toggleBatchEntries(batchId) {
  if (tabData.restore.expanded[batchId]) {
    tabData.restore.expanded[batchId] = false;
    render();
    return;
  }
  tabData.restore.expanded[batchId] = true;
  if (tabData.restore.entriesByBatch[batchId]) {
    render();
    return;
  }
  tabData.restore.entriesByBatch[batchId] = { status: "loading", list: [], error: null };
  render();
  try {
    const resp = await fetch("/api/batch-entries?batch_id=" + encodeURIComponent(batchId));
    const payload = await resp.json();
    tabData.restore.entriesByBatch[batchId] = {
      status: payload.error ? "error" : "ready",
      list: payload.entries || [],
      error: payload.error || null,
    };
    tabData.restore.visibleCounts[batchId] = state.pageSize;
  } catch (err) {
    tabData.restore.entriesByBatch[batchId] = { status: "error", list: [], error: String(err) };
  }
  render();
}

// Companion to renderRestoreScreen() below: one batch's expanded file
// grid, appended as its own full-width table row directly under that
// batch's own row. Bounded to `visibleCount` cards (state.pageSize by
// default, the SAME page-size number every other screen already shares --
// see state.pageSize's own comment -- so a 135-file batch never dumps 135
// <img> tags at once) with a "show more" button for the rest, rather than
// the shared #page-controls pager: that pager is a single, tab-wide
// widget already hidden for this tab (updateScreenVisibility()), not one
// instance per expanded row.
function renderBatchEntriesRow(tbody, b) {
  const wrapRow = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 4;
  cell.style.padding = "0.3rem 0.3rem 0.8rem 1.8rem";

  const fetched = tabData.restore.entriesByBatch[b.batch_id];
  if (!fetched || fetched.status === "loading") {
    cell.appendChild(makeStatusMessage(i18n("js_loading_batch_files")));
  } else if (fetched.status === "error") {
    cell.appendChild(makeStatusMessage(i18n("js_rest_files_err") + fetched.error));
  } else if (fetched.list.length === 0) {
    cell.appendChild(makeStatusMessage(i18n("js_rest_no_files")));
  } else {
    const list = fetched.list;
    const visibleCount = tabData.restore.visibleCounts[b.batch_id] || state.pageSize;
    const visible = list.slice(0, visibleCount);
    // Same per-group path-highlighting the other three screens use
    // (computeGroupPathHighlights()), applied once over the whole visible
    // slice so every card in this batch agrees on the same shared-prefix
    // dimming -- reused as-is, not reimplemented, per the "podglad
    // miniaturek" request's own instruction to share the existing renderer.
    const pathHighlights = computeGroupPathHighlights(visible);
    const row = document.createElement("div");
    row.className = "row";
    visible.forEach((entry, i) => {
      const viewEntry = {
        id: b.batch_id + ":" + i,
        path: entry.path,
        size: entry.size,
        thumb_id: entry.thumb_id,
        thumb_kind: entry.thumb_kind,
      };
      const card = makeCard(viewEntry, false, "batch:" + b.batch_id, pathHighlights[i], { readOnly: true });
      if (!entry.restorable) {
        // Established fact only (batches.list_batch_entries()'s own
        // restorable check, mirrored server-side by
        // build_batch_entries_view()): this file's recorded location no
        // longer holds it, or something already occupies where restore
        // would put it back -- never a guess dressed up as a broken
        // thumbnail (the server already withheld thumb_id for exactly
        // this reason).
        const note = document.createElement("div");
        note.className = "hint";
        note.textContent = i18n("js_cant_restore");
        card.appendChild(note);
      }
      row.appendChild(card);
    });
    cell.appendChild(row);
    if (list.length > visibleCount) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "apply-btn";
      more.textContent = i18n("js_show_next") + (list.length - visibleCount) + ")";
      more.addEventListener("click", () => {
        tabData.restore.visibleCounts[b.batch_id] = visibleCount + state.pageSize;
        render();
      });
      cell.appendChild(more);
    }
  }
  wrapRow.appendChild(cell);
  tbody.appendChild(wrapRow);
}

function ensureRestoreData() {
  if (tabData.restore.status !== "idle") {
    return;
  }
  loadBatches();
}

// Shared by the "Restore" tab's per-row button AND appendResult()'s
// inline "Restore this batch" button -- one click, whole batch, same
// applier.restore_batch() a CLI --restore MANIFEST_PATH would run.
async function submitRestore(batchId, fileCount) {
  if (!confirm(
    i18n("js_restore_files_1") + fileCount + i18n("js_restore_files_2")
  )) {
    return;
  }
  const resp = await fetch("/api/restore", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ batch_id: batchId }),
  });
  const result = await resp.json();
  appendResult(result);
  // No full rescan: the computed views describe
  // the disk correctly again the moment the files are back, so refreshing
  // just needs the server's fresh "applied" flags -- which tab(s) actually
  // held this batch's groups isn't known here (a batch restored from the
  // "Restore" tab may predate this session, or come from a plain CLI
  // --apply run this server never scored), so every tab that has ever
  // loaded data is refreshed rather than guessed at.
  await loadState();
  if (tabData.series.status !== "idle") {
    await refreshSeriesData();
  }
  if (tabData.placement.status !== "idle") {
    await refreshPlacementData();
  }
  await loadBatches();
}

// The "Restore" tab -- a plain table (dates/counts don't need the
// card grid the other three screens share), one row per batch.list_batches()
// entry, newest first (server-side sort). A batch with restorable === 0
// gets a disabled-looking label instead of a button -- distinguishing
// "already restored" from "nothing ever succeeded" the same way
// batches.BatchSummary.already_restored does server-side.
function renderRestoreScreen() {
  const screen = document.getElementById("restore-screen");
  screen.innerHTML = "";

  if (tabData.restore.status === "loading" || tabData.restore.status === "idle") {
    screen.appendChild(makeStatusMessage(i18n("js_loading_batches")));
    return;
  }
  if (tabData.restore.status === "error") {
    screen.appendChild(makeStatusMessage(i18n("js_rest_list_err") + tabData.restore.error));
    return;
  }

  const items = tabData.restore.batches;
  if (items.length === 0) {
    // list_batches() only ever establishes "no batch had a readable
    // manifest.csv" -- it never checks whether !MOVES/!REMOVE exist, are
    // truly empty, or hold directories it simply skipped. Say only what was
    // established: nothing found, never that the directories are empty.
    screen.appendChild(makeStatusMessage(
      i18n("js_no_restore_found")
    ));
    return;
  }

  const table = document.createElement("table");
  table.style.width = "100%";
  table.style.borderCollapse = "collapse";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  [i18n("js_tbl_date"), i18n("js_tbl_type"), i18n("js_tbl_revert"), ""].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    th.style.textAlign = "left";
    th.style.borderBottom = "1px solid GrayText";
    th.style.padding = "0.3rem";
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  items.forEach((b) => {
    const row = document.createElement("tr");

    const dateCell = document.createElement("td");
    dateCell.style.padding = "0.3rem";
    dateCell.textContent = b.dir_name;
    row.appendChild(dateCell);

    const kindCell = document.createElement("td");
    kindCell.style.padding = "0.3rem";
    kindCell.textContent = b.kind === "move" ? "!MOVES" : "!REMOVE";
    row.appendChild(kindCell);

    const countCell = document.createElement("td");
    countCell.style.padding = "0.3rem";
    countCell.textContent = b.error ? "--" : String(b.restorable);
    row.appendChild(countCell);

    const actionCell = document.createElement("td");
    actionCell.style.padding = "0.3rem";
    if (b.error) {
      const label = document.createElement("span");
      label.className = "hint";
      label.textContent = i18n("js_broken_manifest") + b.error;
      actionCell.appendChild(label);
    } else if (b.restorable === 0) {
      const label = document.createElement("span");
      label.className = "hint";
      label.textContent = b.already_restored ? i18n("js_already_restored") : i18n("js_nothing_to_revert");
      actionCell.appendChild(label);
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "undo-btn";
      btn.textContent = i18n("js_btn_restore");
      btn.addEventListener("click", () => submitRestore(b.batch_id, b.restorable));
      actionCell.appendChild(btn);
    }
    // Offered whenever the batch has any status=done row, not just when
    // restorable > 0 -- a batch already fully restored elsewhere is
    // exactly the case where a user wants to confirm what it was.
    if (!b.error && b.done > 0) {
      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      toggleBtn.className = "apply-btn";
      toggleBtn.style.marginLeft = "0.4rem";
      toggleBtn.textContent = tabData.restore.expanded[b.batch_id] ? i18n("js_hide_files") : i18n("js_show_files");
      toggleBtn.addEventListener("click", () => toggleBatchEntries(b.batch_id));
      actionCell.appendChild(toggleBtn);
    }
    row.appendChild(actionCell);

    tbody.appendChild(row);
    if (tabData.restore.expanded[b.batch_id]) {
      renderBatchEntriesRow(tbody, b);
    }
  });
  table.appendChild(tbody);
  screen.appendChild(table);
}

