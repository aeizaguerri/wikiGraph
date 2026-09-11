import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";

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

  const settings = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, { iterations: 200, settings });
  return graph;
}

export function createExperience(graphPayload, container) {
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
  return {
    graph,
    sigma,
    destroy() {
      sigma.kill();
    },
  };
}
