import { createExperience } from "./experience.js";
import "./style.css";

const container = document.getElementById("graph");
const noticeBox = document.getElementById("notice");
const statusBox = document.getElementById("status");

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
      },
    );
    // Handles handed to the headless-browser seam (screen-level assertions).
    window.__wikigraph = experience;
  }
}
