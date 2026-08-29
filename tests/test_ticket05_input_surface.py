"""Ticket 05 — input surface: URL-or-title seed, edition selector, errors, new runs."""

from __future__ import annotations

from tests.helpers import create_run, fetch_graph


async def test_pasted_url_with_fragment_equals_typed_title(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])

    by_url = await fetch_graph(
        client,
        await create_run(client, seed="https://es.wikipedia.org/wiki/Barn_owl#Hunting"),
    )
    by_title = await fetch_graph(
        client, await create_run(client, seed="Barn owl")
    )

    assert by_url["seed"] == by_title["seed"] == "Barn owl"


async def test_pasted_redirect_url_resolves_to_canonical_article(client, stub):
    stub.add_redirect("Barnstar", "Barn star")
    stub.add_page("Barn star")

    graph = await fetch_graph(
        client, await create_run(client, seed="https://es.wikipedia.org/wiki/Barnstar")
    )

    assert graph["seed"] == "Barn star"


async def test_index_php_url_form_is_accepted(client, stub):
    stub.add_page("Barn owl")

    graph = await fetch_graph(
        client,
        await create_run(
            client, seed="https://es.wikipedia.org/w/index.php?title=Barn_owl"
        ),
    )

    assert graph["seed"] == "Barn owl"


async def test_edition_defaults_to_spanish_and_crawl_stays_within_it(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])

    response = await client.post("/api/runs", json={"seed": "Barn owl", "depth": 1})
    assert response.status_code == 201, response.text
    graph = await fetch_graph(client, response.json()["runId"])

    assert graph["language"] == "es"
    assert {request.url.host for request in stub.requests} == {"es.wikipedia.org"}


async def test_edition_english_crawl_stays_within_english_host(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])

    graph = await fetch_graph(
        client, await create_run(client, seed="Barn owl", language="en")
    )

    assert graph["language"] == "en"
    assert {request.url.host for request in stub.requests} == {"en.wikipedia.org"}


async def test_url_edition_must_match_the_selected_edition(client, stub):
    stub.add_page("Barn owl")

    response = await client.post(
        "/api/runs",
        json={
            "seed": "https://en.wikipedia.org/wiki/Barn_owl",
            "depth": 1,
            "language": "es",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "edition_mismatch"


async def test_non_article_seed_is_rejected(client, stub):
    stub.add_page("File:Barn owl", namespace=6)

    response = await client.post(
        "/api/runs", json={"seed": "File:Barn owl", "depth": 1}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "not_an_article"


async def test_malformed_seed_inputs_are_rejected(client, stub):
    for seed in (
        "https://example.com/wiki/Barn_owl",
        "https://es.wikipedia.org/wiki/",
        "https://es.wikipedia.org/w/index.php",
        "   ",
    ):
        response = await client.post("/api/runs", json={"seed": seed, "depth": 1})
        assert response.status_code == 422, seed
        assert response.json()["error"]["code"] == "malformed_seed", seed


async def test_depth_outside_1_to_3_is_rejected(client, stub):
    stub.add_page("Barn owl")

    for depth in (0, 4, -1):
        response = await client.post(
            "/api/runs", json={"seed": "Barn owl", "depth": depth}
        )
        assert response.status_code == 422, depth


async def test_unknown_language_is_rejected(client, stub):
    response = await client.post(
        "/api/runs", json={"seed": "Barn owl", "depth": 1, "language": "fr"}
    )

    assert response.status_code == 422


async def test_new_run_works_after_a_completed_run(client, stub):
    stub.add_page("Barn owl", links=[(0, "Owl")])
    stub.add_page("Einstein", links=[(0, "Relativity")])
    stub.add_page("Relativity")

    first = await fetch_graph(client, await create_run(client, seed="Barn owl"))
    second = await fetch_graph(client, await create_run(client, seed="Einstein"))

    assert first["seed"] == "Barn owl"
    assert second["seed"] == "Einstein"
    assert {node["title"] for node in second["nodes"]} == {
        "Einstein",
        "Relativity",
    }
