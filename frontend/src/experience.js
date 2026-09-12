import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import ForceSupervisor from "graphology-layout-force/worker";

// Encodings (ticket 07): size = degree, color = communityId.
// The palette is 12 fixed hues — golden angle (137.508°), s 62 / l 58, tuned
// for the dark canvas — for community ids 0-11; larger ids render neutral
// gray. Hex only: sigma's WebGL color parser has no hsl() support.
const COMMUNITY_PALETTE = [
  "#d65151",
  "#51d678",
  "#9f51d6",
  "#d6c651",
  "#51c0d6",
  "#d65199",
  "#73d651",
  "#5751d6",
  "#d67e51",
  "#51d6a5",
  "#cb51d6",
  "#bad651",
];
const NEUTRAL_COLOR = "#808080";
const MIN_NODE_SIZE = 2.5;
const SIZE_GROWTH = 1.1;
const EDGE_COLOR = "#313d5b";
const LABEL_COLOR = "#dce4f4";
const ZOOM_THRESHOLD = 6;

// Physics contract (ticket 08): settle once (worker FA2), then STATIC at rest.
// Only the first real drag movement re-heats (local supervisor burst) and the
// burst decays back to stillness by itself; selection clicks never touch it.
const SETTLE_MS = 6000;
const DECAY_MS = 1500;
const DRAG_TRIGGER_PX = 5;
const REHEAT_SETTINGS = {
  attraction: 0.0002,
  repulsion: 0.06,
  gravity: 0.0001,
  inertia: 0.8,
  maxMove: 8,
};

export function communityColor(communityId) {
  return COMMUNITY_PALETTE[communityId] ?? NEUTRAL_COLOR;
}

export function nodeSize(degree) {
  return MIN_NODE_SIZE + SIZE_GROWTH * Math.sqrt(degree);
}

// A seeded PRNG so identical Graphs lay out identically across boots.
function mulberry32(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function toGraph(graphPayload) {
  const graph = new Graph({ type: "directed", multi: false });
  const rng = mulberry32(42);

  for (const article of graphPayload.nodes) {
    graph.addNode(article.title, {
      label: article.title,
      level: article.level,
      isSeed: article.isSeed,
      communityId: article.communityId,
      color: communityColor(article.communityId),
      x: rng() * 2 - 1,
      y: rng() * 2 - 1,
    });
  }
  for (const link of graphPayload.edges) {
    graph.addEdge(link.source, link.target, { color: EDGE_COLOR });
  }
  for (const node of graph.nodes()) {
    graph.setNodeAttribute(node, "size", nodeSize(graph.degree(node)));
  }
  return graph;
}

export function createExperience(graphPayload, container, options = {}) {
  const graph = toGraph(graphPayload);

  // Reducers must exist before Sigma construction: the constructor renders.
  // The Seed page stays visually distinguishable at any zoom.
  const nodeReducer = (node, data) => {
    const res = { ...data };
    if (graph.getNodeAttribute(node, "isSeed")) {
      res.highlighted = true;
    }
    return res;
  };
  const edgeReducer = (edge, data) => data;

  const sigma = new Sigma(graph, container, {
    nodeReducer,
    edgeReducer,
    labelColor: { color: LABEL_COLOR },
    labelRenderedSizeThreshold: ZOOM_THRESHOLD,
  });

  // ---- physics lifecycle: settling → static, drag re-heats, then decays ----
  let physicsState = "settling";
  const onState = (options.onState ?? (() => {}));
  onState(physicsState);

  function setPhysicsState(next) {
    physicsState = next;
    onState(next);
  }

  let settleTimer = null;
  const fa2 = new FA2Layout(graph, {
    settings: forceAtlas2.inferSettings(graph),
  });
  fa2.start();
  settleTimer = setTimeout(stopSettle, SETTLE_MS);

  function stopSettle() {
    if (physicsState !== "settling") return;
    clearTimeout(settleTimer);
    settleTimer = null;
    fa2.stop();
    fa2.kill();
    setPhysicsState("static");
  }

  // The supervisor respects the "fixed" node attribute (FA2 does not), so it
  // owns every burst; a drag during settle swaps physics mid-flight.
  let supervisor = null;
  let decayTimer = null;

  function reheat() {
    if (physicsState !== "settling" && physicsState !== "static") return;
    stopSettle();
    if (!supervisor) {
      supervisor = new ForceSupervisor(graph, { settings: REHEAT_SETTINGS });
    }
    clearTimeout(decayTimer);
    supervisor.start();
    setPhysicsState("re-heating");
  }

  function startDecay() {
    if (physicsState !== "re-heating") return;
    clearTimeout(decayTimer);
    decayTimer = setTimeout(() => {
      decayTimer = null;
      supervisor.stop();
      setPhysicsState("static");
    }, DECAY_MS);
  }

  // ---- drag with re-heat on first real movement; clicks stay inert ----
  // Sigma v3 has no "downNode" event (that was v2): hit-test the mousedown
  // against the GPU picking buffer. A movement threshold keeps stray shakes
  // while pressing a node from counting as a drag.
  const mouseCaptor = sigma.getMouseCaptor();
  let dragging = null;

  mouseCaptor.on("mousedown", (e) => {
    const node = sigma.getNodeAtPosition(e);
    if (!node) return;
    dragging = { node, startX: e.x, startY: e.y };
    graph.setNodeAttribute(node, "fixed", true);
  });
  mouseCaptor.on("mousemove", (e) => {
    if (!dragging) return;
    const moved = Math.hypot(e.x - dragging.startX, e.y - dragging.startY);
    if (moved < DRAG_TRIGGER_PX) return;
    const pos = sigma.viewportToGraph(e);
    graph.setNodeAttribute(dragging.node, "x", pos.x);
    graph.setNodeAttribute(dragging.node, "y", pos.y);
    // Only genuine movement re-heats: plain selection clicks must leave the
    // settled layout untouched.
    if (physicsState !== "re-heating") reheat();
    e.preventSigmaDefault();
    e.original.preventDefault();
    e.original.stopPropagation();
  });
  const release = () => {
    if (!dragging) return;
    graph.setNodeAttribute(dragging.node, "fixed", false);
    dragging = null;
    if (physicsState === "re-heating") startDecay();
  };
  mouseCaptor.on("mouseup", release);
  window.addEventListener("mouseup", release);

  return {
    graph,
    sigma,
    getState: () => physicsState,
    destroy() {
      clearTimeout(settleTimer);
      clearTimeout(decayTimer);
      try {
        fa2.stop();
        fa2.kill();
      } catch {
        // worker may already be dead
      }
      if (supervisor) supervisor.kill();
      window.removeEventListener("mouseup", release);
      sigma.kill();
    },
  };
}
