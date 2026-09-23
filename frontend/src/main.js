import {
  createExperience,
  parseLens,
} from "./experience.js";
import { createLauncher } from "./launcher.js";
import "./style.css";

const container = document.getElementById("graph");
const statusBox = document.getElementById("status");
const truncationBox = document.getElementById("truncation");
const noticeBox = document.getElementById("notice");
const infoCard = document.getElementById("info-card");
const cardTitle = document.getElementById("card-title");
const cardLevel = document.getElementById("card-level");
const cardDegree = document.getElementById("card-degree");
const cardCommunity = document.getElementById("card-community");
const cardLink = document.getElementById("card-link");
const lensCommunity = document.getElementById("lens-community");
const lensLevel = document.getElementById("lens-level");
const legendTitle = document.getElementById("legend-title");
const legendEntries = document.getElementById("legend-entries");
const launchForm = document.getElementById("launch-form");
const waitingBox = document.getElementById("run-waiting");
const runState = document.getElementById("run-state");
const runProgress = document.getElementById("run-progress");
const runRecent = document.getElementById("run-recent");
const retryButton = document.getElementById("run-retry");
const landingView = document.getElementById("landing-view");
const runView = document.getElementById("run-view");
const runTitle = document.getElementById("run-title");
const runCrawled = document.getElementById("run-crawled");
const runDiscovered = document.getElementById("run-discovered");
const runPercent = document.getElementById("run-percent");
const runPhase = document.getElementById("run-phase");
const runOrbit = document.getElementById("run-orbit");
const runBadge = document.getElementById("run-badge");
const runLiveLabel = document.getElementById("run-live-label");
const runUrl = document.getElementById("run-url");

const PHYSICS_LABELS = {
  settling: "settling…",
  static: "",
  "re-heating": "re-heating…",
};

function showNotice(message) {
  noticeBox.textContent = message;
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (body.error && body.error.message) return body.error.message;
  } catch {
    // fall through to the generic message
  }
  return `The request failed (HTTP ${response.status}).`;
}

// Color lens (ticket 10): the choice is URL-expressed (?color=community|level)
// so it is shareable and survives reload; unknown values fall back to the
// community default.
const urlParams = new URLSearchParams(window.location.search);
const bootRunId = urlParams.get("run");
const initialLens = parseLens(urlParams.get("color"));

function syncUrl({ lens = null, run = null }) {
  const url = new URL(window.location.href);
  if (lens) url.searchParams.set("color", lens);
  if (run) url.searchParams.set("run", run);
  window.history.replaceState(null, "", url);
}

function showView(view) {
  const running = view === "run";
  landingView.hidden = running;
  runView.hidden = !running;
  const stage = document.getElementById("stage");
  stage.classList.toggle("has-run", running);
  if (!running) stage.classList.remove("has-graph");
  document.body.classList.toggle("has-run", running);
}

const LIFECYCLE_LABELS = {
  running: "Building your Graph — safe to leave this tab.",
  overload_waiting: "Wikimedia is busy; this run is waiting and will keep its identity.",
  recoverable: "The run was interrupted. Resume it from the last checkpoint.",
  failed: "This run failed and did not produce a completed Graph.",
  expired: "This run has expired; its Graph is no longer available.",
  completed: "Requested Graph ready.",
};

function renderRunState(data) {
  showView("run");
  document.getElementById("stage").classList.toggle("has-graph", data.status === "completed");
  waitingBox.hidden = data.status === "completed";
  runState.textContent = LIFECYCLE_LABELS[data.status] ?? `Run status: ${data.status}`;
  runProgress.textContent = `Crawled ${data.crawled} · Discovered ${data.discovered} · Depth ${data.currentDepth}`;
  runRecent.textContent = data.recent?.length ? `Recent: ${data.recent.join(" · ")}` : "Waiting for the first committed batch…";
  runCrawled.textContent = String(data.crawled ?? 0);
  runDiscovered.textContent = String(data.discovered ?? 0);
  const progress = data.status === "completed" ? 100 : Math.min(99, Math.max(0, Number(data.crawled ?? 0)));
  runPercent.textContent = `${progress}%`;
  runPhase.textContent = data.status === "failed" || data.status === "expired" ? "stopped" : data.status === "completed" ? "ready" : "building";
  runOrbit.className = `c-orbit orbit-${data.status}`;
  runOrbit.classList.toggle("orbit-complete", data.status === "completed");
  runOrbit.classList.toggle("orbit-failed", data.status === "failed" || data.status === "expired");
  runBadge.textContent = data.status === "completed" ? "Ready" : data.status === "failed" || data.status === "expired" ? "Needs attention" : "Live";
  retryButton.hidden = !["recoverable", "overload_waiting"].includes(data.status);
  retryButton.dataset.runId = data.runId;
  retryButton.disabled = data.status === "overload_waiting";
  runLiveLabel.hidden = data.status === "completed" || ["failed", "expired", "recoverable", "overload_waiting"].includes(data.status);
  statusBox.textContent = data.status === "completed" ? "" : "live run";
}

async function refreshRun(runId) {
  const response = await fetch(`/api/runs/${runId}`);
  if (!response.ok) {
    const message = await errorMessage(response);
    if (response.status === 410) {
      renderRunState({ runId, status: "expired", crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
    }
    throw new Error(message);
  }
  const state = await response.json();
  renderRunState(state);
  if (state.status === "running" || state.status === "overload_waiting" || state.status === "recoverable") {
    const preview = await fetch(`/api/runs/${runId}/preview`);
    if (preview.ok) mountExperience(await preview.json());
  }
  return state;
}

// The in-view chrome is rebuilt from the live experience after every mount,
// so a swapped Graph (ticket 11) re-renders legend and lens for free.
function renderLegend(experience) {
  const legend = experience.legendData();
  legendTitle.textContent = legend.title;
  legendEntries.replaceChildren(
    ...legend.entries.map((entry) => {
      const item = document.createElement("li");
      item.className = "legend-entry";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.backgroundColor = entry.color;
      const label = document.createElement("span");
      label.className = "legend-label";
      label.textContent = entry.label;
      item.append(swatch, label);
      return item;
    }),
  );
}

function renderLensButtons(lens) {
  lensCommunity.setAttribute("aria-pressed", String(lens === "community"));
  lensLevel.setAttribute("aria-pressed", String(lens === "level"));
}

let currentExperience = null;
let activeLens = initialLens;

// Ticket 11: the launch surface mounts every completed Graph the same way —
// booting and swapping alike destroy the previous experience first.
function mountExperience(graphPayload) {
  showView("run");
  if (currentExperience) {
    currentExperience.destroy();
    // The old selection card refers to the swapped-out Graph.
    infoCard.hidden = true;
  }
  currentExperience = createExperience(graphPayload, container, {
    lens: activeLens,
    onState(state) {
      statusBox.textContent = PHYSICS_LABELS[state] ?? "";
    },
    onSelection(info) {
      if (!info) {
        infoCard.hidden = true;
        return;
      }
      cardTitle.textContent = info.title;
      cardLevel.textContent = `Level ${info.level}`;
      cardDegree.textContent = `${info.degree} links`;
      cardCommunity.textContent = `Community ${info.communityId}`;
      cardLink.href = info.url;
      cardLink.textContent = "Open on Wikipedia";
      infoCard.hidden = false;
    },
  });
  activeLens = currentExperience.getLens();
  // Handle handed to the headless-browser seam (screen-level assertions).
  window.__wikigraph = currentExperience;
  syncUrl({ lens: activeLens });
  renderLegend(currentExperience);
  renderLensButtons(activeLens);
  truncationBox.hidden = !graphPayload.truncated;
  showNotice("");
  runTitle.textContent = graphPayload.seed ?? "Your Graph";
  runUrl.href = window.location.href;
}

for (const [button, lens] of [
  [lensCommunity, "community"],
  [lensLevel, "level"],
]) {
  button.addEventListener("click", () => {
    syncUrl({ lens: currentExperience.setLens(lens) });
    renderLegend(currentExperience);
    renderLensButtons(currentExperience.getLens());
  });
}

// Launch wiring (ticket 11): the launcher owns the form; this module owns the
// network flow — POST create run → SSE subscription → final Graph swap.
let activeSource = null;
function watchRun(runId) {
  activeSource?.close();
  const source = new EventSource(`/api/runs/${runId}/events`);
  activeSource = source;
  const stop = () => source.close();
  source.addEventListener("progress", (event) => {
    const progress = JSON.parse(event.data);
    launcher.updateProgress(progress);
    renderRunState({ runId, status: "running", ...progress, currentDepth: progress.depth });
    fetch(`/api/runs/${runId}/preview`).then((response) => response.ok ? response.json() : null).then((preview) => preview && mountExperience(preview));
  });
  for (const eventName of ["overload_waiting", "recoverable", "expired"]) {
    source.addEventListener(eventName, (event) => {
      stop();
      const data = JSON.parse(event.data);
      renderRunState({ runId, status: eventName, crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
      if (eventName === "recoverable" || eventName === "overload_waiting") launcher.fail(data.error);
      else launcher.fail(data.error ?? "This run has expired.");
    });
  }
  source.addEventListener("failed", (event) => {
    stop();
    launcher.fail(JSON.parse(event.data).error);
    renderRunState({ runId, status: "failed", crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
  });
  source.addEventListener("completed", async () => {
    stop();
    const response = await fetch(`/api/runs/${runId}/graph`);
    if (!response.ok) {
      launcher.fail(await errorMessage(response));
      return;
    }
    const stateResponse = await fetch(`/api/runs/${runId}`);
    const state = stateResponse.ok ? await stateResponse.json() : null;
    launcher.complete();
    mountExperience(await response.json());
    renderRunState(state ?? { runId, status: "completed", crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
    syncUrl({ run: runId });
  });
  source.onerror = () => { refreshRun(runId).catch(() => {}); };
}

const launcher = createLauncher(launchForm, async (values) => {
  const response = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      seed: values.seed,
      depth: values.depth,
      language: values.language,
      nodeCap: values.nodeCap,
    }),
  });
  if (!response.ok) return { error: await errorMessage(response) };
  const { runId } = await response.json();
  syncUrl({ run: runId });
  renderRunState({ runId, status: "running", crawled: 0, discovered: 1, currentDepth: 0, recent: [] });
  refreshRun(runId).catch((error) => showNotice(error.message));
  watchRun(runId);
  return {};
});

retryButton.addEventListener("click", async () => {
  const runId = retryButton.dataset.runId;
  if (!runId) return;
  const response = await fetch(`/api/runs/${runId}/retry`, { method: "POST" });
  if (!response.ok) {
    renderRunState({ runId, status: "failed", crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
    showNotice(await errorMessage(response));
    return;
  }
  renderRunState({ runId, status: "running", crawled: 0, discovered: 0, currentDepth: 0, recent: [] });
  watchRun(runId);
});

if (bootRunId) {
  try {
    const state = await refreshRun(bootRunId);
    if (state.status === "completed") {
      const response = await fetch(`/api/runs/${bootRunId}/graph`);
      if (response.ok) mountExperience(await response.json());
    } else if (state.status !== "expired" && state.status !== "failed") watchRun(bootRunId);
    if (state.status === "failed" || state.status === "expired") showNotice(state.error ?? LIFECYCLE_LABELS[state.status]);
  } catch (error) { showNotice(error.message); }
} else {
  showView("landing");
  showNotice("No Graph in view yet. Launch a crawl run and it will draw itself here.");
}
