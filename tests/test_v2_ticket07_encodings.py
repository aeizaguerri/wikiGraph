"""v2 Ticket 07 — encodings: size by degree, color by communityId.

The palette contract is asserted on screen: inside Sigma's synchronous
``afterRender`` window the WebGL buffer is still live, so the browser reads
the pixels actually rasterized at each node's viewport position and checks
them against the pinned palette. Any page error fails the suite (harness).
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from tests.conftest import BOOT_TIMEOUT, boot_view, completed_run
from tests.helpers import fetch_graph
from tests.stub import FakeMediaWiki

# The pinned encoding contract, mirrored from frontend/src/experience.js:
# 12 fixed hues (golden-angle, s 62 / l 58) for communityIds 0-11, neutral
# gray beyond; size = 2.5 + 1.1 * sqrt(degree).
PALETTE = [
    "#d65151", "#51d678", "#9f51d6", "#d6c651", "#51c0d6", "#d65199",
    "#73d651", "#5751d6", "#d67e51", "#51d6a5", "#cb51d6", "#bad651",
]
NEUTRAL = "#808080"
COLOR_TOLERANCE = 3

SAMPLE_NODES_JS = """
() => {
  const experience = window.__wikigraph;
  const sigma = experience.sigma;
  const graph = experience.graph;
  const gl = sigma.webGLContexts.nodes;
  const dpr = sigma.pixelRatio;
  const samples = [];
  const handler = () => {
    sigma.removeListener("afterRender", handler);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    for (const node of graph.nodes()) {
      const attrs = graph.getNodeAttributes(node);
      const { x, y } = sigma.graphToViewport({ x: attrs.x, y: attrs.y });
      const pixel = new Uint8Array(4);
      gl.readPixels(
        Math.round(x * dpr),
        Math.round(gl.drawingBufferHeight - y * dpr),
        1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel
      );
      samples.push({
        node,
        degree: graph.degree(node),
        communityId: attrs.communityId,
        isSeed: attrs.isSeed,
        size: sigma.getNodeDisplayData(node).size,
        x,
        y,
        rgb: [pixel[0], pixel[1], pixel[2]],
      });
    }
  };
  sigma.on("afterRender", handler);
  sigma.refresh();
  return samples;
}
"""

MEASURE_RUNS_JS = """
(nodes) => {
  const experience = window.__wikigraph;
  const sigma = experience.sigma;
  const graph = experience.graph;
  const gl = sigma.webGLContexts.nodes;
  const dpr = sigma.pixelRatio;
  const rows = [];
  const handler = () => {
    sigma.removeListener("afterRender", handler);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    for (const node of nodes) {
      const attrs = graph.getNodeAttributes(node);
      const { x, y } = sigma.graphToViewport({ x: attrs.x, y: attrs.y });
      const row = new Uint8Array(gl.drawingBufferWidth * 4);
      gl.readPixels(
        0,
        Math.round(gl.drawingBufferHeight - y * dpr),
        gl.drawingBufferWidth, 1, gl.RGBA, gl.UNSIGNED_BYTE, row
      );
      rows.push({ node, centerX: Math.round(x * dpr), row: Array.from(row) });
    }
  };
  sigma.on("afterRender", handler);
  sigma.refresh();
  return rows;
}
"""

HOVER_INK_JS = """
(positions) => {
  const sigma = window.__wikigraph.sigma;
  const ctx = sigma.canvasContexts.hovers;
  const dpr = sigma.pixelRatio;
  const image = ctx.getImageData(0, 0, ctx.canvas.width, ctx.canvas.height).data;
  const width = ctx.canvas.width;
  const height = ctx.canvas.height;
  const inkNear = (pos, radius) => {
    const cx = Math.round(pos.x * dpr);
    const cy = Math.round(pos.y * dpr);
    const r = Math.ceil(radius * dpr);
    let ink = 0;
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const x = cx + dx;
        const y = cy + dy;
        if (x < 0 || y < 0 || x >= width || y >= height) continue;
        if (image[(y * width + x) * 4 + 3] > 0) ink += 1;
      }
    }
    return ink;
  };
  return positions.map((pos) => inkNear(pos, 14));
}
"""


def hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def matches(rendered: list[int], expected: tuple[int, ...]) -> bool:
    return all(
        abs(channel - wanted) <= COLOR_TOLERANCE
        for channel, wanted in zip(rendered, expected)
    )


def expected_color(community_id: int) -> tuple[int, int, int]:
    return hex_rgb(community_color(community_id))


def community_color(community_id: int) -> str:
    return PALETTE[community_id] if community_id < len(PALETTE) else NEUTRAL


def add_two_cluster_graph(stub: FakeMediaWiki) -> None:
    stub.add_page("Seed", links=[(0, "P1"), (0, "Q1")])
    for cluster in "PQ":
        for i in range(1, 4):
            stub.add_page(
                f"{cluster}{i}",
                links=[(0, f"{cluster}{j}") for j in range(1, 4) if j != i],
            )


def add_thirteen_cliques(stub: FakeMediaWiki) -> None:
    stub.add_page("Seed", links=[(0, f"A{i}") for i in range(13)])
    for i in range(13):
        stub.add_page(f"A{i}", links=[(0, f"B{i}"), (0, f"C{i}")])
        stub.add_page(f"B{i}", links=[(0, f"A{i}"), (0, f"C{i}")])
        stub.add_page(f"C{i}", links=[(0, f"A{i}"), (0, f"B{i}")])


async def sampled_nodes(page: Any) -> list[dict[str, Any]]:
    return await page.evaluate(SAMPLE_NODES_JS)


def run_lengths(rows: list[dict[str, Any]], colors: dict[str, str]) -> dict[str, int]:
    """Count the painted pixels around each node's center along its row."""
    lengths: dict[str, int] = {}
    for entry in rows:
        expected = hex_rgb(colors[entry["node"]])
        row = entry["row"]
        width = len(row) // 4

        def pixel(index: int) -> list[int]:
            base = index * 4
            return row[base : base + 3]

        left = entry["centerX"]
        while left > 0 and matches(pixel(left - 1), expected):
            left -= 1
        right = entry["centerX"]
        while right < width - 1 and matches(pixel(right + 1), expected):
            right += 1
        lengths[entry["node"]] = right - left + 1
    return lengths


async def test_nodes_paint_their_community_palette_color(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Every node renders exactly the palette hue of its communityId."""
    add_two_cluster_graph(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)
    graph = await fetch_graph(view_server, run_id)
    assert graph["communityCount"] >= 2
    assert len({node["communityId"] for node in graph["nodes"]}) >= 2

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)

    assert len(samples) == len(graph["nodes"])
    rendered_by_id: dict[int, set[tuple[int, ...]]] = {}
    for sample in samples:
        expected = expected_color(sample["communityId"])
        assert matches(sample["rgb"], expected), sample
        rendered_by_id.setdefault(sample["communityId"], set()).add(tuple(sample["rgb"]))
    distinct_ids = sorted(rendered_by_id)
    assert len(distinct_ids) >= 2
    rendered_colors = [next(iter(rendered_by_id[community_id])) for community_id in distinct_ids]
    assert len(set(rendered_colors)) == len(distinct_ids)


async def test_communities_beyond_the_twelve_hues_render_neutral_gray(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Ids past the palette's 12 hues fall back to the neutral gray on screen."""
    add_thirteen_cliques(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)
    graph = await fetch_graph(view_server, run_id)
    assert graph["communityCount"] >= 13, graph["communityCount"]
    gray_ids = {
        node["communityId"] for node in graph["nodes"] if node["communityId"] >= 12
    }
    assert gray_ids, graph["communityCount"]

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)

    gray = hex_rgb(NEUTRAL)
    hues = {hex_rgb(color) for color in PALETTE}
    for sample in samples:
        if sample["communityId"] >= 12:
            assert matches(sample["rgb"], gray), sample
            assert not any(matches(sample["rgb"], hue) for hue in hues), sample
        else:
            assert matches(sample["rgb"], expected_color(sample["communityId"])), sample


async def test_single_community_run_renders_one_hue_without_error(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """A one-node, single-community Graph boots clean and paints its one hue."""
    stub.add_page("Seed", links=[])
    run_id = await completed_run(stub, view_server, seed="Seed", depth=1)
    graph = await fetch_graph(view_server, run_id)
    assert graph["communityCount"] == 1

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)

    assert len(samples) == 1
    assert samples[0]["communityId"] == 0
    assert matches(samples[0]["rgb"], hex_rgb(PALETTE[0])), samples[0]
    assert not matches(samples[0]["rgb"], hex_rgb(NEUTRAL)), samples[0]


async def test_all_isolated_run_renders_singleton_hues_without_error(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """An all-isolated Graph boots clean: every singleton gets its own hue."""
    stub.add_page("Seed", links=[(0, "Lonely")])
    stub.add_page("Lonely", links=[])
    run_id = await completed_run(stub, view_server, seed="Seed", depth=1)
    graph = await fetch_graph(view_server, run_id)
    assert graph["communityCount"] == 2

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)

    assert len(samples) == 2
    ids = {sample["communityId"] for sample in samples}
    assert ids == {0, 1}
    for sample in samples:
        assert matches(sample["rgb"], expected_color(sample["communityId"])), sample
    colors = {tuple(sample["rgb"]) for sample in samples}
    assert len(colors) == 2


async def test_node_size_scales_with_degree_and_hubs_render_larger(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Sizes follow the degree encoding, and the hub's painted disc is wider."""
    stub.add_page(
        "Ana",
        [(0, "Biología"), (0, "Química"), (0, "Astronomía"), (0, "Historia")],
    )
    stub.add_page(
        "Biología",
        [(0, "Química"), (0, "Astronomía"), (0, "Ana"), (0, "Selva")],
    )
    stub.add_page("Química", [(0, "Biología")])
    stub.add_page("Astronomía", [(0, "Física")])
    stub.add_page("Historia")
    stub.add_page("Selva")
    stub.add_page("Física")
    run_id = await completed_run(stub, view_server, seed="Ana", depth=2)

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)

    for sample in samples:
        expected = 2.5 + 1.1 * math.sqrt(sample["degree"])
        assert abs(sample["size"] - expected) < 1e-6, sample

    hub = max(samples, key=lambda sample: sample["degree"])
    leaf = min(samples, key=lambda sample: sample["degree"])
    assert hub["degree"] > leaf["degree"]

    colors = {sample["node"]: community_color(sample["communityId"]) for sample in samples}
    rows = await browser_page.evaluate(MEASURE_RUNS_JS, [hub["node"], leaf["node"]])
    lengths = run_lengths(rows, colors)
    assert lengths[hub["node"]] > lengths[leaf["node"]], lengths


async def test_seed_page_is_distinguishable_in_the_view(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The Seed node carries a visible highlight halo; other nodes do not."""
    add_two_cluster_graph(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)

    await boot_view(browser_page, view_server, run_id)
    samples = await sampled_nodes(browser_page)
    seed = next(sample for sample in samples if sample["isSeed"])
    others = [sample for sample in samples if not sample["isSeed"]]
    farthest = max(
        others,
        key=lambda sample: math.hypot(sample["x"] - seed["x"], sample["y"] - seed["y"]),
    )

    positions = await browser_page.evaluate(
        """
        (nodes) => nodes.map((node) => {
          const experience = window.__wikigraph;
          const attrs = experience.graph.getNodeAttributes(node);
          return experience.sigma.graphToViewport({ x: attrs.x, y: attrs.y });
        })
        """,
        [seed["node"], farthest["node"]],
    )
    ink = await browser_page.evaluate(HOVER_INK_JS, positions)

    assert ink[0] > 0, ink
    assert ink[1] == 0, ink
