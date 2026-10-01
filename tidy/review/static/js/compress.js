// seconds -> "N min" (rounded) once large enough that raw seconds would be
// hard to read at a glance, otherwise "N s" -- same rounding rule as
// fmtGapSeconds() above, kept separate because compress estimates are
// plain floats, not the series engine's max_gap_seconds.
function fmtSeconds(seconds) {
  if (!seconds || seconds <= 0) return "0 s";
  if (seconds >= 60) return Math.round(seconds / 60) + " min";
  return Math.round(seconds) + " s";
}

// Compact form of fmtSeconds() for the live per-file timer: "10s",
// "3min".
function fmtSecondsShort(seconds) {
  if (!seconds || seconds <= 0) return "0s";
  if (seconds >= 60) return Math.round(seconds / 60) + "min";
  return Math.round(seconds) + "s";
}

// Shared between the i18n("js_comp_col_q") select in makeCompressActionPanel() and the
// per-profile rows in makeCompressEstimatesTable() below -- one source of
// truth for both the profile order (best-quality-first, see
// docs/business/rules/compression.md) and the label text, so the select and the table can never drift
// apart on what a given profile is called. Third element is the encoder
// name (config.DEFAULT_COMPRESS_PROFILES' own "encoder" field, hardcoded
// client-side the same way the rest of this list already is) -- it is what
// makeCompressActionPanel()'s i18n("js_comp_col_q") select pairs this list with
// (docs/technical/components/compression-mechanism.md), so a profile never shows up paired with a device
// that cannot actually run its encoder.
const COMPRESS_PROFILE_LABELS = [
  ["x265_high", i18n("js_comp_q_high"), "libx265"],
  ["x265_medium", i18n("js_comp_q_med"), "libx265"],
  ["x265", i18n("js_comp_q_std"), "libx265"],
  ["av1", i18n("js_comp_q_min"), "libsvtav1"],
  ["hevc_nvenc", i18n("js_comp_gpu_nv_hevc"), "hevc_nvenc"],
  ["av1_nvenc", i18n("js_comp_gpu_nv_av1"), "av1_nvenc"],
  ["hevc_qsv", i18n("js_comp_gpu_qsv_hevc"), "hevc_qsv"],
  ["av1_qsv", i18n("js_comp_gpu_qsv_av1"), "av1_qsv"],
];

// One row per configured quality profile (config.DEFAULT_COMPRESS_PROFILES,
// via COMPRESS_PROFILE_LABELS above), not per hardware encoder -- every
// profile is estimated for every candidate by compress.estimate_all_
// profiles(), so there is always something to show here, unlike the
// hardware-encoder rows (docs/technical/components/compression-mechanism.md). A leading "Zrodlo"
// row carries the file's current size, which used to sit in the block's
// own header (makeCompressCandidateBlock()) instead. The lookup matches by
// `profile_name`, never by array position -- estimates[] is server-ordered
// by config.DEFAULT_COMPRESS_PROFILES, not by this list. A profile whose
// encoder is not available on this machine has no entry in estimates[] at
// all (build_compress_views()'s available_encoders filter) and gets no row
// here either -- an evaluated-but-uncalibrated one still gets a row, "?"
// instead of a fabricated number (docs/technical/components/compression-mechanism.md).
function makeCompressEstimatesTable(candidate) {
  const table = document.createElement("table");
  table.className = "estimates-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  [i18n("js_comp_col_q"), i18n("js_comp_col_sz"), i18n("js_comp_col_t"), i18n("js_comp_col_qdb")].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  const sourceRow = document.createElement("tr");
  const sourceLabelCell = document.createElement("td");
  sourceLabelCell.textContent = i18n("js_comp_src_orig");
  sourceRow.appendChild(sourceLabelCell);
  const sourceSizeCell = document.createElement("td");
  sourceSizeCell.textContent = fmtSize(candidate.size);
  sourceRow.appendChild(sourceSizeCell);
  const sourceTimeCell = document.createElement("td");
  sourceTimeCell.textContent = "--";
  sourceRow.appendChild(sourceTimeCell);
  const sourceQualityCell = document.createElement("td");
  sourceQualityCell.textContent = "--";
  sourceRow.appendChild(sourceQualityCell);
  tbody.appendChild(sourceRow);

  COMPRESS_PROFILE_LABELS.forEach(([profileName, label]) => {
    const est = (candidate.estimates || []).find((e) => e.profile_name === profileName);
    // No entry at all (as opposed to an entry with estimated_size === 0,
    // meaning "evaluated but not yet calibrated") means this profile's
    // encoder is not available on this machine at all -- build_compress_
    // views()'s available_encoders filter already dropped it server-side
    // (docs/technical/components/compression-mechanism.md: "the estimate table has rows for all
    // profiles available on this machine", not a row of "?" for hardware
    // nobody here has).
    if (!est) return;

    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.textContent = label;
    row.appendChild(nameCell);

    const sizeCell = document.createElement("td");
    const timeCell = document.createElement("td");
    if (est.estimated_size > 0) {
      // "≈" for a seed-based (not yet calibrated) estimate: nowhere does
      // this screen present a seed value as a measurement
      // (docs/technical/components/compression-mechanism.md).
      const prefix = est.calibrated ? "" : "≈ ";
      sizeCell.textContent = prefix + fmtSize(est.estimated_size);
      timeCell.textContent = prefix + fmtSeconds(est.estimated_seconds);
    } else {
      // No seed and no calibration for this profile: "?", never a
      // fabricated number (docs/technical/components/compression-mechanism.md).
      sizeCell.textContent = "?";
      timeCell.textContent = "?";
    }
    row.appendChild(sizeCell);
    row.appendChild(timeCell);

    // i18n("js_comp_col_qdb"): XPSNR-Y difference vs. libx265's own measurement on
    // THIS machine (compress.estimate_all_profiles()' xpsnr_diff), never
    // shown unless both sides are actually measured (docs/technical/components/compression-mechanism.md --
    // same "no unmeasured number" rule as the size/time columns above).
    // libx265 diffs against itself (0.0 when calibrated) and is labelled
    // i18n("js_comp_ref") instead of a number.
    const qualityCell = document.createElement("td");
    if (est.encoder === "libx265" && typeof est.xpsnr_diff === "number") {
      qualityCell.textContent = i18n("js_comp_ref");
    } else if (typeof est.xpsnr_diff === "number") {
      const sign = est.xpsnr_diff >= 0 ? "+" : "";
      qualityCell.textContent = sign + est.xpsnr_diff.toFixed(1) + " dB";
    } else {
      qualityCell.textContent = "?";
    }
    row.appendChild(qualityCell);

    tbody.appendChild(row);
  });
  table.appendChild(tbody);
  return table;
}

// Translates a CompressJob's `status` (queued -> encoding -> verifying ->
// encoded -> done | failed | cancelled, see CompressJob's own docstring in
// tidy/review/workers/compress_queue.py) into the text shown on a candidate's own badge below --
// replaces the old static i18n("js_comp_queued1") label, which stayed frozen for
// the whole run regardless of where in the pipeline that specific file
// actually was. Falls back to the old label when the job list has not
// caught up yet (candidate.applied flips true in the same response that
// creates the job, but GET /api/compress/jobs is a separate poll).
function compressJobStatusLabel(job) {
  if (!job) return i18n("js_comp_queued1");
  switch (job.status) {
    case "queued": return i18n("js_comp_queued2");
    case "encoding": return i18n("js_comp_enc");
    case "verifying": return i18n("js_comp_ver");
    // Encoded+verified, but not landed on disk yet -- the archive move
    // (hashing + moving a whole video file) still has to run for THIS job
    // alone, right away, never waiting on any other job in the queue (see
    // CompressJob's own docstring).
    case "encoded": return i18n("js_comp_save");
    case "done": return i18n("js_comp_done") + fmtSize(job.original_size) + " -> " + fmtSize(job.result_size) + ")";
    case "cancelled": return i18n("js_comp_canc");
    case "failed": return i18n("js_comp_err") + (job.error ? ": " + job.error : "");
    default: return job.status;
  }
}

// One candidate's block: the "compress this file" selection checkbox and
// reason on the left of the bar, "pomin" on the right (same
// left-metadata/right-skip layout as makeSeriesSkipControl()'s group bar),
// the read-only thumbnail card below (makeCard() reused exactly as the
// restore tab's renderBatchEntriesRow() does, with { readOnly: true } --
// never a click-to-reassign-keeper or click-to-toggle-membership one, but
// its dblclick still opens the file via compress_registry, see
// handle_open()) -- and the per-profile estimates table, which is where
// the file's current size now lives (it used to duplicate here too).
function makeCompressCandidateBlock(candidate) {
  const sel = state.compressSelection[candidate.group_id] || { selected: true, skip: false };
  const block = document.createElement("div");
  block.className = "group" + (candidate.skipped ? " skipped" : "");

  const bar = document.createElement("div");
  bar.className = "group-bar";

  const selectLabel = document.createElement("label");
  selectLabel.className = "compress-select-label";
  const selectBox = document.createElement("input");
  selectBox.type = "checkbox";
  selectBox.checked = !!sel.selected;
  selectBox.disabled = !!candidate.applied;
  selectBox.addEventListener("change", (e) => {
    sel.selected = e.target.checked;
    render();
  });
  selectLabel.appendChild(selectBox);
  selectLabel.appendChild(document.createTextNode(i18n("js_comp_compress")));
  bar.appendChild(selectLabel);

  // candidate.reason (the rule-8 bpp-threshold string, e.g. "bpp 0.452 >
  // threshold 0.100 for hevc") used to render here too -- purely internal
  // heuristic plumbing, meaningless to someone deciding whether to
  // compress a file, so it is not shown in the header. It still explains
  // *why a file was excluded* in the skipped list at the bottom, where a
  // reason is the whole point.

  if (candidate.applied) {
    const badge = document.createElement("span");
    const job = (tabData.compress.jobs || []).find((j) => j.group_id === candidate.group_id);
    // No job yet (GET /api/compress/jobs has not caught up) reads the same
    // as job.status === "queued" to the user -- both mean "not started",
    // so both get the same light-grey highlight rather than flashing
    // green first. "encoding" gets its own yellow, so an actively-running
    // file is visually distinct from both "waiting" and "already done".
    const isQueued = !job || job.status === "queued";
    const isEncoding = !!job && job.status === "encoding";
    const isFailed = !!job && job.status === "failed";
    badge.className = "applied-badge" +
      (isQueued ? " applied-badge-queued" : "") +
      (isEncoding ? " applied-badge-encoding" : "") +
      (isFailed ? " applied-badge-failed" : "");
    badge.textContent = compressJobStatusLabel(job);
    bar.appendChild(badge);

    // Estimated vs. elapsed, pinned to the bar's right edge via margin-
    // left: auto. Estimated (candidate.estimates[], matched by the
    // profile THIS job actually used) is compress.estimate_time()'s own
    // number, which only ever models the ENCODE pass -- it has no model
    // for verify_output()'s extra decode+XPSNR passes (two full decodes
    // plus a combined XPSNR filter pass, real wall-clock time neither
    // measured nor seeded anywhere -- an unmeasured number is never
    // fabricated, docs/technical/components/compression-mechanism.md). So the estimate is only a meaningful comparison against
    // elapsed while still actually encoding; once verifying, showing it
    // against elapsed-since-encode-started would silently understate how
    // much longer there is to go, so only elapsed (labelled "check")
    // is shown from that point on.
    if (job && (job.status === "encoding" || job.status === "verifying" || job.status === "encoded")) {
      const timeInfo = document.createElement("span");
      timeInfo.className = "compress-time-info";
      if (job.status === "encoding") {
        const est = (candidate.estimates || []).find((e) => e.profile_name === job.profile_name);
        const estText = est && est.estimated_seconds > 0 ? fmtSecondsShort(est.estimated_seconds) : "?";
        timeInfo.textContent = "~" + estText + "/" + fmtSecondsShort(job.elapsed_seconds);
      } else {
        timeInfo.textContent = "check " + fmtSecondsShort(job.elapsed_seconds);
      }
      bar.appendChild(timeInfo);
    }
  }

  const skipLabel = document.createElement("label");
  skipLabel.className = "skip-toggle skip-label";
  const skipBox = document.createElement("input");
  skipBox.type = "checkbox";
  skipBox.className = "skip-group-checkbox";
  skipBox.checked = !!sel.skip;
  skipBox.disabled = !!candidate.applied;
  skipBox.addEventListener("change", (e) => {
    toggleCompressSkip(candidate, e.target.checked);
  });
  skipLabel.appendChild(document.createTextNode(i18n("js_comp_skip_saved")));
  skipLabel.appendChild(skipBox);
  bar.appendChild(skipLabel);

  block.appendChild(bar);

  const row = document.createElement("div");
  row.className = "row";
  const pathHighlights = computeGroupPathHighlights([candidate]);
  row.appendChild(makeCard(candidate, false, candidate.group_id, pathHighlights[0], { readOnly: true }));
  block.appendChild(row);

  block.appendChild(makeCompressEstimatesTable(candidate));

  return block;
}

// The codec/device choice, the non-sticky "remove originals" checkbox, and
// the i18n("js_comp_btn_comp") button -- plus, while a queue is running, its progress
// bar/Cancel button, and once it finishes, a total before/after + time
// summary (GET /api/compress/jobs' own `jobs`, not the results panel
// below, which is `removable`).
// Returns three pieces for three toolbar slots: settings on the left,
// action buttons on the right (where the other tabs keep theirs), progress
// on its own line underneath.
function makeCompressActionPanel() {
  const settingsRow = document.createElement("div");
  settingsRow.className = "compress-settings-row";
  const actionsRow = document.createElement("div");
  actionsRow.className = "compress-actions-row";
  const progressRow = document.createElement("div");
  progressRow.className = "compress-progress-row";
  const panel = { settingsRow, actionsRow, progressRow };

  // i18n("js_comp_col_q"): one option per (profile, detected encoder row) pair whose
  // encoder names match -- the same labels as the estimates table rows
  // (COMPRESS_PROFILE_LABELS), so a profile is never offered on a device
  // that cannot run it. Rules: docs/technical/components/review-ui.md
  const availableEncoders = tabData.compress.encoders || [];
  const profileLabel = document.createElement("label");
  profileLabel.textContent = i18n("js_comp_lbl_q");
  const profileSelect = document.createElement("select");
  profileSelect.id = "compress-profile";
  COMPRESS_PROFILE_LABELS.forEach(([value, label, encoderName]) => {
    const devices = availableEncoders.filter((enc) => enc.name === encoderName);
    devices.forEach((enc) => {
      const opt = document.createElement("option");
      opt.value = value + "|" + enc.device_key;
      opt.dataset.profile = value;
      opt.dataset.deviceKey = enc.device_key;
      // Two cards of one vendor are two rows of one encoder: only then
      // does the device name have to disambiguate the label.
      opt.textContent = devices.length > 1 ? label + " - " + enc.display_name : label;
      profileSelect.appendChild(opt);
    });
  });
  // Greyed out and pinned to what THIS queue uses while it runs; the idle
  // pick is remembered because the panel is rebuilt on every render().
  profileSelect.disabled = tabData.compress.jobsRunning;
  const pickOption = (profile, deviceKey) => {
    const opt = Array.from(profileSelect.options).find(
      (o) => o.dataset.profile === profile && (!deviceKey || o.dataset.deviceKey === deviceKey),
    );
    if (opt) profileSelect.value = opt.value;
  };
  if (tabData.compress.jobsRunning && tabData.compress.submittedProfile) {
    pickOption(tabData.compress.submittedProfile, tabData.compress.submittedDeviceKey);
  } else if (tabData.compress.chosenProfile) {
    pickOption(tabData.compress.chosenProfile, tabData.compress.chosenDeviceKey);
  }
  profileSelect.addEventListener("change", () => {
    const opt = profileSelect.selectedOptions[0];
    tabData.compress.chosenProfile = opt ? opt.dataset.profile : null;
    tabData.compress.chosenDeviceKey = opt ? opt.dataset.deviceKey : null;
    render();
  });
  profileLabel.appendChild(profileSelect);

  // i18n("js_comp_measure"): shown only while the selected option's device has no
  // measurement yet; shares the single-slot queue with i18n("js_comp_btn_comp").
  const selectedOption = profileSelect.selectedOptions[0];
  const selectedEncoderInfoForCalibrate = selectedOption
    ? availableEncoders.find((e) => e.device_key === selectedOption.dataset.deviceKey)
    : null;
  const calibrationRunning = !!(tabData.compress.calibration &&
    tabData.compress.calibration.status === "running");
  if (selectedEncoderInfoForCalibrate && !selectedEncoderInfoForCalibrate.calibrated) {
    const calibrateBtn = document.createElement("button");
    calibrateBtn.type = "button";
    calibrateBtn.id = "compress-calibrate-btn";
    calibrateBtn.className = "apply-btn";
    calibrateBtn.textContent = i18n("js_comp_measure");
    calibrateBtn.disabled = tabData.compress.jobsRunning || calibrationRunning;
    calibrateBtn.addEventListener(
      "click", () => submitCalibrate(selectedEncoderInfoForCalibrate.device_key),
    );
    profileLabel.appendChild(calibrateBtn);
  }
  settingsRow.appendChild(profileLabel);

  if (calibrationRunning) {
    const calibMsg = document.createElement("span");
    calibMsg.className = "compress-calibration-status";
    calibMsg.textContent = i18n("js_comp_calib") +
      (tabData.compress.calibration.current_device_key || "...") + ")";
    progressRow.appendChild(calibMsg);
  }

  // Unseeded while idle (never reads any `state` field then): this
  // checkbox must default unchecked on every render while still selecting
  // candidates, because checking it moves files to !REMOVE without a
  // second click (docs/technical/components/review-ui.md). submitApplyCompress() reads its
  // .checked value straight off the DOM at click time. Same non-sticky
  // contract as the remove-originals checkbox below while idle -- a
  // relaxed quality bar chosen for one stuck file must never silently
  // carry into a later, unrelated apply. Separate from the i18n("js_comp_col_q")
  // select above: that picks the ENCODE profile, this only widens what
  // XPSNR score the VERIFICATION step will accept from whatever profile
  // was chosen, for the case reported in practice -- a real, otherwise-
  // fine capture landing ~1 dB under the default 38.0 dB threshold even
  // at the highest x265 tier.
  const relaxedLabel = document.createElement("label");
  relaxedLabel.className = "compress-allow-lower-quality-label";
  const relaxedBox = document.createElement("input");
  relaxedBox.type = "checkbox";
  relaxedBox.id = "compress-allow-lower-quality";
  // Same reasoning as profileSelect above: read only at click time, so
  // greyed out while a queue is already running rather than implying a
  // mid-queue change would take effect.
  relaxedBox.disabled = tabData.compress.jobsRunning;
  // Same "show what this running queue actually uses" reasoning as
  // profileSelect above -- only while running, never while idle.
  if (tabData.compress.jobsRunning) {
    relaxedBox.checked = tabData.compress.submittedAllowLowerQuality;
  }
  relaxedLabel.appendChild(relaxedBox);
  relaxedLabel.appendChild(document.createTextNode(
    i18n("js_comp_allow_low"),
  ));
  settingsRow.appendChild(relaxedLabel);

  const removeLabel = document.createElement("label");
  removeLabel.className = "compress-remove-originals-label";
  const removeBox = document.createElement("input");
  removeBox.type = "checkbox";
  removeBox.id = "compress-remove-originals";
  removeLabel.appendChild(removeBox);
  removeLabel.appendChild(document.createTextNode(i18n("js_comp_rm_orig")));
  settingsRow.appendChild(removeLabel);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "compress-apply-btn";
  btn.className = "apply-btn";
  btn.textContent = i18n("js_comp_btn_comp");
  btn.disabled = tabData.compress.jobsRunning;
  btn.addEventListener("click", () => submitApplyCompress());

  if (tabData.compress.jobsRunning) {
    const wrapper = document.createElement("div");
    wrapper.style.display = "flex";
    wrapper.style.alignItems = "center";
    wrapper.style.justifyContent = "center";
    wrapper.style.gap = "16px";

    wrapper.appendChild(makeProgressMessage(
      i18n("js_comp_prog") + compressQueueTimeText() + ")...", tabData.compress.jobsProgress, "480px",
    ));
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "apply-btn";
    cancelBtn.style.padding = "4px 10px";
    cancelBtn.style.fontSize = "0.85em";
    cancelBtn.style.fontWeight = "normal";
    cancelBtn.textContent = i18n("js_comp_btn_canc");
    cancelBtn.addEventListener("click", () => cancelCompressQueue());
    wrapper.appendChild(cancelBtn);
    progressRow.appendChild(wrapper);
  } else {
    // Once the queue has finished (never while it is still running --
    // that is what the progress bar above is for): total before/after
    // size and total time, next to i18n("js_comp_btn_comp"), covering the whole batch
    // in one line instead of making the user add up each candidate
    // card's own badge by hand. Each candidate card's badge (compressJob
    // StatusLabel()) still carries the per-file "done (size -> size)"/
    // "blad: ..." detail -- this is only the aggregate.
    //
    // Scoped to THIS directory's own candidates' group_ids -- tabData.
    // compress.jobs is a single, global, cross-directory queue (see this
    // module's own comment on it: "the queue keeps running in the
    // background regardless of which source directory's candidates are
    // on screen"), so an unfiltered sum kept showing a directory switched
    // AWAY from's totals next to a completely different directory's own
    // i18n("js_comp_btn_comp") button. Ids are unique across computations (this
    // module's own next_compress_id_prefix()), so this can never
    // accidentally match a different directory's job by coincidence.
    const currentGroupIds = new Set((tabData.compress.candidates || []).map((c) => c.group_id));
    const relevantJobs = (tabData.compress.jobs || []).filter((j) => currentGroupIds.has(j.group_id));
    const doneJobs = relevantJobs.filter((j) => j.status === "done");
    const summary = document.createElement("span");
    summary.className = "compress-total-summary";

    if (doneJobs.length > 0) {
      const totalBefore = doneJobs.reduce((sum, j) => sum + j.original_size, 0);
      const totalAfter = doneJobs.reduce((sum, j) => sum + j.result_size, 0);
      // Sum of each job's OWN elapsed time, not last.finished_at -
      // first.started_at: jobs run strictly one at a time (never in
      // parallel), so the two are equivalent, but summing per-job also
      // correctly counts a failed/cancelled job's partial encoding time.
      const totalElapsed = relevantJobs.reduce((sum, j) => sum + (j.elapsed_seconds || 0), 0);
      const pct = totalBefore > 0 ? Math.round((1 - totalAfter / totalBefore) * 100) : 0;
      summary.textContent =
        i18n("js_comp_tot_comp") + fmtSize(totalBefore) + " -> " + fmtSize(totalAfter) +
        " (" + (pct >= 0 ? "-" : "+") + Math.abs(pct) + "%). Czas kompresji: " +
        fmtSeconds(totalElapsed);
      actionsRow.appendChild(summary);
    } else {
      // Nothing has actually run yet for this directory -- an ESTIMATED
      // preview instead, over exactly the candidates submitApplyCompress()
      // would submit right now (selected, not skipped, not already
      // applied) at the currently chosen profile, so switching directory
      // or profile recalculates this line instead of leaving a stale
      // number on screen.
      // The option value is "profile|device_key"; estimates are keyed by profile.
      const chosenOpt = profileSelect.selectedOptions[0];
      const chosenProfileName = chosenOpt ? chosenOpt.dataset.profile : null;
      let estBefore = 0, estAfter = 0, estSeconds = 0, haveEstimate = false;
      for (const c of visibleCompressCandidates()) {
        if (c.applied) continue;
        const csel = state.compressSelection[c.group_id];
        if (!csel || csel.skip || !csel.selected) continue;
        const est = (c.estimates || []).find((e) => e.profile_name === chosenProfileName);
        if (!est || est.estimated_size <= 0) continue;
        haveEstimate = true;
        estBefore += c.size;
        estAfter += est.estimated_size;
        estSeconds += est.estimated_seconds || 0;
      }
      if (haveEstimate) {
        const pct = estBefore > 0 ? Math.round((1 - estAfter / estBefore) * 100) : 0;
        summary.textContent =
          i18n("js_comp_est_comp") + fmtSize(estBefore) + " -> " + fmtSize(estAfter) +
          " (" + (pct >= 0 ? "-" : "+") + Math.abs(pct) + i18n("js_est_time") +
          fmtSeconds(estSeconds);
        actionsRow.appendChild(summary);
      }
    }
  }
  // Summary then button, the same order as #summary + apply buttons on the other tabs.
  actionsRow.appendChild(btn);

  return panel;
}

// "~estimated/elapsed" for the running queue: the estimate sums each job's
// encode estimate for its own profile ("?" when no job on screen has one),
// elapsed sums every job's own elapsed_seconds.
function compressQueueTimeText() {
  const byGroup = new Map((tabData.compress.candidates || []).map((c) => [c.group_id, c]));
  let estSeconds = 0, haveEstimate = false, elapsed = 0;
  for (const job of tabData.compress.jobs || []) {
    elapsed += job.elapsed_seconds || 0;
    const candidate = byGroup.get(job.group_id);
    const est = candidate && (candidate.estimates || []).find((e) => e.profile_name === job.profile_name);
    if (est && est.estimated_seconds > 0) {
      estSeconds += est.estimated_seconds;
      haveEstimate = true;
    }
  }
  return "~" + (haveEstimate ? fmtSecondsShort(estSeconds) : "?") + "/" + fmtSecondsShort(elapsed);
}

// Results panel: one checkbox per CONFIRMED, VERIFIED result --
// `removable` (from _compress_removable_candidates(), server-side) only
// ever contains rows read back from a done+verified !MOVES manifest entry
// whose original and compressed file both still exist on disk, so every
// row rendered here gets a checkbox; there is no unverified/failed row in
// this list to withhold one from (those live in tabData.compress.jobs
// above, rendered with no checkbox at all -- the structural "no checkbox
// for an unverified row" contract expressed in the UI, mirroring
// _validate_compress_remove_selections()'s server-side one).
function makeCompressResultsPanel() {
  const panel = document.createElement("div");
  panel.className = "compress-results-panel";
  const removable = tabData.compress.removable || [];
  if (removable.length === 0) {
    return panel;
  }

  const heading = document.createElement("div");
  heading.className = "group-bar folder-bar";
  heading.textContent = i18n("js_comp_res_head");
  panel.appendChild(heading);

  // "Select all": checked exactly when every row is; toggling it
  // sets every row, toggling a row re-syncs it without a render().
  const allLabel = document.createElement("label");
  allLabel.className = "compress-results-select-all";
  const allBox = document.createElement("input");
  allBox.type = "checkbox";
  allBox.id = "compress-results-select-all";
  const syncAllBox = () => {
    allBox.checked = removable.every((r) => compressRemoveSelection[r.file_id]);
  };
  syncAllBox();
  allBox.addEventListener("change", (e) => {
    removable.forEach((r) => { compressRemoveSelection[r.file_id] = e.target.checked; });
    render();
  });
  allLabel.appendChild(allBox);
  allLabel.appendChild(document.createTextNode(i18n("js_comp_sel_all")));
  panel.appendChild(allLabel);

  const list = document.createElement("ul");
  list.className = "compress-results-list";
  removable.forEach((row) => {
    const li = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!compressRemoveSelection[row.file_id];
    box.addEventListener("change", (e) => {
      compressRemoveSelection[row.file_id] = e.target.checked;
      syncAllBox();
    });
    label.appendChild(box);
    const pct = row.original_size > 0
      ? Math.round((1 - row.compressed_size / row.original_size) * 100)
      : 0;
    label.appendChild(document.createTextNode(
      " " + row.path + "  " + fmtSize(row.original_size) + " -> " + fmtSize(row.compressed_size) +
      " (" + (pct >= 0 ? "-" : "+") + Math.abs(pct) + "%)",
    ));
    li.appendChild(label);
    list.appendChild(li);
  });
  panel.appendChild(list);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "apply-btn";
  btn.textContent = i18n("js_comp_mv_sel");
  btn.className = "danger-remove-btn";
  btn.addEventListener("click", () => {
    const selected = new Set(
      removable.filter((r) => compressRemoveSelection[r.file_id]).map((r) => r.file_id),
    );
    if (selected.size === 0) {
      document.getElementById("result").textContent = i18n("js_comp_no_sel_mv");
      return;
    }
    if (!confirm(
      i18n("js_conf_move") + selected.size + i18n("js_conf_move_tail")
    )) {
      return;
    }
    submitApplyCompressRemove(selected);
  });
  panel.appendChild(btn);

  return panel;
}

function renderCompressScreen() {
  const screen = document.getElementById("compress-screen");
  screen.innerHTML = "";
  const actionSlot = document.getElementById("compress-action-slot");
  const applySlot = document.getElementById("compress-apply-slot");
  const progressSlot = document.getElementById("compress-progress-slot");
  [actionSlot, applySlot, progressSlot].forEach((slot) => { slot.innerHTML = ""; });
  const entry = tabData.compress;

  if (entry.unavailableReason) {
    screen.appendChild(makeStatusMessage(i18n("js_comp_unavail") + entry.unavailableReason));
    return;
  }
  if (entry.status === "error") {
    screen.appendChild(makeStatusMessage(i18n("js_comp_err_calc") + (entry.error || i18n("js_unknown_err"))));
    return;
  }
  if (entry.status !== "ready") {
    screen.appendChild(makeProgressMessage(i18n("js_comp_calc"), entry.progress));
    return;
  }

  if ((entry.skipped || []).length > 0) {
    const notesBox = document.createElement("div");
    notesBox.className = "notes-section";
    appendNotesSection(notesBox, "compressIneligible", i18n("js_comp_inel"),
                       entry.skipped, (s) => s.path + " (" + s.reason + ")");
    screen.appendChild(notesBox);
  }

  const visible = visibleCompressCandidates();
  if (visible.length === 0) {
    const totalCount = (entry.candidates || []).length;
    if (totalCount > 0) {
      screen.appendChild(makeStatusMessage(
        i18n("js_comp_all_cand") + totalCount + i18n("js_comp_all_skipped").replace("{dir}", state.compressSource),
      ));
    } else {
      screen.appendChild(makeStatusMessage(i18n("js_comp_no_cand_dir") + state.compressSource + "."));
    }
  } else {
    const list = document.createElement("div");
    list.className = "compress-candidate-list";
    visible.forEach((c) => list.appendChild(makeCompressCandidateBlock(c)));
    screen.appendChild(list);
  }

  const panel = makeCompressActionPanel();
  actionSlot.appendChild(panel.settingsRow);
  applySlot.appendChild(panel.actionsRow);
  progressSlot.appendChild(panel.progressRow);
  screen.appendChild(makeCompressResultsPanel());
}

// The submit payload for POST /api/apply-compress: one {group_id,
// file_ids} entry per selected, non-skipped, not-yet-applied candidate --
// same "already applied/skipped/emptied selections are silently left out"
// rule as buildPlacementPayload() above (an empty file_ids would be a 400
// from the server for a group the user simply never selected).
function buildCompressApplyPayload(candidates) {
  const selections = [];
  for (const c of candidates) {
    if (c.applied) continue;
    const sel = state.compressSelection[c.group_id];
    if (!sel || sel.skip || !sel.selected) continue;
    selections.push({ group_id: c.group_id, file_ids: [c.id] });
  }
  return selections;
}

// The submit payload for POST /api/apply-compress-remove: groups the
// selected removable file_ids by their own group_id (each currently a
// "group of one", same as compress candidates, but built generically
// rather than assuming that -- the server's own selection model does not
// assume it either).
function buildCompressRemovePayload(selectedFileIds) {
  const byGroup = new Map();
  for (const row of tabData.compress.removable) {
    if (!selectedFileIds.has(row.file_id)) continue;
    if (!byGroup.has(row.group_id)) byGroup.set(row.group_id, []);
    byGroup.get(row.group_id).push(row.file_id);
  }
  return Array.from(byGroup, ([group_id, file_ids]) => ({ group_id, file_ids }));
}

// GET /api/compress/encoders, fetched once per page load (like
// ensureSourceDirs()) -- the machine's own hardware does not change
// mid-session, so this never needs a second fetch.
let compressEncodersLoaded = false;
async function ensureCompressEncoders() {
  if (compressEncodersLoaded) return;
  compressEncodersLoaded = true;
  const resp = await fetch("/api/compress/encoders");
  const payload = await resp.json();
  tabData.compress.encodersAvailable = !!payload.available;
  tabData.compress.encoders = payload.encoders || [];
  tabData.compress.machineId = payload.machine_id;
  tabData.compress.ffmpegVersion = payload.ffmpeg_version;
  tabData.compress.unavailableReason = payload.unavailable_reason || null;
  // A blocked/missing ffmpeg may be fixed mid-session: re-ask on the next visit.
  if (!payload.available) compressEncodersLoaded = false;
  if (state.tab === "compress") render();
}

// One GET /api/compress/jobs, applied to tabData.compress -- shared by the
// polling loop below and by every one-off refresh (submitApplyCompress(),
// submitApplyCompressRemove()) that wants the freshest queue/results state
// right away rather than waiting up to 1s for the next scheduled poll.
function fetchCompressJobsOnce() {
  return fetch("/api/compress/jobs")
    .then((resp) => resp.json())
    .then((payload) => {
      tabData.compress.jobsRunning = payload.running;
      tabData.compress.jobs = payload.jobs || [];
      tabData.compress.jobsProgress = payload.progress || { done: 0, total: 0 };
      tabData.compress.removable = payload.removable || [];
      tabData.compress.calibration = payload.calibration || null;
      if (state.tab === "compress") render();
      return payload;
    });
}

// Self-rescheduling poll, same shape as pollTabData()'s own setTimeout
// loop, but keyed off `running` rather than a "computing"/"ready" status
// string -- GET /api/compress/jobs has no lazy-computation shape, it is a
// live queue. Stops rescheduling the moment the server reports nothing is
// running, and compressJobsPolling drops back to false so a later
// ensureCompressJobs() (a fresh tab switch, or the next i18n("js_comp_btn_comp")) can
// start the loop again.
let compressJobsPolling = false;
function pollCompressJobsLoop() {
  fetchCompressJobsOnce().then((payload) => {
    if (payload.running) {
      setTimeout(pollCompressJobsLoop, 1000);
    } else {
      compressJobsPolling = false;
    }
  });
}

// Starts (or resumes) the jobs poll loop -- a no-op if one is already
// running, so switching away from and back to the compress tab never
// stacks a second overlapping loop.
function ensureCompressJobs() {
  if (compressJobsPolling) return;
  compressJobsPolling = true;
  pollCompressJobsLoop();
}

async function cancelCompressQueue() {
  await fetch("/api/compress/cancel", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  ensureCompressJobs();
  await fetchCompressJobsOnce();
}

// Re-fetches GET /api/compress after an apply, same reason
// refreshPlacementData()/refreshSeriesData() exist: the background
// computation is already "ready" and never re-runs, so this only picks up
// the fresh per-candidate "applied" flag POST /api/apply-compress just
// stamped (state.applied_compress_group_ids, server-side).
async function refreshCompressData() {
  const resp = await fetch("/api/compress?source=" + encodeURIComponent(state.compressSource));
  const payload = await resp.json();
  if (payload.source && payload.source !== state.compressSource) return;
  tabData.compress.status = payload.status;
  tabData.compress.candidates = payload.candidates || [];
  tabData.compress.skipped = payload.skipped || [];
  tabData.compress.error = payload.error;
  if (payload.unavailable_reason) {
    tabData.compress.unavailableReason = payload.unavailable_reason;
  }
  ensureCompressSelection(tabData.compress.candidates);
  render();
}

// The compress screen's own submit path -- POST /api/apply-compress, which
// only ever QUEUES the encode (the actual work runs on a background
// thread server-side); remove_originals=true is
// entirely a server-side pipeline from here on, nothing more for the UI to
// orchestrate beyond showing the queue's progress.
// i18n("js_comp_measure") button's own submit path -- POST /api/compress/calibrate
// (docs/technical/components/compression-mechanism.md). Measures ONE device_key against the first visible
// candidate; ensureCompressJobs()/the 1s poll loop then picks up
// tabData.compress.calibration's progress the same way it already does
// for tabData.compress.jobs.
async function submitCalibrate(deviceKey) {
  const candidates = visibleCompressCandidates();
  if (candidates.length === 0) {
    document.getElementById("result").textContent = i18n("js_comp_no_cand_meas");
    return;
  }
  const resp = await fetch("/api/compress/calibrate", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ file_id: candidates[0].id, device_keys: [deviceKey] }),
  });
  const result = await resp.json();
  appendResult(result);
  ensureCompressJobs();
}

async function submitApplyCompress() {
  const selections = buildCompressApplyPayload(visibleCompressCandidates());
  if (selections.length === 0) {
    document.getElementById("result").textContent = i18n("js_comp_no_sel_cmp");
    return;
  }
  const profileSelect = document.getElementById("compress-profile");
  const profileOption = profileSelect ? profileSelect.selectedOptions[0] : null;
  const profile = profileOption ? profileOption.dataset.profile : "x265";
  const selectedEncoderInfo = profileOption
    ? (tabData.compress.encoders || []).find((e) => e.device_key === profileOption.dataset.deviceKey)
    : null;
  // Omitted entirely for a CPU row -- POST /api/apply-compress rejects any
  // device_key at all alongside a CPU profile (docs/technical/components/compression-mechanism.md), so a CPU
  // selection must never send one, not even its own bare encoder name.
  const deviceKey = selectedEncoderInfo && selectedEncoderInfo.is_hardware
    ? selectedEncoderInfo.device_key
    : null;
  const relaxedBox = document.getElementById("compress-allow-lower-quality");
  const allowLowerQuality = !!(relaxedBox && relaxedBox.checked);
  // Captured so the panel can keep showing what THIS queue actually uses
  // once it starts and the controls go non-interactive -- see the fields'
  // own comment in tabData.compress above.
  tabData.compress.submittedProfile = profile;
  tabData.compress.submittedDeviceKey = deviceKey;
  tabData.compress.submittedAllowLowerQuality = allowLowerQuality;
  const removeBox = document.getElementById("compress-remove-originals");
  const removeOriginals = !!(removeBox && removeBox.checked);
  if (removeOriginals && !confirm(
    i18n("js_comp_conf_remove")
  )) {
    return;
  }
  const body = {
    selections: selections, profile: profile, remove_originals: removeOriginals,
    allow_lower_quality: allowLowerQuality,
  };
  if (deviceKey) body.device_key = deviceKey;
  const resp = await fetch("/api/apply-compress", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await resp.json();
  appendResult(result);
  await refreshCompressData();
  ensureCompressJobs();
}

// Results panel's own submit path -- POST /api/apply-compress-remove. Clears the
// just-submitted rows out of compressRemoveSelection afterwards: they will
// disappear from the next GET /api/compress/jobs response anyway (their
// original no longer exists at row.src_abs once moved), but leaving a
// now-meaningless "checked" entry behind for a file_id that could in theory
// be reused is needless risk for no benefit.
async function submitApplyCompressRemove(selectedFileIds) {
  const selections = buildCompressRemovePayload(selectedFileIds);
  const resp = await fetch("/api/apply-compress-remove", {
    method: "POST",
    headers: { "X-Dedup-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ selections: selections }),
  });
  const result = await resp.json();
  appendResult(result);
  for (const fileId of selectedFileIds) delete compressRemoveSelection[fileId];
  await fetchCompressJobsOnce();
}

