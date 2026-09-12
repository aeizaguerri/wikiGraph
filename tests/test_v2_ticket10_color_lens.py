"""v2 Ticket 10 — color lens: community ⇄ level selector.

Two lenses on the same picture: an in-view selector switches the node
color encoding between the community palette and the level ladder live
(no re-layout); the choice is expressed in the URL (``?color=``) and
restored on reload; a legend names the active encoding and maps each
color to its meaning. The level lens stays honestly sparse on
cap-truncated runs — only the levels actually present are shown.

Requires the bundle built via ``npm run build`` (``frontend/``).
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import boot_view, completed_run
from tests.helpers import fetch_graph, wait_static
from tests.stub import FakeMediaWiki

# The pinned lens contract, mirrored from frontend/src/experience.js:
# the community palette (ticket 07) and a 4-step level ladder for the
# depth range 0-3; levels beyond the ladder render neutral gray.
PALETTE = [
    "#d65151", "#51d678", "#9f51d6", "#d6c651", "#51c0d6", "#d65199",
    "#73d651", "#5751d6", "#d67e51", "#51d6a5", "#cb51d6", "#bad651",
]
NEUTRAL = "#808080"
LEVEL_LADDER = ["#f2f6ff", "#a9c3ea", "#5f83ad", "#3a5372"]
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
        level: attrs.level,
        communityId: attrs.communityId,
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

LEGEND_STATE_JS = """
() => {
  const title = document.getElementById('legend-title');
  const entries = [...document.querySelectorAll('.legend-entry')];
  return {
    title: title ? title.textContent : null,
    entries: entries.map((entry) => ({
      label: entry.querySelector('.legend-label').textContent,
      color: entry.querySelector('.legend-swatch').style.backgroundColor,
    })),
  };
}
"""

# Sampling positions (or pixels) twice only compares like with like once the
# FA2 settle has ended and the layout is truly frozen.


def hex_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def matches(rendered: list[int], expected: tuple[int, ...]) -> bool:
    return all(
        abs(channel - wanted) <= COLOR_TOLERANCE
        for channel, wanted in zip(rendered, expected)
    )


def community_color(community_id: int) -> str:
    return PALETTE[community_id] if community_id < len(PALETTE) else NEUTRAL


def level_color(level: int) -> str:
    return LEVEL_LADDER[level] if level < len(LEVEL_LADDER) else NEUTRAL


def normalize_css(color: str) -> str:
    """Playwright reports backgroundColor as ``rgb(r, g, b)``."""
    parts = color.removeprefix("rgb(").removesuffix(")").split(",")
    rgb = tuple(int(part.strip()) for part in parts)
    return "#{:02x}{:02x}{:02x}".format(*rgb)


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


async def assert_paint(
    page: Any, color_of: Any
) -> None:
    for sample in await sampled_nodes(page):
        expected = hex_rgb(color_of(sample))
        assert matches(sample["rgb"], expected), sample


async def test_selector_switches_colors_live_without_relayout(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The selector repaints community ⇄ level in place; positions never move."""
    add_two_cluster_graph(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)
    graph = await fetch_graph(view_server, run_id)
    assert graph["communityCount"] >= 2

    await boot_view(browser_page, view_server, run_id)
    await wait_static(browser_page)
    await assert_paint(
        browser_page, lambda s: community_color(s["communityId"])
    )

    before = {s["node"]: (s["x"], s["y"]) for s in await sampled_nodes(browser_page)}
    await browser_page.click("#lens-level")
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getLens() === 'level'"
    )
    await assert_paint(browser_page, lambda s: level_color(s["level"]))

    after = {s["node"]: (s["x"], s["y"]) for s in await sampled_nodes(browser_page)}
    assert after == before, "the lens switch must not re-layout"

    await browser_page.click("#lens-community")
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getLens() === 'community'"
    )
    await assert_paint(
        browser_page, lambda s: community_color(s["communityId"])
    )


async def test_lens_choice_is_url_expressed_and_restored_on_reload(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Switching writes ?color=; reloading (or sharing) restores the lens."""
    add_two_cluster_graph(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)

    await boot_view(browser_page, view_server, run_id)
    assert await browser_page.evaluate("() => window.__wikigraph.getLens()") == "community"
    assert "color=community" in browser_page.url

    await browser_page.click("#lens-level")
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getLens() === 'level'"
    )
    assert "color=level" in browser_page.url

    await browser_page.reload()
    await browser_page.wait_for_function(
        "() => window.__wikigraph", timeout=10_000
    )
    assert await browser_page.evaluate("() => window.__wikigraph.getLens()") == "level"
    await assert_paint(browser_page, lambda s: level_color(s["level"]))

    # A shared URL boots straight into the requested lens...
    shared = browser_page.url.replace("color=level", "color=community")
    await browser_page.goto(shared)
    await browser_page.wait_for_function("() => window.__wikigraph")
    assert await browser_page.evaluate("() => window.__wikigraph.getLens()") == "community"
    await assert_paint(
        browser_page, lambda s: community_color(s["communityId"])
    )

    # ...and an unknown lens value falls back to the community default.
    bogus = browser_page.url.replace("color=community", "color=paisley")
    await browser_page.goto(bogus)
    await browser_page.wait_for_function("() => window.__wikigraph")
    assert await browser_page.evaluate("() => window.__wikigraph.getLens()") == "community"
    await assert_paint(
        browser_page, lambda s: community_color(s["communityId"])
    )


async def test_legend_names_the_active_encoding_and_its_colors(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The legend names the lens and maps every shown color to its meaning."""
    add_two_cluster_graph(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)
    graph = await fetch_graph(view_server, run_id)
    community_ids = sorted({node["communityId"] for node in graph["nodes"]})
    assert len(community_ids) >= 2

    await boot_view(browser_page, view_server, run_id)
    legend = await browser_page.evaluate(LEGEND_STATE_JS)
    assert "community" in legend["title"].lower(), legend
    assert [entry["label"] for entry in legend["entries"]] == [
        f"Community {community_id}" for community_id in community_ids
    ], legend
    assert [normalize_css(entry["color"]) for entry in legend["entries"]] == [
        community_color(community_id) for community_id in community_ids
    ], legend

    await browser_page.click("#lens-level")
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getLens() === 'level'"
    )
    levels = sorted({node["level"] for node in graph["nodes"]})
    legend = await browser_page.evaluate(LEGEND_STATE_JS)
    assert "level" in legend["title"].lower(), legend
    assert [entry["label"] for entry in legend["entries"]] == [
        f"Level {level}" for level in levels
    ], legend
    assert [normalize_css(entry["color"]) for entry in legend["entries"]] == [
        level_color(level) for level in levels
    ], legend


async def test_legend_folds_gray_communities_into_one_bucket(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """Ids past the 12-hue palette share gray: one legend entry, many ids."""
    add_thirteen_cliques(stub)
    run_id = await completed_run(stub, view_server, seed="Seed", depth=2)
    graph = await fetch_graph(view_server, run_id)
    community_ids = sorted({node["communityId"] for node in graph["nodes"]})
    assert 12 in community_ids, community_ids

    await boot_view(browser_page, view_server, run_id)
    legend = await browser_page.evaluate(LEGEND_STATE_JS)
    labels = [entry["label"] for entry in legend["entries"]]
    assert labels[-1].startswith("Larger communities"), legend
    assert "(gray)" in labels[-1], legend
    assert normalize_css(legend["entries"][-1]["color"]) == NEUTRAL, legend


async def test_level_lens_stays_honestly_sparse(
    stub: FakeMediaWiki, view_server: httpx.AsyncClient, browser_page: Any
) -> None:
    """The level lens shows only the levels the run actually reached."""
    stub.add_page("Seed", links=[(0, "Direct")])
    stub.add_page("Direct", links=[])
    run_id = await completed_run(stub, view_server, seed="Seed", depth=3)
    graph = await fetch_graph(view_server, run_id)
    levels = {node["level"] for node in graph["nodes"]}
    assert levels == {0, 1}, levels

    await boot_view(browser_page, view_server, run_id)
    await browser_page.click("#lens-level")
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getLens() === 'level'"
    )

    legend = await browser_page.evaluate(LEGEND_STATE_JS)
    assert [entry["label"] for entry in legend["entries"]] == [
        "Level 0",
        "Level 1",
    ], legend
    await assert_paint(browser_page, lambda s: level_color(s["level"]))
