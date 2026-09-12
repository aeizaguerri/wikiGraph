import {
  createExperience,
  parseLens,
} from "./experience.js";
import "./style.css";

const container = document.getElementById("graph");
const noticeBox = document.getElementById("notice");
const statusBox = document.getElementById("status");
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
const runId = urlParams.get("run");
const initialLens = parseLens(urlParams.get("color"));

function syncUrl(lens) {
  const url = new URL(window.location.href);
  url.searchParams.set("color", lens);
  window.history.replaceState(null, "", url);
}

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

if (!runId) {
  showNotice("No Graph in view yet. Launch a crawl run and it will draw itself here.");
} else {
  const response = await fetch(`/api/runs/${runId}/graph`);
  if (!response.ok) {
    showNotice(await errorMessage(response));
  } else {
    const experience = createExperience(
      await response.json(),
      container,
      {
        lens: initialLens,
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
      },
    );
    // Handles handed to the headless-browser seam (screen-level assertions).
    window.__wikigraph = experience;

    syncUrl(experience.getLens());
    renderLegend(experience);
    renderLensButtons(experience.getLens());
    for (const [button, lens] of [
      [lensCommunity, "community"],
      [lensLevel, "level"],
    ]) {
      button.addEventListener("click", () => {
        syncUrl(experience.setLens(lens));
        renderLegend(experience);
        renderLensButtons(experience.getLens());
      });
    }
  }
}
