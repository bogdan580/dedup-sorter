function pageCountFor(groups) {
  return Math.max(1, Math.ceil(groups.length / state.pageSize));
}

function render() {
  updateSummary();
  // Exactly one of the three screens is ever visible -- toggled by
  // style.display here, never by tearing down/rebuilding DOM nodes, so a
  // background series/placement computation started on an earlier visit
  // keeps running/cached regardless of which tab is on screen right now.
  updateScreenVisibility();
  updateApplyButtonsEnabled();
  // Set once, on the root element, rather than per-screen -- CSS custom
  // properties inherit, so #dup-screen/#series-screen/#placement-screen (all
  // three read var(--cols)/var(--thumb-scale)) pick this up uniformly,
  // regardless of which one is actually visible right now.
  document.documentElement.style.setProperty("--cols", String(state.columns));
  document.documentElement.style.setProperty("--thumb-scale", String(state.thumbZoom));

  if (state.tab === "dup") {
    const screen = document.getElementById("dup-screen");
    screen.innerHTML = "";

    if (!state.data) {
      return;
    }

    const groups = state.data.groups;

    if (groups.length === 0) {
      const empty = document.createElement("div");
      empty.className = "placeholder-screen";
      empty.textContent = i18n("js_dup_empty");
      screen.appendChild(empty);
      disablePagerControls();
      return;
    }

    updatePagerControls(groups);
    const pageGroups = currentPageGroups(groups);

    for (const g of pageGroups) {
      const sel = state.selection[g.group_id] || { keeperId: g.keeper.id, excludedIds: new Set(), skip: false };

      const card_group = document.createElement("div");
      card_group.className = "group" + (g.applied ? " applied" : "");

      const groupBar = document.createElement("div");
      groupBar.className = "group-bar";
      // Left side: group metadata, built from the same fields the cards
      // below already show (no server round trip needed for a description).
      const infoSpan = document.createElement("span");
      infoSpan.textContent = i18n("js_dup_group_info") + (g.discarded.length + 1) + i18n("js_files") + fmtSize(g.size) + i18n("js_each");
      groupBar.appendChild(infoSpan);
      if (g.applied) {
        const badge = document.createElement("span");
        badge.className = "applied-badge";
        badge.textContent = i18n("js_applied");
        groupBar.appendChild(badge);
      }
      // Rotates the whole group's thumbnail in one click, alongside the
      // per-card button. Every card in a dup group shares the same
      // g.thumb_id, so this is a convenience alias for clicking any one
      // card's own button.
      if (g.thumb_id) {
        const rotateGroupBtn = document.createElement("button");
        rotateGroupBtn.type = "button";
        rotateGroupBtn.className = "rotate-group-btn";
        rotateGroupBtn.title = i18n("js_rotate_group_title");
        rotateGroupBtn.textContent = i18n("js_rotate_group_btn");
        rotateGroupBtn.addEventListener("click", () => rotateGroupThumbs([g.thumb_id]));
        groupBar.appendChild(rotateGroupBtn);
      }
      // Right side: the skip toggle, after the rotate button so
      // .rotate-group-btn's margin-left: auto pushes both into one
      // right-aligned action cluster.
      const skipLabel = document.createElement("label");
      skipLabel.className = "skip-toggle";
      const skipCheckbox = document.createElement("input");
      skipCheckbox.type = "checkbox";
      skipCheckbox.className = "skip-group-checkbox";
      skipCheckbox.checked = sel.skip;
      skipCheckbox.addEventListener("change", (e) => {
        sel.skip = e.target.checked;
        render();
      });
      skipLabel.appendChild(skipCheckbox);
      skipLabel.appendChild(document.createTextNode(i18n("js_skip_group")));
      groupBar.appendChild(skipLabel);
      card_group.appendChild(groupBar);

      const row = document.createElement("div");
      row.className = "row";
      const entries = [
        { ...g.keeper, thumb_id: g.thumb_id, thumb_kind: g.thumb_kind },
        ...g.discarded.map((d) => ({ ...d, thumb_id: g.thumb_id, thumb_kind: g.thumb_kind })),
      ];
      // Computed once per group (keeper + all discarded together), not per
      // card, so every card in the group agrees on the same shared-prefix /
      // divergence point.
      const pathHighlights = computeGroupPathHighlights(entries);
      entries.forEach((entry, i) => {
        row.appendChild(makeCard(entry, entry.id === sel.keeperId, g.group_id, pathHighlights[i]));
      });
      card_group.appendChild(row);
      screen.appendChild(card_group);
    }
    return;
  }

  if (state.tab === "series") {
    renderSeriesScreen();
    return;
  }

  if (state.tab === "placement") {
    renderPlacementScreen();
    return;
  }

  if (state.tab === "compress") {
    renderCompressScreen();
    return;
  }

  if (state.tab === "restore") {
    renderRestoreScreen();
    return;
  }
}

// Shows/hides the apply bar and pagination controls, and swaps which of
// the three screen containers is visible -- factored out of render() so
// both render() and switchTab() can call it without duplicating the id
// list.
// --- dev-mode series tuning -----------------------------------------------
//
// The thresholds reach the engine ONLY as part of the next POST /api/rescan.
// Nothing here fetches, and nothing here re-groups anything client-side: a
// group list is what the server computed with the thresholds it had, and
// showing it under a different number than the one that produced it is the
// single thing this control must never do.

