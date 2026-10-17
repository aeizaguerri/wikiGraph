// Encodings (ticket 07): size = degree, color = communityId.
// The palette is 12 fixed hues — golden angle (137.508°), s 62 / l 58, tuned
// for the dark canvas — for community ids 0-11; larger ids render neutral
// gray. Hex only: sigma's WebGL color parser has no hsl() support.
export const COMMUNITY_PALETTE = [
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
export const NEUTRAL_COLOR = "#808080";
// Level lens (ticket 10): a 4-step ladder for the depth range 0-3, coolest
// (brightest on the dark canvas) at the Seed page; levels beyond 3 — which
// depth > 3 cannot produce — would fall back to the neutral gray.
export const LEVEL_LADDER = ["#f2f6ff", "#a9c3ea", "#5f83ad", "#3a5372"];
export const MIN_NODE_SIZE = 2.5;
export const SIZE_GROWTH = 1.1;

export function communityColor(communityId) {
  return COMMUNITY_PALETTE[communityId] ?? NEUTRAL_COLOR;
}

export function levelColor(level) {
  return LEVEL_LADDER[level] ?? NEUTRAL_COLOR;
}

// Color lens (ticket 10): community (primary) ⇄ level (secondary). The lens
// only re-colors nodes through the reducer — it never touches positions, so
// the settled layout survives the switch. `getLens`/`setLens` are the seam
// the URL sync and the legend read and drive.
export const COLOR_LENS_VALUES = ["community", "level"];

export function parseLens(value) {
  return COLOR_LENS_VALUES.includes(value) ? value : "community";
}

export function nodeSize(degree) {
  return MIN_NODE_SIZE + SIZE_GROWTH * Math.sqrt(degree);
}

export function wikipediaUrl(language, title) {
  return `https://${language}.wikipedia.org/wiki/${encodeURIComponent(
    title.replaceAll(" ", "_"),
  )}`;
}
