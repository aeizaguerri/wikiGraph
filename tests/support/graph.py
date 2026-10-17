"""Shared graph fixtures and browser-observable physics helpers."""

from typing import Any

from tests.support.mediawiki import FakeMediaWiki

# Outlast the frontend settle (6000 ms) and bounded decay (1500 ms).
STATIC_WAIT = 14_000

DECAY_WAIT = 4_000

POSITIONS_JS = """
() => {
  const graph = window.__wikigraph.graph;
  return graph.nodes().map((node) => {
    const { x, y } = graph.getNodeAttributes(node);
    return `${node}:${x.toFixed(9)},${y.toFixed(9)}`;
  }).sort();
}
"""

NODE_POS_JS = """
(node) => {
  const experience = window.__wikigraph;
  const attrs = experience.graph.getNodeAttributes(node);
  const point = experience.sigma.graphToViewport({ x: attrs.x, y: attrs.y });
  const rect = document.getElementById("graph").getBoundingClientRect();
  return { x: rect.left + point.x, y: rect.top + point.y };
}
"""

def populate(stub: FakeMediaWiki) -> None:
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

async def wait_static(page: Any, timeout: int = STATIC_WAIT) -> None:
    await page.wait_for_function("() => window.__wikigraph.getState() === 'static'",
                                 timeout=timeout)

async def page_decay(page: Any) -> None:
    await page.wait_for_function(
        "() => window.__wikigraph.getState() === 'static'", timeout=DECAY_WAIT
    )
