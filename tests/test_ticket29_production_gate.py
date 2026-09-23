from __future__ import annotations

from benchmarks.ticket29_production_gate import (
    CriterionStatus,
    blocking_criteria,
    build_report,
    classify_local_benchmark,
)


def test_local_benchmark_is_not_labelled_as_deployed_evidence() -> None:
    report = build_report(
        local_benchmark={
            "dataset_nodes": 2_500,
            "result_nodes": 2_500,
            "limiter": {
                "max_in_flight": 1,
                "max_starts_in_second": 2,
                "max_attempts_per_minute": 120,
            },
        },
        live_probe={"healthz": True, "readyz": True},
    )

    assert report["criteria"]["representative_2500_benchmark"]["status"] == (
        CriterionStatus.INCOMPLETE.value
    )
    assert report["criteria"]["representative_2500_benchmark"]["origin"] == (
        "local-deterministic"
    )
    assert report["criteria"]["spanish_and_english_runs"]["status"] == (
        CriterionStatus.PENDING.value
    )


def test_local_benchmark_requires_all_budget_fields() -> None:
    evidence = classify_local_benchmark({"dataset_nodes": 2_500})

    assert evidence["status"] == CriterionStatus.INCOMPLETE.value
    assert "limiter.max_in_flight" in evidence["missing"]
    assert evidence["origin"] == "local-deterministic"


def test_live_readiness_does_not_prove_user_journeys() -> None:
    report = build_report(
        local_benchmark=None,
        live_probe={"healthz": True, "readyz": True},
    )

    assert report["criteria"]["persistence_failure_fail_closed"]["status"] == (
        CriterionStatus.LIVE_PROBE.value
    )
    assert report["criteria"]["spanish_and_english_runs"]["status"] == (
        CriterionStatus.PENDING.value
    )


def test_gate_rejects_any_incomplete_criterion() -> None:
    report = build_report(local_benchmark=None, live_probe={})

    assert set(blocking_criteria(report)) == {
        "spanish_and_english_runs",
        "reopen_and_retention",
        "restart_checkpoint_recovery",
        "representative_2500_benchmark",
        "global_budget_and_fairness",
        "overload_recovery",
        "persistence_failure_fail_closed",
        "higher_budget_disabled",
        "cutover_rejects_failed_checks",
    }
