import { createExperience } from "./experience.js";
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

const runId = new URLSearchParams(window.location.search).get("run");

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
  }
}
