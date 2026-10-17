"""Browser regressions for live graph stability, redraw, and launch defaults."""

from tests.support.browser import BOOT_TIMEOUT, view_url
from tests.support.api import create_run
from tests.support.graph import NODE_POS_JS, POSITIONS_JS, page_decay


async def test_live_preview_keeps_canvas_and_camera_through_completion(
    stub, view_server, browser_page
):
    gate = stub.gate("Stable preview")
    stub.add_page("Stable preview", links=[(0, "New neighbour")])
    stub.add_page("New neighbour")
    run_id = await create_run(view_server, seed="Stable preview", depth=1)
    await browser_page.goto(f"{view_url(view_server, run_id)}&color=level")
    await browser_page.wait_for_function("() => window.__wikigraph", timeout=BOOT_TIMEOUT)
    await browser_page.evaluate("""() => {
      window.originalExperience = window.__wikigraph;
      window.originalCanvas = document.querySelector('#graph canvas');
      window.__wikigraph.sigma.getCamera().setState({ratio: 0.7});
    }""")
    # Exercise several real snapshot polls, with no graph changes.
    await browser_page.wait_for_timeout(2200)
    assert await browser_page.evaluate("() => window.__wikigraph === window.originalExperience")
    assert await browser_page.evaluate("() => document.querySelector('#graph canvas') === window.originalCanvas")
    assert await browser_page.evaluate("() => window.__wikigraph.sigma.getCamera().getState().ratio") == 0.7
    await browser_page.wait_for_function(
        "() => window.__wikigraph.getState() === 'static'", timeout=BOOT_TIMEOUT,
    )
    point = await browser_page.evaluate(NODE_POS_JS, 'Stable preview')
    await browser_page.mouse.click(point['x'], point['y'])
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") == 'Stable preview'
    camera = await browser_page.evaluate("() => window.__wikigraph.sigma.getCamera().getState()")
    gate.set()
    await browser_page.wait_for_function(
        "() => document.getElementById('run-waiting').hidden && window.__wikigraph.graph.hasNode('New neighbour')",
        timeout=BOOT_TIMEOUT,
    )
    assert await browser_page.evaluate("() => window.__wikigraph === window.originalExperience")
    assert await browser_page.evaluate("() => document.querySelector('#graph canvas') === window.originalCanvas")
    assert await browser_page.evaluate("() => window.__wikigraph.graph.hasDirectedEdge('Stable preview', 'New neighbour')")
    assert await browser_page.evaluate("() => window.__wikigraph.getLens()") == 'level'
    assert await browser_page.evaluate("() => window.__wikigraph.sigma.getCamera().getState().ratio") == 0.7
    # A snapshot arriving after initial settling must run physics without a drag.
    assert await browser_page.evaluate("() => window.__wikigraph.getState()") == 're-heating'
    before = await browser_page.evaluate(POSITIONS_JS)
    await browser_page.wait_for_timeout(200)
    assert await browser_page.evaluate(POSITIONS_JS) != before
    await page_decay(browser_page)
    assert await browser_page.evaluate("() => window.__wikigraph.sigma.getCamera().getState()") == camera
    assert await browser_page.evaluate("() => window.__wikigraph.getSelected()") == 'Stable preview'
    # Repeated polling of unchanged topology must leave the settled layout alone.
    settled = await browser_page.evaluate(POSITIONS_JS)
    await browser_page.wait_for_timeout(2200)
    assert await browser_page.evaluate("() => window.__wikigraph.getState()") == 'static'
    assert await browser_page.evaluate(POSITIONS_JS) == settled


async def test_static_graph_redraws_after_expand_and_collapse(stub, view_server, browser_page):
    stub.add_page("Redraw graph", links=[(0, "Neighbour")])
    stub.add_page("Neighbour")
    run_id = await create_run(view_server, seed="Redraw graph", depth=1)
    await browser_page.goto(view_url(view_server, run_id))
    await browser_page.wait_for_function(
        "() => window.__wikigraph?.getState() === 'static' && document.getElementById('run-waiting').hidden",
        timeout=BOOT_TIMEOUT,
    )
    await browser_page.evaluate("""() => {
      window.redraws = 0;
      window.__wikigraph.sigma.on('afterRender', () => window.redraws++);
    }""")
    for button in ["#graph-expand", "#graph-collapse"]:
        before = await browser_page.evaluate("() => window.redraws")
        # DOM click does not move the pointer over the canvas to force a repaint.
        await browser_page.locator(button).evaluate("button => button.click()")
        await browser_page.wait_for_timeout(200)
        assert await browser_page.evaluate("() => window.redraws") > before
