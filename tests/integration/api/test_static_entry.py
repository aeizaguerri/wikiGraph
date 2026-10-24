"""Static entry point contract tests."""

from wikigraph.app import STATIC_DIR


async def test_root_serves_the_v2_experience_and_retires_v1(client) -> None:
    assert not (STATIC_DIR / "prototype-v2").exists()
    assert not (STATIC_DIR / "v2").exists()

    page = await client.get("/")

    assert page.status_code == 200
    assert 'id="landing-view"' in page.text
    assert 'class="c-search"' in page.text
    assert 'id="run-view"' in page.text
    assert 'id="lens-community"' in page.text
    assert "cytoscape" not in page.text.lower()

    for retired_asset in ("/app.js", "/style.css", "/v2/"):
        response = await client.get(retired_asset)
        assert response.status_code == 404, retired_asset
