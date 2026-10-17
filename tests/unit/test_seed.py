"""Pure input-normalization rules for seed titles and Wikipedia URLs."""

import pytest

from wikigraph.seed import ParsedSeed, SeedError, parse_seed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://es.wikipedia.org/wiki/Barn_owl#Hunting",
            ParsedSeed(title="Barn_owl", language="es"),
        ),
        (
            "https://es.wikipedia.org/wiki/Barnstar",
            ParsedSeed(title="Barnstar", language="es"),
        ),
        (
            "https://es.wikipedia.org/w/index.php?title=Barn_owl",
            ParsedSeed(title="Barn_owl", language="es"),
        ),
        ("Barn owl", ParsedSeed(title="Barn owl", language=None)),
    ],
)
def test_parse_seed_accepts_launch_input_examples(raw, expected):
    assert parse_seed(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            "https://example.com/wiki/Barn_owl",
            "The seed URL must point at a Wikipedia article.",
        ),
        (
            "https://es.wikipedia.org/wiki/",
            "The URL does not name an article.",
        ),
        (
            "https://es.wikipedia.org/w/index.php",
            "The URL does not name an article.",
        ),
        ("   ", "Provide a seed page title or a Wikipedia URL."),
    ],
)
def test_parse_seed_rejects_launch_input_examples(raw, message):
    with pytest.raises(SeedError) as error:
        parse_seed(raw)
    assert str(error.value) == message
