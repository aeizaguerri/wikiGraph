// Launch form (ticket 11): a boxless full-bleed overlay over the blurred live
// graph. Form state lives outside the DOM (this closure), so re-opens and
// later layout modes (ticket 12) never disturb a running crawl — progress
// re-renders from state in whatever surface is mounted.
export const CAP_MIN = 1;
export const CAP_MAX = 5000;
export const DEFAULT_CAP = 500;

export function clampCap(raw) {
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed)) return DEFAULT_CAP;
  return Math.min(CAP_MAX, Math.max(CAP_MIN, parsed));
}

export function createLauncher(form, onLaunch) {
  const state = {
    open: false,
    phase: "idle", // idle | running
    seed: "",
    depth: 2,
    language: "es",
    cap: DEFAULT_CAP,
    progress: null, // { crawled, discovered, depth }
    error: null,
  };

  const overlay = document.getElementById("launch-overlay");
  const seed = form.elements.namedItem("seed");
  const cap = form.elements.namedItem("nodeCap");
  const depthRow = document.getElementById("launch-depth");
  const editionRow = document.getElementById("launch-edition");
  const submit = document.getElementById("launch-submit");
  const progressLine = document.getElementById("launch-progress");
  const errorLine = document.getElementById("launch-error");

  seed.addEventListener("input", () => {
    state.seed = seed.value;
  });
  cap.addEventListener("focus", () => {
    // Focus selects the whole value, so typing replaces it — no spinners.
    cap.select();
  });
  cap.addEventListener("blur", () => {
    state.cap = clampCap(cap.value);
    cap.value = String(state.cap);
  });

  for (const button of depthRow.querySelectorAll(".chip")) {
    button.addEventListener("click", () => {
      state.depth = Number(button.dataset.depth);
      renderChips();
    });
  }
  for (const button of editionRow.querySelectorAll(".chip")) {
    button.addEventListener("click", () => {
      state.language = button.dataset.language;
      renderChips();
    });
  }

  function renderChips() {
    for (const button of depthRow.querySelectorAll(".chip")) {
      button.setAttribute(
        "aria-pressed",
        String(Number(button.dataset.depth) === state.depth),
      );
    }
    for (const button of editionRow.querySelectorAll(".chip")) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.language === state.language),
      );
    }
  }

  function running() {
    return state.phase === "running";
  }

  function render() {
    overlay.hidden = !state.open;
    seed.value = state.seed;
    cap.value = String(state.cap);
    renderChips();
    const locked = running();
    for (const field of [seed, cap, submit]) field.disabled = locked;
    if (state.progress) {
      progressLine.hidden = false;
      progressLine.textContent =
        `Crawled ${state.progress.crawled} · Discovered ${state.progress.discovered}`;
    } else {
      progressLine.hidden = true;
      progressLine.textContent = "";
    }
    if (state.error) {
      errorLine.hidden = false;
      errorLine.textContent = state.error;
    } else {
      errorLine.hidden = true;
      errorLine.textContent = "";
    }
  }

  // A click on the overlay root — the backdrop — closes it; the form panel
  // and its controls float above it, and no ✕ button exists anywhere.
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  function open() {
    if (state.open) return;
    state.open = true;
    render();
    seed.focus();
  }

  function close() {
    if (!state.open) return;
    state.open = false;
    render();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (running()) return;
    state.seed = seed.value;
    state.cap = clampCap(cap.value);
    cap.value = String(state.cap);
    // The lock renders immediately: a double submit can't start two runs.
    // render() releases everything again on any error (phase back to idle).
    state.phase = "running";
    state.error = null;
    state.progress = null;
    submit.textContent = "Launching…";
    render();
    try {
      const outcome = await onLaunch({
        seed: state.seed,
        depth: state.depth,
        language: state.language,
        nodeCap: state.cap,
      });
      if (outcome.error) {
        state.error = outcome.error;
        state.phase = "idle";
      } else {
        // Run accepted; waiting for its first SSE progress event. The 0/0
        // counters are singularly honest — nothing crawled yet.
        state.progress = { crawled: 0, discovered: 0, depth: state.depth };
      }
    } catch (error) {
      state.error = `The request failed (${error.message}).`;
      state.phase = "idle";
    } finally {
      submit.textContent = "Launch crawl";
      render();
    }
  });

  function updateProgress(data) {
    state.progress = data;
    render();
  }

  // Failures surface honestly in the form and release its fields for retry.
  function fail(message) {
    state.phase = "idle";
    state.progress = null;
    state.error = message;
    render();
  }

  // Completion auto-closes the overlay and resets the launcher for the next one.
  function complete() {
    state.phase = "idle";
    state.progress = null;
    state.error = null;
    state.seed = "";
    close();
  }

  render();
  return { open, close, updateProgress, fail, complete };
}
