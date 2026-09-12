import {
  createExperience,
  parseLens,
} from "./experience.js";
import { createLauncher } from "./launcher.js";
import "./style.css";

const container = document.getElementById("graph");
const statusBox = document.getElementById("status");
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
  showNotice("");
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
function watchRun(runId) {
  const source = new EventSource(`/api/runs/${runId}/events`);
  const stop = () => source.close();
  source.addEventListener("progress", (event) => {
    launcher.updateProgress(JSON.parse(event.data));
  });
  source.addEventListener("failed", (event) => {
    stop();
    launcher.fail(JSON.parse(event.data).error);
  });
  source.addEventListener("completed", async () => {
    stop();
    const response = await fetch(`/api/runs/${runId}/graph`);
    if (!response.ok) {
      launcher.fail(await errorMessage(response));
      return;
    }
    launcher.complete();
    mountExperience(await response.json());
    syncUrl({ run: runId });
  });
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
  watchRun(runId);
  return {};
});

document.getElementById("launch-chip").addEventListener("click", () => {
  launcher.open();
});

if (bootRunId) {
  const response = await fetch(`/api/runs/${bootRunId}/graph`);
  if (!response.ok) {
    showNotice(await errorMessage(response));
  } else {
    mountExperience(await response.json());
  }
} else {
  showNotice("No Graph in view yet. Launch a crawl run and it will draw itself here.");
}
