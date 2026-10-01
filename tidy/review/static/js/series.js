function renderSeriesScreen() {
  const screen = document.getElementById("series-screen");
  screen.innerHTML = "";
  const entry = tabData.series;

  if (entry.status === "error") {
    screen.appendChild(makeStatusMessage(i18n("js_ser_err_calc") + (entry.error || i18n("js_unknown_err"))));
    disablePagerControls();
    return;
  }
  if (entry.status !== "ready") {
    screen.appendChild(makeProgressMessage(i18n("js_comp_calc"), entry.progress));
    disablePagerControls();
    return;
  }
  if (entry.groups.length === 0) {
    screen.appendChild(makeStatusMessage(i18n("js_ser_no_grp")));
    disablePagerControls();
    return;
  }

  const visible = visibleSeriesGroups();
  if (visible.length === 0) {
    // Every group found is one the user already dismissed in this or an
    // earlier run -- say so explicitly instead of showing the same empty
    // screen as "no series found at all", which would look like data loss.
    screen.appendChild(makeStatusMessage(
      i18n("js_all_series_skipped_1") + entry.groups.length + i18n("js_all_series_skipped_2") +
      i18n("js_tick_show_skipped_grp"),
    ));
    disablePagerControls();
    return;
  }

  updatePagerControls(visible);
  const pageGroups = currentPageGroups(visible);

  for (const g of pageGroups) {
    const sel = state.seriesSelection[g.group_id] || { keepIds: new Set([g.suggested_keeper_id]), skip: false };

    const groupDiv = document.createElement("div");
    groupDiv.className = "group" + (g.applied ? " applied" : "") + (g.skipped ? " skipped" : "");

    const groupBar = document.createElement("div");
    groupBar.className = "group-bar";

    const infoSpan = document.createElement("span");
    infoSpan.textContent = i18n("js_series_lbl") + g.kind + i18n("js_span_lbl") + fmtGapSeconds(g.max_gap_seconds) +
      ", " + g.entries.length + i18n("js_files_lbl");
    groupBar.appendChild(infoSpan);

    if (g.applied) {
      const badge = document.createElement("span");
      badge.className = "applied-badge";
      badge.textContent = i18n("js_applied");
      groupBar.appendChild(badge);
    }
    // A series often gets shot entirely in one wrong orientation (camera
    // held sideways for the whole burst) -- one click rotates every
    // distinct photo in the group, unlike the dup screen where it's just
    // an alias for one shared thumb_id.
    const seriesThumbIds = g.entries.map((e) => e.thumb_id).filter(Boolean);
    if (seriesThumbIds.length > 0) {
      const rotateGroupBtn = document.createElement("button");
      rotateGroupBtn.type = "button";
      rotateGroupBtn.className = "rotate-group-btn";
      rotateGroupBtn.title = i18n("js_rotate_group_title");
      rotateGroupBtn.textContent = i18n("js_rotate_group_btn");
      rotateGroupBtn.addEventListener("click", () => rotateGroupThumbs(seriesThumbIds));
      groupBar.appendChild(rotateGroupBtn);
    }
    // Right side: the skip toggle, after the rotate button so both land in
    // the same right-aligned action cluster as the dup and placement bars.
    
      // Smart Select: Keep first
      if (!g.applied) {
        const keepFirstBtn = document.createElement("button");
        keepFirstBtn.type = "button";
        keepFirstBtn.className = "minimal-btn";
        keepFirstBtn.title = i18n("js_keep_first_title") || "Keep only the first photo";
        keepFirstBtn.textContent = i18n("js_keep_first_btn") || "✓ Keep one";
        keepFirstBtn.addEventListener("click", () => {
          sel.keepIds = new Set([g.entries[0].id]);
          sel.skip = false;
          render();
        });
        groupBar.appendChild(keepFirstBtn);
      }

      groupBar.appendChild(makeSeriesSkipControl(g, sel));
    groupDiv.appendChild(groupBar);

    const row = document.createElement("div");
    row.className = "row";
    const pathHighlights = computeGroupPathHighlights(g.entries);
    g.entries.forEach((fileEntry, i) => {
      const isKept = sel.keepIds.has(fileEntry.id);
      row.appendChild(makeCard(fileEntry, isKept, g.group_id, pathHighlights[i], { multiKeep: true }));
    });
    
      groupDiv.appendChild(row);

    screen.appendChild(groupDiv);
  }
}

