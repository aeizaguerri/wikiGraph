import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";

// Placeholder visual constants; the community palette and encodings land in
// their own tickets (07, 13).
const NODE_SIZE = 4;
const NODE_COLOR = "#9db4dc";
const EDGE_COLOR = "#313d5b";
const LABEL_COLOR = "#dce4f4";
const ZOOM_THRESHOLD = 6;

// Reducers must exist before Sigma construction: the constructor renders.
const nodeReducer = (node, data) => data;
const edgeReducer = (edge, data) => data;

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
      size: NODE_SIZE,
      color: NODE_COLOR,
      x: rng() * 2 - 1,
      y: rng() * 2 - 1,
    });
  }
  for (const link of graphPayload.edges) {
    graph.addEdge(link.source, link.target, { color: EDGE_COLOR });
  }

  const settings = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, { iterations: 200, settings });
  return graph;
}

export function createExperience(graphPayload, container) {
  const graph = toGraph(graphPayload);
  const sigma = new Sigma(graph, container, {
    nodeReducer,
    edgeReducer,
    labelColor: { color: LABEL_COLOR },
    labelRenderedSizeThreshold: ZOOM_THRESHOLD,
  });
  return {
    graph,
    sigma,
    destroy() {
      sigma.kill();
    },
  };
}
