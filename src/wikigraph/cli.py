"""Run the wikiGraph server: `wikigraph`."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("wikigraph.app:app", host="127.0.0.1", port=8000)
