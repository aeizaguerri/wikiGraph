"""Conservative evidence collector for the ticket 29 production gate.

This module deliberately does not launch a production Crawl or inject faults into
Wikimedia.  It combines a bounded local benchmark with safe public health probes
and keeps evidence origin explicit so local measurements cannot be reported as
Render or Supabase metrics.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from enum import Enum
from typing import Any


class CriterionStatus(str, Enum):
    PROVEN = "proven"
    LIVE_PROBE = "live-probe-only"
    LOCAL_ONLY = "local-only"
    INCOMPLETE = "incomplete"
    PENDING = "pending"


CRITERIA = (
    "spanish_and_english_runs",
    "reopen_and_retention",
    "restart_checkpoint_recovery",
    "representative_2500_benchmark",
    "global_budget_and_fairness",
    "overload_recovery",
    "persistence_failure_fail_closed",
    "higher_budget_disabled",
    "cutover_rejects_failed_checks",
)

BENCHMARK_FIELDS = (
    "completion_seconds",
    "upstream_requests",
    "continuation_requests",
    "cache_hits",
    "retry_delay_seconds",
    "fairness",
    "cpu_seconds",
    "memory_peak_bytes",
    "persistence_effects",
)


def _evidence(
    status: CriterionStatus,
    origin: str,
    *,
    detail: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status.value,
        "origin": origin,
        "detail": detail,
    }
    if metrics:
        result["metrics"] = metrics
    return result


def classify_local_benchmark(result: dict[str, Any]) -> dict[str, Any]:
    """Classify a benchmark without promoting missing operational metrics."""
    missing = [field for field in BENCHMARK_FIELDS if field not in result]
    limiter = result.get("limiter")
    if not isinstance(limiter, dict):
        missing.extend(
            [
                "limiter.max_in_flight",
                "limiter.max_starts_in_second",
                "limiter.max_attempts_per_minute",
            ]
        )
    else:
        missing.extend(
            f"limiter.{field}"
            for field in ("max_in_flight", "max_starts_in_second")
            if field not in limiter
        )
        if not {
            "max_attempts_per_minute",
            "max_attempts_in_minute",
        }.intersection(limiter):
            missing.append("limiter.max_attempts_per_minute")
    if result.get("dataset_nodes") != 2_500 or result.get("result_nodes") != 2_500:
        missing.append("dataset_nodes/result_nodes=2500")
    status = CriterionStatus.LOCAL_ONLY if not missing else CriterionStatus.INCOMPLETE
    evidence = _evidence(
        status,
        "local-deterministic",
        detail="Deterministic transport only; this is not Render CPU, memory, or persistence telemetry.",
        metrics=result,
    )
    if missing:
        evidence["missing"] = missing
    return evidence


def build_report(
    *, local_benchmark: dict[str, Any] | None, live_probe: dict[str, Any]
) -> dict[str, Any]:
    """Build a fail-closed report suitable for attaching to ticket 29."""
    criteria = {
        criterion: _evidence(
            CriterionStatus.PENDING,
            "not-run",
            detail="Requires controlled deployed acceptance evidence.",
        )
        for criterion in CRITERIA
    }
    if local_benchmark is not None:
        criteria["representative_2500_benchmark"] = classify_local_benchmark(
            local_benchmark
        )
        limiter = local_benchmark.get("limiter", {})
        if isinstance(limiter, dict) and all(
            limiter.get(field) == expected
            for field, expected in (
                ("max_in_flight", 1),
                ("max_starts_in_second", 2),
                ("max_attempts_per_minute", 120),
            )
        ):
            criteria["global_budget_and_fairness"] = _evidence(
                CriterionStatus.LOCAL_ONLY,
                "local-deterministic",
                detail="Local governor ceiling and deterministic fairness only; not deployed concurrency telemetry.",
                metrics=limiter,
            )
        elif (
            limiter.get("max_in_flight") == 1
            and limiter.get("max_starts_in_second") == 2
            and limiter.get("max_attempts_in_minute") == 120
        ):
            criteria["global_budget_and_fairness"] = _evidence(
                CriterionStatus.LOCAL_ONLY,
                "local-deterministic",
                detail="Local governor ceiling and deterministic fairness only; not deployed concurrency telemetry.",
                metrics=limiter,
            )
    if live_probe.get("healthz") and live_probe.get("readyz"):
        criteria["persistence_failure_fail_closed"] = _evidence(
            CriterionStatus.LIVE_PROBE,
            "deployed-public-health",
            detail="Public health/readiness endpoints succeeded; failure injection and retention remain unproven.",
        )
    return {
        "scope": "ticket-29-production-reliability",
        "launches_performed": 0,
        "fault_injections_performed": 0,
        "criteria": criteria,
    }


def probe(url: str, timeout: float = 20.0) -> tuple[bool, int | None, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "wikiGraph/ticket29-gate"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, response.status, response.read(512).decode("utf-8", "replace")
    except (OSError, urllib.error.URLError) as exc:
        return False, None, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-url", default="https://wikigraph.onrender.com")
    parser.add_argument("--output", type=argparse.FileType("w"), default="-")
    args = parser.parse_args()

    from benchmarks.ticket19_representative import run_benchmark
    import asyncio

    benchmark = asyncio.run(run_benchmark())
    health_ok, health_status, _ = probe(f"{args.render_url.rstrip('/')}/healthz")
    ready_ok, ready_status, _ = probe(f"{args.render_url.rstrip('/')}/readyz")
    report = build_report(
        local_benchmark=benchmark,
        live_probe={
            "healthz": health_ok,
            "healthz_status": health_status,
            "readyz": ready_ok,
            "readyz_status": ready_status,
        },
    )
    json.dump(report, args.output, indent=2, sort_keys=True)
    args.output.write("\n")


if __name__ == "__main__":
    main()
