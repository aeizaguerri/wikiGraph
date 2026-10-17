from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from scripts.ci_cd import (
    ReleaseError,
    validate_artifact,
    validate_environment,
    validate_run_id,
    validate_source_run,
    preflight,
    deploy_render,
    poll_render_deploy,
    check_readiness,
)


SHA = "a" * 40
REPOSITORY = "aeizaguerri/wikiGraph"


def run(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "repository": {"full_name": REPOSITORY},
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
    }
    result.update(overrides)
    return result


def test_production_protection_is_rechecked_after_approval_from_workflow_source() -> None:
    from pathlib import Path

    workflow = Path(".github/workflows/deploy.yml").read_text()
    steps = workflow.split("      - name: ")[1:]

    def step_named(name: str) -> tuple[int, str]:
        for index, block in enumerate(steps):
            if block.startswith(name + "\n"):
                return index, block
        raise AssertionError(f"Missing workflow step: {name}")

    original_index, original = step_named("Validate CI run, artifact, and production gate")
    source_index, source_checkout = step_named("Check out workflow source")
    approved_source_index, approved_source_checkout = step_named("Check out workflow source for post-approval recheck")
    recheck_index, recheck = step_named("Recheck production protection after approval")
    deploy_checkout_index, deploy_checkout = step_named("Check out the validated source commit")
    render_index, _ = step_named("Deploy exact source commit to Render and verify readiness")
    cloudflare_index, _ = step_named("Deploy tested frontend to Cloudflare Worker")

    assert source_index < original_index
    assert original_index < approved_source_index < recheck_index < deploy_checkout_index < render_index < cloudflare_index
    assert "ref: ${{ github.sha }}" in source_checkout
    assert "ref: ${{ github.sha }}" in approved_source_checkout
    assert "python scripts/ci_cd.py preflight" in original
    assert "python scripts/ci_cd.py preflight" in recheck
    for env_name, value in (
        ("GITHUB_TOKEN", "${{ github.token }}"),
        ("RELEASE_PREFLIGHT_TOKEN", "${{ secrets.RELEASE_PREFLIGHT_TOKEN }}"),
        ("GITHUB_REPOSITORY", "${{ github.repository }}"),
        ("GITHUB_REF", "${{ github.ref }}"),
        ("CI_RUN_ID", "${{ inputs.ci_run_id }}"),
    ):
        assert f"{env_name}: {value}" in original
        assert f"{env_name}: {value}" in recheck
    assert 'if [[ "$sha" != "${{ needs.preflight.outputs.sha }}" ]]' in recheck
    assert "ref: ${{ needs.preflight.outputs.sha }}" in deploy_checkout


def test_run_id_accepts_digits_only() -> None:
    assert validate_run_id("123456") == "123456"
    for value in ("", "12x", "1/../2", " 12"):
        with pytest.raises(ReleaseError):
            validate_run_id(value)


def test_preflight_reads_source_artifact_and_environment_without_live_api_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    run_data = run()
    responses = {
        f"/repos/{REPOSITORY}/actions/runs/123456": run_data,
        f"/repos/{REPOSITORY}/compare/{SHA}...main": {"status": "ahead"},
        f"/repos/{REPOSITORY}/actions/runs/123456/artifacts": {"artifacts": [{"id": 12, "name": f"frontend-{SHA}", "expired": False}]},
        f"/repos/{REPOSITORY}/environments/production": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}], "deployment_branch_policy": {"custom_branch_policies": True}},
        f"/repos/{REPOSITORY}/environments/production/deployment-branch-policies": {"branch_policies": [{"name": "main"}]},
    }
    calls: list[str] = []

    def github_read(path: str, token: str) -> Any:
        assert token == "read-only-test-token"
        calls.append(path)
        return responses[path]

    monkeypatch.setattr("scripts.ci_cd._github_json", github_read)
    assert preflight() == SHA
    assert len(calls) == 5
    responses[f"/repos/{REPOSITORY}/actions/runs/123456/artifacts"] = {"artifacts": [{"id": 12, "name": "frontend-" + "b" * 40, "expired": False}]}
    with pytest.raises(ReleaseError):
        preflight()


def test_source_run_must_be_successful_main_ci_for_this_repository() -> None:
    validate_source_run(run(), REPOSITORY, SHA, "ahead")
    for bad_run in (
        run(repository={"full_name": "attacker/fork"}),
        run(path=".github/workflows/untrusted.yml"),
        run(event="pull_request"),
        run(head_branch="feature"),
        run(head_sha="not-a-commit"),
        run(status="in_progress", conclusion=None),
        run(conclusion="failure"),
    ):
        with pytest.raises(ReleaseError):
            validate_source_run(bad_run, REPOSITORY, SHA, "ahead")


def test_source_sha_must_still_be_an_ancestor_of_main() -> None:
    for relation in ("behind", "diverged", "unknown"):
        with pytest.raises(ReleaseError):
            validate_source_run(run(), REPOSITORY, SHA, relation)


def test_artifact_must_be_exact_and_unexpired() -> None:
    artifact = {"id": 12, "name": f"frontend-{SHA}", "expired": False}
    validate_artifact(artifact, SHA)
    for bad_artifact in (
        {"name": "frontend-" + "b" * 40, "expired": False},
        {"name": f"frontend-{SHA}", "expired": True},
        {"name": f"frontend-{SHA}"},
        {"name": f"frontend-{SHA}", "expired": False, "id": None},
    ):
        with pytest.raises(ReleaseError):
            validate_artifact(bad_artifact, SHA)


def test_production_environment_requires_reviewers_and_main_restriction() -> None:
    validate_environment(
        {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}], "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
        [{"name": "main"}],
    )
    with pytest.raises(ReleaseError):
        validate_environment({"protection_rules": [], "deployment_branch_policy": {"custom_branch_policies": True}}, [{"name": "main"}])
    with pytest.raises(ReleaseError):
        validate_environment({"protection_rules": [{"type": "required_reviewers", "reviewers": []}], "deployment_branch_policy": {"custom_branch_policies": True}}, [{"name": "main"}])
    with pytest.raises(ReleaseError):
        validate_environment({"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}], "deployment_branch_policy": {"custom_branch_policies": True}}, [{"name": "release"}])


@dataclass
class FakeRender:
    service: dict[str, Any]
    post_response: tuple[int, dict[str, Any]]
    get_responses: list[tuple[int, dict[str, Any]]]
    posts: int = 0

    def get_service(self, service_id: str) -> dict[str, Any]:
        return self.service

    def post_deploy(self, service_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.posts += 1
        return self.post_response

    def get_deploy(self, service_id: str, deploy_id: str) -> tuple[int, dict[str, Any]]:
        return self.get_responses.pop(0)


def render_api(**overrides: Any) -> FakeRender:
    service = {"repo": "https://github.com/aeizaguerri/wikiGraph", "branch": "main", "autoDeploy": "no"}
    service.update(overrides)
    return FakeRender(service, (201, {"id": "dep-1", "commit": {"id": SHA}, "status": "created"}), [(200, {"id": "dep-1", "commit": {"id": SHA}, "status": "live"})])


def test_render_deploy_posts_exact_sha_once_and_rejects_bad_service_or_uncertain_response() -> None:
    api = render_api()
    deploy_id = deploy_render(api, "svc", SHA, REPOSITORY, "main")
    assert deploy_id == "dep-1"
    assert api.posts == 1
    for bad_service in (
        render_api(repo="https://github.com/other/repo"),
        render_api(branch="release"),
        render_api(autoDeploy="yes"),
    ):
        with pytest.raises(ReleaseError):
            deploy_render(bad_service, "svc", SHA, REPOSITORY, "main")
        assert bad_service.posts == 0
    for response in ((202, {"status": "queued"}), (201, {"id": "dep-2", "commit": {"id": "b" * 40}, "status": "created"}), (500, {})):
        uncertain = render_api()
        uncertain.post_response = response
        with pytest.raises(ReleaseError):
            deploy_render(uncertain, "svc", SHA, REPOSITORY, "main")
        assert uncertain.posts == 1


def test_render_polling_requires_exact_commit_and_live_or_known_status() -> None:
    api = render_api()
    assert poll_render_deploy(api, "svc", "dep-1", SHA, max_polls=1, interval=0, sleep=lambda _: None)["status"] == "live"
    for response in (
        (200, {"id": "dep-1", "commit": {"id": "b" * 40}, "status": "live"}),
        (200, {"id": "dep-1", "commit": {"id": SHA}, "status": "unknown"}),
        (200, {"id": "dep-1", "commit": {"id": SHA}, "status": "build_failed"}),
        (200, {"id": "dep-1", "commit": {"id": SHA}, "status": "created"}),
    ):
        pending = render_api()
        pending.get_responses = [response]
        with pytest.raises(ReleaseError):
            poll_render_deploy(pending, "svc", "dep-1", SHA, max_polls=1, interval=0, sleep=lambda _: None)


def test_readiness_is_checked_only_at_fixed_render_origin() -> None:
    check_readiness("https://wikigraph.onrender.com/readyz", lambda _: 200)
    for status in (404, 503):
        with pytest.raises(ReleaseError):
            check_readiness("https://wikigraph.onrender.com/readyz", lambda _: status)
    with pytest.raises(ReleaseError):
        check_readiness("https://attacker.example/readyz", lambda _: 200)
