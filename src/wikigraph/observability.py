"""Structured, secret-free production evidence for ticket 29."""

from __future__ import annotations

import json
import logging
import resource
from typing import Any


logger = logging.getLogger("wikigraph.observability")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def emit(event: str, **fields: Any) -> None:
    """Emit one JSON log record without request bodies, titles, or credentials."""
    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))


def process_usage() -> dict[str, int | float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "user_cpu_seconds": usage.ru_utime,
        "system_cpu_seconds": usage.ru_stime,
        "max_rss_bytes": usage.ru_maxrss * 1024,
    }
