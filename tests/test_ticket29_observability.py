from __future__ import annotations

import json
import logging

from wikigraph.observability import emit, process_usage


def test_observability_emits_structured_secret_free_records(
    caplog: object,
) -> None:
    with caplog.at_level(logging.INFO, logger="wikigraph.observability"):  # type: ignore[attr-defined]
        emit("wikimedia_attempt", owner="opaque-run", status_code=200)

    record = json.loads(caplog.records[-1].message)  # type: ignore[attr-defined]
    assert record == {
        "event": "wikimedia_attempt",
        "owner": "opaque-run",
        "status_code": 200,
    }
    assert "Authorization" not in caplog.records[-1].message  # type: ignore[attr-defined]


def test_process_usage_has_cpu_and_memory_measurements() -> None:
    usage = process_usage()

    assert usage["user_cpu_seconds"] >= 0
    assert usage["system_cpu_seconds"] >= 0
    assert usage["max_rss_bytes"] > 0
