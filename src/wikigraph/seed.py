"""Seed page input normalization: a pasted Wikipedia URL or a typed title."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import ParseResult, parse_qs, unquote, urlparse

from wikigraph.titles import clean_title

WIKIPEDIA_HOST = re.compile(r"^([a-z-]{2,})\.wikipedia\.org$")
WIKI_PATH = "/wiki/"
INDEX_PATHS = {"/w/index.php", "/index.php"}


class SeedError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSeed:
    title: str
    language: str | None


def parse_seed(raw: str) -> ParsedSeed:
    text = raw.strip()
    if not text:
        raise SeedError("Provide a seed page title or a Wikipedia URL.")
    if "://" not in text and ".wikipedia.org/" in text:
        text = f"https://{text}"
    if "://" in text:
        return _parse_url_seed(text)
    title = clean_title(text)
    if not title:
        raise SeedError("The seed page has no article title.")
    return ParsedSeed(title, None)


def _parse_url_seed(text: str) -> ParsedSeed:
    url = urlparse(text)
    if url.scheme not in ("http", "https"):
        raise SeedError("The seed URL must be an http(s) Wikipedia URL.")
    match = WIKIPEDIA_HOST.match((url.hostname or "").lower())
    if match is None:
        raise SeedError("The seed URL must point at a Wikipedia article.")
    raw_title = _title_from_path(url)
    title = clean_title(unquote(raw_title))
    if not title:
        raise SeedError("The seed page has no article title.")
    return ParsedSeed(title, match.group(1))


def _title_from_path(url: ParseResult) -> str:
    if url.path.startswith(WIKI_PATH):
        raw = url.path[len(WIKI_PATH) :]
    elif url.path in INDEX_PATHS:
        raw = str(parse_qs(url.query).get("title", [""])[0])
    else:
        raise SeedError("The URL path does not point at an article.")
    if not raw.strip():
        raise SeedError("The URL does not name an article.")
    return raw
