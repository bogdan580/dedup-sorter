// The placement screen ("Grouping suggestions") renders one source directory
// (default !NEW) measured against the rest of the archive, as
// folders -> suggestions -> files. The notes (non-media files, empty
// folders) render first, before any card -- buried under folder cards,
// nobody would ever see them.
// The folder whose existing contents were taken on these same days, if
// any. Used both as the pre-filled destination and to decide whether the
// destination currently in the box is a folder that already exists.
function bestDateMatch(suggestion) {
  return (suggestion.candidates || []).find((c) => c.why) || null;
}

// True when `dest` is a directory the server actually saw on disk. Every
// candidate comes from the directory walk, so membership there IS
// existence; the engine's own proposal carries its own flag.
function destExistsOnDisk(suggestion, dest) {
  const wanted = (dest || "").toLowerCase();
  if (!wanted) return false;
  if ((suggestion.candidates || []).some((c) => c.rel.toLowerCase() === wanted)) return true;
  return suggestion.dest_exists && suggestion.suggested_folder.toLowerCase() === wanted;
}

// The destinations the user set by hand during this session, counted, so a
// sorting run that keeps landing in the same few folders offers them
// instead of making the user find them again. Kept outside `state` (whose
// shape is pinned by a test) and outside `tabData` (server data only).
// Deliberately survives a rescan, unlike every selection: those are keyed
// by ids the rescan renumbers, while a folder path means the same thing
// before and after.
//
// Only explicit acts of setting count -- picking from the list, "New
// folder", or typing into the field (on `change`, i.e. once per edit, not
// per keystroke). The pre-filled destination the engine proposes does NOT:
// counting it would fill this list with folders the user never chose, which
// is the one thing that would make it worthless.
const POPULAR_DEST_LIMIT = 5;
const destUsage = { counts: new Map(), seq: 0 };

function recordDestUsage(dest) {
  const rel = (dest || "").trim();
  if (!rel) return;
  const prev = destUsage.counts.get(rel);
  destUsage.seq += 1;
  destUsage.counts.set(rel, { count: (prev ? prev.count : 0) + 1, seq: destUsage.seq });
}

// Top N by how often it was set, ties broken by most-recently-set. The
// tiebreak is not cosmetic: early in a session every count is 1, so without
// it the order would say nothing at all.
function popularDests(limit) {
  const out = [];
  destUsage.counts.forEach((info, rel) => {
    out.push({ rel: rel, count: info.count, seq: info.seq });
  });
  out.sort((a, b) => (b.count - a.count) || (b.seq - a.seq));
  return out.slice(0, limit);
}

function ensurePlacementSelection(folders) {
  for (const folder of folders) {
    for (const suggestion of folder.suggestions) {
      if (!state.placementSelection[suggestion.group_id]) {
        // When an existing folder already holds photos from these very
        // days, that folder is the pre-filled destination, not a hint next
        // to a different one. The field stays editable and the "New
        // folder" button puts the engine's name back.
        const match = bestDateMatch(suggestion);
        // Every file starts selected and the suggested folder starts as the
        // destination: the common case is "yes, that's right", and making
        // the user tick each photo to accept a correct suggestion would be
        // busy work. Unticking is how you disagree.
        state.placementSelection[suggestion.group_id] = {
          fileIds: new Set(suggestion.entries.map((e) => e.id)),
          dest: match ? match.rel : suggestion.suggested_folder,
          skip: false,
        };
      }
      // "pomin" is a row in the cache DB; the server reports it as
      // suggestion.skipped on every GET /api/placement. Re-synced from the
      // server every time data arrives, same reasoning as
      // ensureSeriesSelection()'s own re-sync: the database is the single
      // source of truth for it, and togglePlacementSkip() only writes here
      // after the server confirmed.
      state.placementSelection[suggestion.group_id].skip = !!suggestion.skipped;
    }
  }
}

// The pager runs over suggestions, not folders -- a single mixed folder can
// hold hundreds of them, so paging by folder would put an unbounded number
// of cards on one page.
//
// Skipped suggestions are hidden here -- same feature as
// visibleSeriesGroups(), unless i18n("js_show_skipped_sugg") is ticked, which
// is the only way to reach one and un-skip it. Every caller (pager, apply
// payload, summary) goes through this one function, so a hidden suggestion
// can never end up on a page nobody can see or in a payload nobody reviewed.
function placementSuggestions() {
  const out = [];
  for (const folder of tabData.placement.folders || []) {
    for (const suggestion of folder.suggestions) {
      if (!state.showSkippedPlacement && suggestion.skipped) continue;
      out.push({ folder, suggestion });
    }
  }
  return out;
}

function skippedPlacementCount() {
  let count = 0;
  for (const folder of tabData.placement.folders || []) {
    for (const suggestion of folder.suggestions) {
      if (suggestion.skipped) count += 1;
    }
  }
  return count;
}

// One foldable section of the notes block at the
// top of the placement screen. `key` is a fixed section name, not an index --
// a run with no camera-drift warnings must not shift "Pominiete" onto the
// warnings' fold state.
//
// The count moves INTO the heading (it was only on "Pominiete" before),
// because a folded section is otherwise a line that says a problem exists
// without saying how big it is -- and the size is what decides whether it is
// worth opening.
function appendNotesSection(notesBox, key, headingText, items, describe) {
  if (items.length === 0) return;
  const open = !!state.placementNotesOpen[key];

  const heading = document.createElement("button");
  heading.type = "button";
  heading.className = "notes-toggle";
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = open ? "\u25BE" : "\u25B8";
  heading.appendChild(caret);
  heading.appendChild(document.createTextNode(headingText + ": " + items.length));
  heading.setAttribute("aria-expanded", open ? "true" : "false");
  heading.title = open ? "Zwin liste" : "Rozwin liste";
  notesBox.appendChild(heading);

  const list = document.createElement("ul");
  items.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = describe(item);
    list.appendChild(li);
  });
  list.style.display = open ? "" : "none";
  notesBox.appendChild(list);

  // style.display rather than render(): the placement tab re-renders on every
  // poll tick while a directory is still computing, and rebuilding a
  // several-hundred-path list to flip one section would throw away the
  // scroll position each time. The <li>s are built either way -- these lists
  // come from the notes payload already in memory, so there is nothing to
  // defer.
  heading.addEventListener("click", () => {
    const next = !state.placementNotesOpen[key];
    if (next) {
      state.placementNotesOpen[key] = true;
    } else {
      delete state.placementNotesOpen[key];
    }
    list.style.display = next ? "" : "none";
    caret.textContent = next ? "\u25BE" : "\u25B8";
    heading.setAttribute("aria-expanded", next ? "true" : "false");
    heading.title = next ? "Zwin liste" : "Rozwin liste";
  });
}

function renderPlacementScreen() {
  const screen = document.getElementById("placement-screen");
  screen.innerHTML = "";
  const entry = tabData.placement;

  if (entry.status === "error") {
    screen.appendChild(makeStatusMessage(i18n("js_plac_err_calc") + (entry.error || i18n("js_unknown_err"))));
    disablePagerControls();
    return;
  }
  if (entry.status !== "ready") {
    screen.appendChild(makeProgressMessage(i18n("js_comp_calc"), entry.progress));
    disablePagerControls();
    return;
  }

  const notes = entry.notes || {};
  const skipped = notes.skipped_non_media || [];
  const emptyFolders = notes.empty_folders || [];
  const warnings = (entry.notes && entry.notes.camera_drift_warnings) || [];
  const suspicious = (entry.notes && entry.notes.suspicious) || [];
  if (skipped.length > 0 || emptyFolders.length > 0 || warnings.length > 0 || suspicious.length > 0) {
    const notesBox = document.createElement("div");
    notesBox.className = "notes-section";
    // This screen proposes a destination, so a photo whose camera clock
    // drifted is about to be filed under someone else's trip. Rendered
    // first, above the non-media/empty-folder notes.
    appendNotesSection(notesBox, "warnings", i18n("js_warn_clock_drift"),
                       warnings, (w) => w);
    appendNotesSection(notesBox, "suspicious", i18n("js_suspicious_dates"),
                       suspicious,
                       (item) => item.path + " (" + item.when + ", " + (item.camera_model || "?") + ")");
    appendNotesSection(notesBox, "skipped", i18n("js_skipped_non_media"),
                       skipped, (path) => path);
    appendNotesSection(notesBox, "emptyFolders", i18n("js_empty_folders"),
                       emptyFolders, (name) => name);
    screen.appendChild(notesBox);
  }

  const all = placementSuggestions();
  if (all.length === 0) {
    // Distinguishes "nothing here at all" from "everything here was
    // skipped". totalCount walks the raw (unfiltered) folders, since
    // placementSuggestions() above already hid the skipped ones.
    let totalCount = 0;
    for (const folder of tabData.placement.folders || []) {
      totalCount += folder.suggestions.length;
    }
    if (totalCount > 0) {
      screen.appendChild(makeStatusMessage(
        i18n("js_all_sugg_skipped_1") + totalCount + i18n("js_all_sugg_skipped_2") + state.placementSource +
         i18n("js_tick_show_skipped_sugg"),
      ));
      disablePagerControls();
      return;
    }
    screen.appendChild(makeStatusMessage(i18n("js_plac_no_sugg_dir").replace("{dir}", state.placementSource)));
    disablePagerControls();
    return;
  }

  updatePagerControls(all);
  const pageItems = currentPageGroups(all);
  let lastFolderId = null;
  for (const item of pageItems) {
    const folder = item.folder;
    const suggestion = item.suggestion;

    // One header per folder, not per suggestion: a mixed folder's dozen
    // suggestions all came out of the same drop, and repeating the header
    // for each would bury that.
    if (folder.folder_id !== lastFolderId) {
      lastFolderId = folder.folder_id;
      const folderBar = document.createElement("div");
      folderBar.className = "group-bar folder-bar";
      const label = folder.name === "" ? i18n("js_loose_files") : folder.name;
      const kindText = folder.whole_folder
        ? i18n("js_folder_coherent")
        : (folder.classification === "loose" ? i18n("js_folder_loose") : i18n("js_folder_mixed"));
      if (folder.name === "") {
        folderBar.textContent = i18n("js_directory") + label;
      } else {
        folderBar.textContent = i18n("js_directory") + label + " (" + kindText + ")";
      }
      screen.appendChild(folderBar);
    }

    screen.appendChild(makeSuggestionBlock(folder, suggestion));
  }
}

function makeSuggestionBlock(folder, suggestion) {
  const sel = state.placementSelection[suggestion.group_id];
  const block = document.createElement("div");
  // .skipped: same dashed/dimmed visual language as the series screen's
  // .group.skipped -- only reachable with i18n("js_show_skipped_sugg") on,
  // since placementSuggestions() hides it otherwise.
  block.className = "group" + (suggestion.skipped ? " skipped" : "");

  const bar = document.createElement("div");
  bar.className = "group-bar";

  if (suggestion.applied) {
    const badge = document.createElement("span");
    badge.className = "applied-badge";
    badge.textContent = i18n("js_applied").toLowerCase();
    bar.appendChild(badge);
  }

  // Destination controls (label/input/picker/"New directory") live in their
  // own wrapping row below .group-bar, not crammed into the header strip
  // shared with dup/series.
  const destRow = document.createElement("div");
  destRow.className = "dest-row";

  // The destination is an editable field seeded with the suggestion.
  // The server sanitises whatever comes back (relative path, no "..", no
  // drive letter, no Windows-illegal names) and applier re-checks
  // containment independently -- nothing typed here is trusted.
  const destLabel = document.createElement("label");
  destLabel.className = "dest-label";
  destLabel.textContent = i18n("js_dest_dir");
  const destInput = document.createElement("input");
  destInput.type = "text";
  destInput.className = "dest-input";
  destInput.value = sel.dest;
  destInput.disabled = !!suggestion.applied;
  destInput.addEventListener("input", () => {
    sel.dest = destInput.value;
    refreshDestState();
    updateSummary();
  });
  // `change`, not `input`: a typed destination counts as ONE act of setting
  // it, when the user leaves the field -- counting keystrokes would rank
  // every prefix of a folder name above the folder itself.
  destInput.addEventListener("change", () => {
    recordDestUsage(destInput.value);
  });
  destLabel.appendChild(destInput);
  destRow.appendChild(destLabel);

  // The way back to a brand-new folder once the field has been pre-filled
  // with an existing one. It fills in the engine's own "RRRR Miejsce" name
  // rather than blanking the field. Nothing is created here; the folder
  // comes into existence only when the move is applied.
  const newDirBtn = document.createElement("button");
  newDirBtn.type = "button";
  newDirBtn.className = "new-dir-btn";
  newDirBtn.textContent = i18n("js_new_folder");
  newDirBtn.title = i18n("js_new_folder_title") + suggestion.suggested_folder;
  newDirBtn.disabled = !!suggestion.applied;
  newDirBtn.addEventListener("click", () => {
    sel.dest = suggestion.suggested_folder;
    destInput.value = suggestion.suggested_folder;
    recordDestUsage(suggestion.suggested_folder);
    refreshDestState();
    updateSummary();
  });
  destRow.appendChild(newDirBtn);

  // Existing folders offered as a list, never auto-selected.
  // Picking one only fills the text field -- the field stays the single
  // source of truth, so a pick can still be edited afterwards.
  // The list is worth showing even for a suggestion with no candidates of
  // its own, once this session has folders the user keeps choosing -- that
  // is exactly the group whose destination has to be typed out by hand.
  if ((suggestion.candidates && suggestion.candidates.length > 0)
      || popularDests(POPULAR_DEST_LIMIT).length > 0) {
    const picker = document.createElement("select");
    picker.className = "dest-picker";
    picker.disabled = !!suggestion.applied;
    // Date matches get their own group at the top of the list. The
    // server already sorts them first, but position alone does not say
    // WHY they are first -- and that difference is the whole point: a
    // folder holding photos from these very days is a different kind of
    // answer from one that merely shares a word with the source name.
    const dated = (suggestion.candidates || []).filter((c) => c.why);
    const plain = (suggestion.candidates || []).filter((c) => !c.why && !c.outside);
    // The rest of the archive, kept below the scoped folders rather than
    // left out.
    const outside = (suggestion.candidates || []).filter((c) => !c.why && c.outside);
    function addGroup(label, cands, describe) {
      if (cands.length === 0) return;
      const group = document.createElement("optgroup");
      group.label = label;
      cands.forEach((cand) => {
        const opt = document.createElement("option");
        opt.value = cand.rel;
        opt.textContent = describe(cand);
        group.appendChild(opt);
      });
      picker.appendChild(group);
    }
    // Rebuilt rather than built once: the popular group changes as the user
    // works, and a destination set in a group two screens up must be in
    // this list the moment the list is opened, not after the next render.
    function fillPicker() {
      picker.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = i18n("js_choose_folder");
      picker.appendChild(placeholder);
      addGroup(i18n("js_matches_dates"), dated, (c) => c.rel + " -- " + c.why);
      // Between the two, deliberately: a date match is an answer about this
      // group; a popular folder is an answer about this session's sorting
      // run, which beats a plain name match but never a date match.
      addGroup(i18n("js_popular_dirs"), popularDests(POPULAR_DEST_LIMIT),
               (c) => c.rel + " (" + c.count + "x)");
      addGroup(i18n("js_existing_dirs"), plain,
               (c) => c.rel + " (" + c.file_count + i18n("js_files_parens"));
      addGroup(i18n("js_other_archive_dirs"), outside,
               (c) => c.rel + " (" + c.file_count + i18n("js_files_parens"));
      picker.value = "";
    }
    fillPicker();
    // Both events, because neither alone covers reopening the list: `focus`
    // does not fire on a select that is already focused, and `mousedown`
    // misses a keyboard user. Refilling twice is harmless -- it rebuilds
    // from the same counts.
    picker.addEventListener("focus", fillPicker);
    picker.addEventListener("mousedown", fillPicker);
    picker.addEventListener("change", () => {
      if (!picker.value) return;
      sel.dest = picker.value;
      destInput.value = picker.value;
      recordDestUsage(picker.value);
      picker.value = "";
      refreshDestState();
      updateSummary();
    });
    destRow.appendChild(picker);
  }

  // The source directory itself as a one-click destination: the answer to
  // "leave these here, in the folder I am sorting" for files sitting in a
  // subfolder of it. It is the same `rel` the server handed us in
  // /api/source-dirs, never a path composed here.
  const sourceDirBtn = document.createElement("button");
  sourceDirBtn.type = "button";
  sourceDirBtn.className = "source-dir-btn";
  sourceDirBtn.textContent = i18n("js_current_dir");
  sourceDirBtn.title = i18n("js_set_dest_to_src") + state.placementSource;
  sourceDirBtn.disabled = !!suggestion.applied;
  sourceDirBtn.addEventListener("click", () => {
    sel.dest = state.placementSource;
    destInput.value = state.placementSource;
    recordDestUsage(state.placementSource);
    refreshDestState();
    updateSummary();
  });
  destRow.appendChild(sourceDirBtn);

  // The colour describes the folder in the box RIGHT NOW, not the one the
  // engine started from -- the box is editable and pre-filled, so a state
  // pinned to the original proposal would drift into telling the user
  // "new folder" while they are about to move photos into a folder full
  // of them. (The bug this replaced was the same shape: events.py's
  // matched_existing_dir is true as soon as `#Home` exists, so
  // `#Home2 Bialka Tatrzanska`, never on disk, read as existing.)
  // Green = the folder is already on disk, orange = it will be created.
  // Carried by the label and the field's own
  // border rather than a word beside them, so the answer sits ON the thing
  // it is about -- one less place for the eye to travel to.
  function refreshDestState() {
    const exists = destExistsOnDisk(suggestion, sel.dest);
    destLabel.classList.toggle("dest-existing", exists);
    destLabel.classList.toggle("dest-new", !exists);
    destInput.classList.toggle("dest-existing", exists);
    destInput.classList.toggle("dest-new", !exists);
  }
  refreshDestState();

  // The bar does not repeat the date-match reason or the per-kind file
  // counts -- the matched folder is the value in the destination field
  // above, and the files themselves are listed directly below.

  const thumbIds = suggestion.entries.map((e) => e.thumb_id).filter(Boolean);
  if (thumbIds.length > 0) {
    const rotateGroupBtn = document.createElement("button");
    rotateGroupBtn.type = "button";
    rotateGroupBtn.className = "rotate-group-btn";
    rotateGroupBtn.title = i18n("js_rotate_group_title");
    rotateGroupBtn.textContent = i18n("js_rotate_group_btn");
    rotateGroupBtn.addEventListener("click", () => rotateGroupThumbs(thumbIds));
    bar.appendChild(rotateGroupBtn);
  }

  // The "pomin" checkbox matches the series screen: same shared
  // .skip-toggle look, same server round trip (POST /api/placement-skip)
  // rather than a purely client-side flag, so a skipped suggestion stays
  // skipped on the next run. suggestion.skipped (server-reported) is the
  // source of truth, same as g.skipped on the series screen --
  // ensurePlacementSelection() re-syncs sel.skip from it on every poll tick.
  const skipLabel = document.createElement("label");
  skipLabel.className = "skip-toggle skip-label";
  const skipBox = document.createElement("input");
  skipBox.type = "checkbox";
  skipBox.className = "skip-group-checkbox";
  skipBox.checked = !!sel.skip;
  skipBox.disabled = !!suggestion.applied;
  skipBox.addEventListener("change", (e) => {
    togglePlacementSkip(suggestion, e.target.checked);
  });
  skipLabel.appendChild(document.createTextNode(i18n("js_skip_group_saved")));
  skipLabel.appendChild(skipBox);
  bar.appendChild(skipLabel);

  block.appendChild(bar);
  block.appendChild(destRow);

  const row = document.createElement("div");
  row.className = "row";
  const pathHighlights = computeGroupPathHighlights(suggestion.entries);
  suggestion.entries.forEach((fileEntry, i) => {
    // multiKeep reuses the series screen's card behaviour: a click toggles
    // this file's membership in a set, and every selected card gets the
    // highlight border. The meaning of the set differs (here "files that
    // move", there "files that stay"), but the interaction is identical,
    // and a second card renderer would be a second place for the two
    // screens to drift apart.
    const selected = sel.fileIds.has(fileEntry.id);
    const card = makeCard(fileEntry, selected, suggestion.group_id, pathHighlights[i], {
      multiKeep: true,
      readOnly: !!suggestion.applied,
      onToggle: () => {
        if (sel.fileIds.has(fileEntry.id)) {
          sel.fileIds.delete(fileEntry.id);
        } else {
          sel.fileIds.add(fileEntry.id);
        }
        render();
      },
    });
    row.appendChild(card);
  });
  block.appendChild(row);

  return block;
}

// The submit payload for POST /api/apply-placement. Skipped
// suggestions, already-applied ones, and ones the user emptied by unticking
// every card are all left out -- empty file_ids is a 400 from the server,
// and sending one would turn "I don't want this group" into an error.
function buildPlacementPayload(items) {
  const selections = [];
  for (const item of items) {
    const suggestion = item.suggestion;
    if (suggestion.applied) continue;
    const sel = state.placementSelection[suggestion.group_id];
    if (!sel || sel.skip) continue;
    const fileIds = suggestion.entries.map((e) => e.id).filter((id) => sel.fileIds.has(id));
    if (fileIds.length === 0) continue;
    selections.push({
      group_id: suggestion.group_id,
      file_ids: fileIds,
      dest: sel.dest,
    });
  }
  return selections;
}

function computePlacementSummary(items) {
  let fileCount = 0;
  let bytes = 0;
  let groupCount = 0;
  for (const item of items) {
    const suggestion = item.suggestion;
    if (suggestion.applied) continue;
    const sel = state.placementSelection[suggestion.group_id];
    if (!sel || sel.skip) continue;
    const moving = suggestion.entries.filter((e) => sel.fileIds.has(e.id));
    if (moving.length === 0) continue;
    groupCount += 1;
    fileCount += moving.length;
    moving.forEach((e) => { bytes += e.size || 0; });
  }
  return { fileCount, bytes, groupCount };
}

// --- compress screen ("Sugestia kompresji") -------------------------------
//
// Same structure as the placement screen above, content swapped
// (docs/technical/components/review-ui.md): a source-directory picker (shared #source-dir, see
// renderSourceDirSelect()), a candidate list instead of folders/
// suggestions, and its own action panel + results panel instead of the
// shared #apply-bar -- a compress "apply" queues an async background job,
// not an immediate move, so it needs a i18n("js_comp_btn_comp") button and progress
// display the shared bar has no shape for (see updateScreenVisibility(),
// which hides #apply-bar/#page-controls for this tab exactly like it does
// for "restore").

