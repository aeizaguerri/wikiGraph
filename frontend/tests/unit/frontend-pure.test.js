import test from "node:test";
import assert from "node:assert/strict";

import { clampCap } from "../../src/launcher.js";
import {
  COLOR_LENS_VALUES,
  communityColor,
  levelColor,
  nodeSize,
  parseLens,
  wikipediaUrl,
} from "../../src/graph-style.js";

test("clampCap uses the launch cap defaults and inclusive limits", () => {
  assert.equal(clampCap("not a number"), 500);
  assert.equal(clampCap(""), 500);
  assert.equal(clampCap("0"), 1);
  assert.equal(clampCap("1"), 1);
  assert.equal(clampCap("5000"), 5000);
  assert.equal(clampCap("5001"), 5000);
  assert.equal(clampCap("17.9"), 17);
});

test("communityColor returns the fixed palette and neutral fallback", () => {
  assert.deepEqual(
    Array.from({ length: 12 }, (_, id) => communityColor(id)),
    [
      "#d65151", "#51d678", "#9f51d6", "#d6c651", "#51c0d6", "#d65199",
      "#73d651", "#5751d6", "#d67e51", "#51d6a5", "#cb51d6", "#bad651",
    ],
  );
  assert.equal(communityColor(12), "#808080");
  assert.equal(communityColor(-1), "#808080");
});

test("levelColor returns the depth ladder and neutral fallback", () => {
  assert.deepEqual(
    Array.from({ length: 4 }, (_, level) => levelColor(level)),
    ["#f2f6ff", "#a9c3ea", "#5f83ad", "#3a5372"],
  );
  assert.equal(levelColor(4), "#808080");
  assert.equal(levelColor(-1), "#808080");
});

test("parseLens accepts only the published lens values", () => {
  assert.deepEqual(COLOR_LENS_VALUES, ["community", "level"]);
  assert.equal(parseLens("community"), "community");
  assert.equal(parseLens("level"), "level");
  assert.equal(parseLens("paisley"), "community");
  assert.equal(parseLens(undefined), "community");
});

test("nodeSize uses square-root degree scaling", () => {
  assert.equal(nodeSize(0), 2.5);
  assert.equal(nodeSize(1), 3.6);
  assert.ok(Math.abs(nodeSize(9) - 5.8) < 1e-12);
});

test("wikipediaUrl encodes titles after replacing spaces with underscores", () => {
  assert.equal(
    wikipediaUrl("es", "Biología y Química"),
    "https://es.wikipedia.org/wiki/Biolog%C3%ADa_y_Qu%C3%ADmica",
  );
  assert.equal(
    wikipediaUrl("en", "C++/C#? & spaces"),
    "https://en.wikipedia.org/wiki/C%2B%2B%2FC%23%3F_%26_spaces",
  );
});
