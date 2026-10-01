// What the server says is in force right now, plus the engine defaults --
// never a value this file made up.
function serverSeriesTuning() {
  const data = state.data || {};
  return {
    hamming: data.series_hamming_threshold,
    windowSeconds: data.series_window_seconds,
    hammingDefault: data.series_hamming_threshold_default,
    windowDefault: data.series_window_seconds_default,
  };
}

// What the next Rescan would send: the user's pick where they made one,
// otherwise whatever is in force.
function chosenSeriesTuning() {
  const server = serverSeriesTuning();
  return {
    hamming: state.seriesTuning.hamming !== null
      ? state.seriesTuning.hamming : server.hamming,
    windowSeconds: state.seriesTuning.windowSeconds !== null
      ? state.seriesTuning.windowSeconds : server.windowSeconds,
  };
}

function seriesTuningPending() {
  const server = serverSeriesTuning();
  if (server.hamming === undefined || server.windowSeconds === undefined) return false;
  const chosen = chosenSeriesTuning();
  return chosen.hamming !== server.hamming
    || chosen.windowSeconds !== server.windowSeconds;
}

// Rebuilt only when the (current, default) pair changes, not on every
// render: this runs from updateScreenVisibility(), which a series poll tick
// calls every second, and replacing the <option>s under an open dropdown
// would close it mid-choice.
function buildTuningSelect(el, choices, current, defaultValue) {
  const key = current + "|" + defaultValue;
  if (el.dataset.builtFor !== key) {
    el.innerHTML = "";
    const values = choices.map((c) => c.value);
    const all = values.indexOf(current) === -1
      ? choices.concat([{ value: current, label: String(current) }])
          .sort((a, b) => a.value - b.value)
      : choices;
    for (const choice of all) {
      const option = document.createElement("option");
      option.value = String(choice.value);
      option.textContent = choice.value === defaultValue
        ? choice.label + i18n("js_default_suffix")
        : choice.label;
      el.appendChild(option);
    }
    el.dataset.builtFor = key;
  }
  el.value = String(current);
}

function updateSeriesTuningControls() {
  const box = document.getElementById("series-tuning");
  if (!box) return;
  // The series tab only: the thresholds change nothing on the other tabs,
  // so offering them there would be a dead control.
  box.style.display = state.tab === "series" ? "" : "none";
  const server = serverSeriesTuning();
  // loadState() has not landed yet -- leave the empty selects alone rather
  // than inventing a value to put in them.
  if (server.hamming === undefined || server.windowSeconds === undefined) return;
  const chosen = chosenSeriesTuning();
  buildTuningSelect(
    document.getElementById("series-hamming"),
    SERIES_HAMMING_CHOICES, chosen.hamming, server.hammingDefault,
  );
  buildTuningSelect(
    document.getElementById("series-window"),
    SERIES_WINDOW_CHOICES, chosen.windowSeconds, server.windowDefault,
  );
  const pending = seriesTuningPending();
  box.classList.toggle("pending", pending);
  // The general tuning warning lives in the [?] tooltip (js_tooltip_win).
  document.getElementById("series-tuning-hint").textContent = pending
    ? i18n("js_rescan_needed")
    : "";
}

