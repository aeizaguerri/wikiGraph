"""Shared title cleaning: strip section fragments and surrounding whitespace."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def clean_title(title: str) -> str:
    """Strip `#section` fragments, collapse whitespace, and trim."""
    without_fragment = title.split("#", 1)[0]
    return _WHITESPACE.sub(" ", without_fragment).strip()
