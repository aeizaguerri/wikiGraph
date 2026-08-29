"use strict";

const LEVEL_COLORS = { 0: "#0f172a", 1: "#2563eb", 2: "#7c3aed", 3: "#059669" };

const form = document.getElementById("run-form");
const errorBox = document.getElementById("error");
const progressPanel = document.getElementById("progress");
const graphView = document.getElementById("graph-view");
const truncatedBanner = document.getElementById("truncated-banner");
const crawledCount = document.getElementById("crawled-count");
const discoveredCount = document.getElementById("discovered-count");
const frontierDepth = document.getElementById("frontier-depth");
const recentFeed = document.getElementById("recent-feed");
const startButton = document.getElementById("start-button");
const tooltip = document.getElementById("tooltip");

let eventSource = null;
let graphInstance = null;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetPanels();
  const body = {
    seed: document.getElementById("seed").value.trim(),
    language: document.getElementById("language").value,
    depth: Number(document.getElementById("depth").value),
    nodeCap: Number(document.getElementById("node-cap").value) || 500,
  };
  if (!body.seed) {
    showError("Provide a seed page title or a Wikipedia URL.");
    return;
  }
  startButton.disabled = true;
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    const { runId } = await response.json();
    progressPanel.classList.remove("hidden");
    follow(runId);
  } catch (err) {
    showError("Could not reach the wikiGraph server.");
  } finally {
    startButton.disabled = false;
  }
});

function follow(runId) {
  eventSource = new EventSource(`/api/runs/${runId}/events`);
  eventSource.addEventListener("progress", (event) => {
    const data = JSON.parse(event.data);
    crawledCount.textContent = data.crawled;
    discoveredCount.textContent = data.discovered;
    frontierDepth.textContent = data.depth;
    if (data.recent.length > 0) {
      const item = document.createElement("li");
      item.textContent = data.recent[data.recent.length - 1];
      recentFeed.prepend(item);
      while (recentFeed.children.length > 10) {
        recentFeed.removeChild(recentFeed.lastChild);
      }
    }
  });
  eventSource.addEventListener("completed", async (event) => {
    eventSource.close();
    eventSource = null;
    progressPanel.classList.add("hidden");
    const { truncated } = JSON.parse(event.data);
    if (truncated) {
      truncatedBanner.classList.remove("hidden");
    }
    const response = await fetch(`/api/runs/${runId}/graph`);
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    renderGraph(await response.json());
  });
  eventSource.addEventListener("failed", (event) => {
    eventSource.close();
    eventSource = null;
    progressPanel.classList.add("hidden");
    const data = JSON.parse(event.data);
    showError(data.error || "The crawl run failed.");
  });
  eventSource.onerror = () => {
    if (eventSource !== null && eventSource.readyState === EventSource.CLOSED) {
      eventSource = null;
      progressPanel.classList.add("hidden");
      showError("The connection to the crawl run was lost.");
    }
  };
}

async function renderGraph(graph) {
  graphView.classList.remove("hidden");
  if (graphInstance !== null) {
    graphInstance.destroy();
  }
  const elements = [
    ...graph.nodes.map((node) => ({
      data: { id: node.title, label: node.title },
      classes: node.isSeed ? "seed level-0" : `level-${node.level}`,
    })),
    ...graph.edges.map((edge) => ({
      data: { source: edge.source, target: edge.target },
    })),
  ];
  const cy = cytoscape({
    container: document.getElementById("graph"),
    elements,
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          "font-size": 9,
          color: "#1e293b",
          "text-wrap": "wrap",
          "text-max-width": 90,
          "background-color": "#2563eb",
          width: 22,
          height: 22,
        },
      },
      {
        selector: "node.level-1",
        style: { "background-color": LEVEL_COLORS[1] },
      },
      {
        selector: "node.level-2",
        style: { "background-color": LEVEL_COLORS[2] },
      },
      {
        selector: "node.level-3",
        style: { "background-color": LEVEL_COLORS[3] },
      },
      {
        selector: "node.seed",
        style: {
          "background-color": LEVEL_COLORS[0],
          shape: "star",
          width: 42,
          height: 42,
          "font-weight": "bold",
          "font-size": 12,
        },
      },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": "#94a3b8",
          "target-arrow-color": "#475569",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "arrow-scale": 1.2,
        },
      },
    ],
    layout: { name: "cose", animate: false, padding: 30 },
  });
  cy.on("mouseover", "node", (event) => {
    tooltip.textContent = event.target.data("label");
    tooltip.classList.remove("hidden");
  });
  cy.on("mousemove", "node", (event) => {
    tooltip.style.left = `${event.renderedPosition.x + 12}px`;
    tooltip.style.top = `${event.renderedPosition.y + 12}px`;
  });
  cy.on("mouseout", "node", () => tooltip.classList.add("hidden"));
  graphInstance = cy;
}

function resetPanels() {
  errorBox.classList.add("hidden");
  truncatedBanner.classList.add("hidden");
  graphView.classList.add("hidden");
  recentFeed.replaceChildren();
  if (eventSource !== null) {
    eventSource.close();
    eventSource = null;
  }
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (body.error && body.error.message) {
      return body.error.message;
    }
  } catch (err) {
    // fall through to the generic message
  }
  return `The request failed (HTTP ${response.status}).`;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}
