// A video with no thumbnail (ffmpeg missing,
// failed extraction, or a failed <img> load) gets a distinguishable
// placeholder from the generic "no preview" -- otherwise a user can't
// tell "this is a photo Pillow couldn't decode" from "this is a video, no
// preview was ever attempted/possible" at a glance.
function makePlaceholder(thumbKind) {
  const ph = document.createElement("div");
  ph.className = "placeholder";
  ph.textContent = thumbKind === "video" ? i18n("js_video_no_preview") : i18n("js_no_preview");
  return ph;
}

// Split an absolute path into its
// segments (every directory, then the filename last), plus the separator
// character actually used in that path -- so the card can re-render the
// path visually the same way, just as individual spans instead of one
// text blob. Consecutive separators (e.g. a leading "\\" on a UNC path)
// collapse into a single boundary; this only affects how the path is
// chopped up for display, entry.path itself is never touched.
function splitPathSegments(path) {
  const sepMatch = /[\\/]/.exec(path);
  const sep = sepMatch ? sepMatch[0] : "\\";
  const segments = path.split(/[\\/]+/).filter((s) => s !== "");
  return { segments, sep };
}

// Given the *directory* segments (filename already
// excluded) of every path in one group, find the first index at which they
// stop agreeing -- either because a segment differs or because a shorter
// path has already run out of directories. Segments before that index are
// the group's shared prefix (dimmed); returning `maxLen` means every path
// has the exact same directories, so there is nothing to highlight.
function firstDivergingDirIndex(dirSegmentsPerPath) {
  const maxLen = Math.max(0, ...dirSegmentsPerPath.map((d) => d.length));
  for (let i = 0; i < maxLen; i += 1) {
    const first = dirSegmentsPerPath[0][i];
    const allSame = dirSegmentsPerPath.every((d) => d.length > i && d[i] === first);
    if (!allSame) return i;
  }
  return maxLen;
}

// The actual per-group comparison, computed once per
// group (over keeper + every discarded entry together) and reused for
// every card in that group -- entirely client-side, from entry.path
// already present in the view model; no new endpoint, no new server data.
// A group whose members all share identical directories gets the same
// highlightFrom (== each path's own directory count) for every entry, so
// nothing is ever marked as different -- satisfying "identical directories
// -> no highlight" without a separate code path.
function computeGroupPathHighlights(entries) {
  const parsed = entries.map((e) => splitPathSegments(e.path));
  const dirsPerPath = parsed.map((p) => p.segments.slice(0, -1));
  const highlightFrom = firstDivergingDirIndex(dirsPerPath);
  return parsed.map((p) => ({ segments: p.segments, sep: p.sep, highlightFrom }));
}

// `options.readOnly` (default false, so every existing dup-screen call
// site keeps today's behavior) is the flag a read-only screen passes to
// reuse this exact renderer instead of copying it. Read-only means: no
// "skip this file" checkbox, a click never reassigns the keeper, and a
// dblclick does nothing but openFile().
//
// `options.multiKeep` is the series screen's own flag -- unlike readOnly's
// single, server-suggested keeper, every card the user has kept gets the
// .card.keeper outline (isKeeper is decided by the caller per-card, from
// seriesSelection.keepIds), a plain click toggles membership instead of
// reassigning a single keeper, and there is no "skip this file" checkbox
// (keepIds already expresses per-file exclusion).
function makeCard(entry, isKeeper, groupId, pathInfo, options) {
  options = options || {};
  const card = document.createElement("div");
  card.className = "card" + (isKeeper ? " keeper" : "");
  card.dataset.id = entry.id;
  // Rotate button lives per card, not per group -- unlike the dup screen
  // (one shared thumb_id per group), series/placement cards each show a
  // different photo with its own thumb_id. .thumb-wrap is the positioning
  // anchor (CSS: position: relative) so the button overlays this card's
  // own thumbnail corner regardless of the card's total width.
  const thumbWrap = document.createElement("div");
  thumbWrap.className = "thumb-wrap";
  if (entry.thumb_id) {
    const img = document.createElement("img");
    img.className = "thumb";
    const thumbVersion = thumbVersions[entry.thumb_id];
    img.src = "/api/thumb/" + encodeURIComponent(entry.thumb_id) +
      (thumbVersion ? "?v=" + thumbVersion : "");
    img.alt = "";
    // A thumbnail URL that 404s (or otherwise fails to load) must swap to
    // the same placeholder as "no thumb_id at all", never a broken-image
    // icon.
    img.onerror = () => {
      img.replaceWith(makePlaceholder(entry.thumb_kind));
    };
    thumbWrap.appendChild(img);
    const rotateBtn = document.createElement("button");
    rotateBtn.type = "button";
    rotateBtn.className = "rotate-btn";
    rotateBtn.title = i18n("js_rotate_thumb");
    rotateBtn.textContent = "↻";
    rotateBtn.addEventListener("click", (e) => {
      e.stopPropagation();  // must not also trigger the card's own click (keeper/keep-toggle)
      rotateThumb(entry.thumb_id);
    });
    thumbWrap.appendChild(rotateBtn);
  } else {
    thumbWrap.appendChild(makePlaceholder(entry.thumb_kind));
  }
  card.appendChild(thumbWrap);
  const size = document.createElement("div");
  size.className = "size";
  size.textContent = fmtSize(entry.size);
  card.appendChild(size);
  const path = document.createElement("div");
  path.className = "path";
  // Rendered as one <span> per path segment -- content set only via
  // textContent, never innerHTML, since directory/file names come straight
  // off disk (docs/technical/security.md).
  // Directory segments before pathInfo.highlightFrom are the group's
  // shared prefix (dimmed); from highlightFrom onward is where this
  // group's paths actually diverge (highlighted). The filename (last
  // segment) is never compared and never highlighted -- a different name
  // is not "a different folder".
  const lastIndex = pathInfo.segments.length - 1;
  pathInfo.segments.forEach((seg, i) => {
    const span = document.createElement("span");
    if (i === lastIndex) {
      span.className = "path-segment path-segment-file";
    } else {
      span.className = "path-segment path-segment-dir " +
        (i >= pathInfo.highlightFrom ? "path-segment-diff" : "path-segment-common");
    }
    span.textContent = seg;
    path.appendChild(span);
    if (i < lastIndex) {
      path.appendChild(document.createTextNode(pathInfo.sep));
    }
  });
  if (!isKeeper && !options.readOnly) {
    const reasonSpan = document.createElement("span");
    reasonSpan.className = "path-reason";
    reasonSpan.textContent = " (" + (entry.reason || "user-selected") + ")";
    path.appendChild(reasonSpan);
  }
  
    if (pathInfo.segments.includes("!NEW")) {
      const newBadge = document.createElement("span");
      newBadge.className = "new-badge";
      newBadge.textContent = "NEW";
      newBadge.title = "From the !NEW directory";
      path.appendChild(newBadge);
    }
    card.appendChild(path);

  if (!isKeeper && !options.readOnly && !options.multiKeep) {
    const label = document.createElement("label");
    label.className = "exclude-label";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "exclude-checkbox";
    const excludeSel = state.selection[groupId];
    checkbox.checked = excludeSel ? excludeSel.excludedIds.has(entry.id) : false;
    checkbox.addEventListener("click", (e) => e.stopPropagation());
    checkbox.addEventListener("change", (e) => {
      const sel = state.selection[groupId];
      if (!sel) return;
      if (e.target.checked) {
        sel.excludedIds.add(entry.id);
      } else {
        sel.excludedIds.delete(entry.id);
      }
      render();
    });
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(i18n("js_skip_file")));
    card.appendChild(label);
  }
  if (!options.readOnly && !options.multiKeep) {
    // Dup screen only: a plain click reassigns the keeper.
    card.addEventListener("click", () => {
      const sel = state.selection[groupId];
      if (!sel) return;
      sel.keeperId = entry.id;
      sel.excludedIds.delete(entry.id);
      render();
    });
  } else if (options.multiKeep && options.onToggle) {
    // The placement screen shares multiKeep's *interaction* (click toggles
    // membership, selected cards get the border) but not its bookkeeping --
    // its set means "files that move", lives in state.placementSelection,
    // and may legitimately be emptied, which the series branch below
    // deliberately forbids. An explicit callback keeps the two rules apart
    // instead of putting a screen test inside makeCard.
    card.addEventListener("click", () => { options.onToggle(); });
  } else if (options.multiKeep) {
    // Click toggles this file's membership in the group's keepIds. Refuses
    // to drop the last kept file via a plain click -- a group must keep at
    // least one file is enforced server-side regardless, but a client-side
    // no-op here avoids a click that silently produces an empty keepIds set
    // with no visual feedback (the card would just stay outlined, since
    // isKeeper is recomputed on the very next render() from the unchanged
    // keepIds).
    card.addEventListener("click", () => {
      const sel = state.seriesSelection[groupId];
      if (!sel) return;
      if (sel.keepIds.has(entry.id)) {
        if (sel.keepIds.size > 1) {
          sel.keepIds.delete(entry.id);
        }
      } else {
        sel.keepIds.add(entry.id);
      }
      render();
    });
  }
  card.addEventListener("dblclick", () => {
    if (!options.readOnly && !options.multiKeep) {
      const sel = state.selection[groupId];
      if (sel) {
        sel.keeperId = entry.id;
        sel.excludedIds.delete(entry.id);
        render();
      }
    }
    // For a read-only or multiKeep (series) card, this is the only effect a
    // dblclick has -- open the file, nothing else.
    openFile(entry.id);
  });
  return card;
}

// A failed open must be visible. This used to fire-and-forget the fetch,
// which is how a missing placement_registry lookup in /api/open (a plain
// 400 on every "Grouping suggestions" dblclick) stayed invisible: the
// server said "unknown id", the browser threw the answer away, and the
// card just sat there. Surfacing it through appendResult() is enough --
// the same "#result" log every apply/restore already writes to.
async function openFile(fileId) {
  let resp;
  try {
    resp = await fetch("/api/open", {
      method: "POST",
      headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ file_id: fileId }),
    });
  } catch (err) {
    appendResult({ error: i18n("js_open_err") + String(err) });
    return;
  }
  if (!resp.ok) {
    let detail = "HTTP " + resp.status;
    try {
      const body = await resp.json();
      if (body && body.error) detail = body.error;
    } catch (err) {
      // Non-JSON error body -- the status line is all there is to report.
    }
    appendResult({ error: i18n("js_open_err") + detail });
  }
}

// Per-card rotate button: POSTs to that card's own thumb_id, then bumps
// thumbVersions so render() cache-busts the <img> using that id and
// re-renders with the freshly rotated bytes.
async function rotateThumb(thumbId) {
  const resp = await fetch("/api/thumb/rotate", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ thumb_id: thumbId }),
  });
  if (resp.ok) {
    thumbVersions[thumbId] = (thumbVersions[thumbId] || 0) + 1;
    render();
  }
}

// Whole-group rotate button. thumbIds may repeat (dup groups pass the same
// shared id for every card) or list several distinct ones (series/
// placement, one photo per card) -- de-duplicated here so a shared id is
// only ever POSTed once.
// Rotates are fired in parallel and render() runs a single time at the end,
// not once per photo.
async function rotateGroupThumbs(thumbIds) {
  const uniqueIds = [...new Set(thumbIds)];
  const responses = await Promise.all(uniqueIds.map((id) => fetch("/api/thumb/rotate", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ thumb_id: id }),
  })));
  let anyRotated = false;
  responses.forEach((resp, i) => {
    if (resp.ok) {
      thumbVersions[uniqueIds[i]] = (thumbVersions[uniqueIds[i]] || 0) + 1;
      anyRotated = true;
    }
  });
  if (anyRotated) {
    render();
  }
}

