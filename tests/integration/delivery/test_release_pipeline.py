from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from scripts.ci_cd import (
    ReleaseError,
    validate_artifact,
    validate_run_id,
    validate_source_run,
    preflight,
    deploy_render,
    poll_render_deploy,
    check_readiness,
)


SHA = "a" * 40
REPOSITORY = "aeizaguerri/wikiGraph"


def test_ci_runs_frontend_and_python_test_levels_in_order_with_browser_diagnostics_scoped() -> None:
    from pathlib import Path

    workflow = Path(".github/workflows/ci.yml").read_text()
    steps = workflow.split("      - name: ")[1:]

    def step_named(name: str) -> tuple[int, str]:
        for index, block in enumerate(steps):
            if block.startswith(name + "\n"):
                return index, block
        raise AssertionError(f"Missing CI workflow step: {name}")

    install_index, _ = step_named("Install frontend dependencies")
    frontend_index, frontend = step_named("Run frontend unit tests")
    build_index, _ = step_named("Build frontend")
    unit_index, unit = step_named("Run Python unit tests")
    integration_index, integration = step_named("Run Python integration tests")
    chromium_index, _ = step_named("Install Chromium for browser tests")
    e2e_index, e2e = step_named("Run Python end-to-end tests")
    mypy_index, _ = step_named("Run mypy")
    worker_index, worker = step_named("Run Cloudflare Worker tests")

    assert install_index < frontend_index < build_index < unit_index < integration_index < chromium_index < e2e_index < mypy_index < worker_index
    assert "npm --prefix frontend test" in frontend
    assert "uv run pytest tests/unit" in unit
    assert "uv run pytest tests/integration" in integration
    assert "uv run pytest tests/e2e" in e2e
    assert 'WIKIGRAPH_GRAPH_CLICK_DIAGNOSTICS: "1"' in e2e
    assert "WIKIGRAPH_GRAPH_CLICK_DIAGNOSTICS" not in unit + integration
    assert "node --test cloudflare/worker.test.js" in worker


def run(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "repository": {"full_name": REPOSITORY},
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-02-01T12:00:00Z",
    }
    result.update(overrides)
    return result


def test_release_identity_is_rechecked_after_environment_from_workflow_source() -> None:
    from pathlib import Path

    workflow = Path(".github/workflows/deploy.yml").read_text()
    steps = workflow.split("      - name: ")[1:]

    def step_named(name: str) -> tuple[int, str]:
        for index, block in enumerate(steps):
            if block.startswith(name + "\n"):
                return index, block
        raise AssertionError(f"Missing workflow step: {name}")

    original_index, original = step_named("Resolve and validate CI run and artifact")
    source_index, source_checkout = step_named("Check out workflow source")
    approved_source_index, approved_source_checkout = step_named("Check out workflow source for post-environment recheck")
    recheck_index, recheck = step_named("Recheck release identity after environment")
    deploy_checkout_index, deploy_checkout = step_named("Check out the validated source commit")
    render_index, _ = step_named("Deploy exact source commit to Render and verify readiness")
    cloudflare_index, _ = step_named("Deploy tested frontend to Cloudflare Worker")

    assert source_index < original_index
    assert original_index < approved_source_index < recheck_index < deploy_checkout_index < render_index < cloudflare_index
    assert "ref: ${{ github.sha }}" in source_checkout
    assert "ref: ${{ github.sha }}" in approved_source_checkout
    assert "RELEASE_PREFLIGHT_TOKEN" not in workflow
    assert "deployments: read" not in workflow
    assert "python scripts/ci_cd.py select-run" in original
    assert "python scripts/ci_cd.py preflight" in original
    assert "python scripts/ci_cd.py preflight" in recheck
    for env_name, value in (
        ("GITHUB_TOKEN", "${{ github.token }}"),
        ("GITHUB_REPOSITORY", "${{ github.repository }}"),
        ("GITHUB_REF", "${{ github.ref }}"),
    ):
        assert f"{env_name}: {value}" in original
        assert f"{env_name}: {value}" in recheck
    assert "CI_RUN_ID: ${{ inputs.ci_run_id }}" in original
    assert 'CI_RUN_ID="$run_id" python scripts/ci_cd.py preflight' in original
    assert "CI_RUN_ID: ${{ needs.preflight.outputs.run_id }}" in recheck
    assert "run-id: ${{ needs.preflight.outputs.run_id }}" in step_named("Download the exact tested frontend artifact")[1]
    assert "run_id: ${{ steps.verify.outputs.run_id }}" in workflow
    assert 'if [[ "$sha" != "${{ needs.preflight.outputs.sha }}" ]]' in recheck
    assert "ref: ${{ needs.preflight.outputs.sha }}" in deploy_checkout


def test_select_run_orders_all_bounded_pages_by_created_at(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    older = run()
    older.update(id=111, created_at="2026-01-01T00:00:00Z")
    first_page = [older] + [run(event="pull_request", id=number) for number in range(1000, 1099)]
    newest = run(head_sha="b" * 40)
    newest.update(id=222, created_at="2026-03-01T00:00:00Z")
    responses = {
        "1": {"workflow_runs": first_page},
        "2": {"workflow_runs": [newest]},
        f"/repos/{REPOSITORY}/compare/{SHA}...main": {"status": "ahead"},
        f"/repos/{REPOSITORY}/actions/runs/{older['id']}/artifacts": {"artifacts": [{"id": 11, "name": f"frontend-{SHA}", "expired": False}]},
        f"/repos/{REPOSITORY}/compare/{'b' * 40}...main": {"status": "ahead"},
        f"/repos/{REPOSITORY}/actions/runs/{newest['id']}/artifacts": {"artifacts": [{"id": 22, "name": f"frontend-{'b' * 40}", "expired": False}]},
    }
    listing_pages: list[str] = []
    listing_queries: list[dict[str, list[str]]] = []
    calls: list[tuple[str, str]] = []

    def request(url: str, *, token: str | None = None, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(url)
        calls.append((parsed.path, method))
        assert token == "read-only-test-token" and method == "GET"
        if parsed.path.endswith("/workflows/ci.yml/runs"):
            query = parse_qs(parsed.query)
            listing_queries.append(query)
            assert query["branch"] == ["main"] and query["event"] == ["push"]
            assert query["status"] == ["completed"] and query["per_page"] == ["100"]
            page = query["page"][0]
            listing_pages.append(page)
            return 200, responses[page]
        return 200, responses[parsed.path]

    monkeypatch.setattr(ci_cd, "_request", request)
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 0
    assert capsys.readouterr().out == f"222 {'b' * 40}\n"
    assert listing_pages == ["1", "2"]
    assert all(set(query) == {"branch", "event", "status", "per_page", "page"} for query in listing_queries)
    assert (f"/repos/{REPOSITORY}/actions/runs/{older['id']}/artifacts", "GET") not in calls


def test_select_run_fails_closed_on_malformed_ordering_metadata(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    candidate = run(created_at="2026-02-01 12:00:00Z", id=456)
    calls: list[str] = []

    def request(url: str, **kwargs: Any) -> tuple[int, Any]:
        from urllib.parse import urlparse
        calls.append(urlparse(url).path)
        return 200, {"workflow_runs": [candidate]}

    monkeypatch.setattr(ci_cd, "_request", request)
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Release stopped safely: GitHub workflow run has malformed created_at ordering metadata\n"
    assert calls == [f"/repos/{REPOSITORY}/actions/workflows/ci.yml/runs"]


def test_select_run_rejects_when_no_unexpired_exact_sha_artifact_exists(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    candidate = run()
    candidate["id"] = 123
    monkeypatch.setattr(ci_cd, "_request", lambda url, **kwargs: (200, {"workflow_runs": [candidate]} if "/workflows/" in url else {"status": "ahead"} if "/compare/" in url else {"artifacts": []}))
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Release stopped safely: No successful main CI run has a valid unexpired exact-SHA frontend artifact\n"


def test_select_run_does_not_hide_github_api_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    calls: list[tuple[str, str]] = []

    def request(url: str, *, token: str | None = None, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        from urllib.parse import urlparse
        calls.append((urlparse(url).path, method))
        return 403, {"message": "denied"}

    monkeypatch.setattr(ci_cd, "_request", request)
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Release stopped safely: GitHub GET /repos/aeizaguerri/wikiGraph/actions/workflows/ci.yml/runs?branch=main&event=push&status=completed&per_page=100&page=1 failed with HTTP 403\n"
    assert calls == [(f"/repos/{REPOSITORY}/actions/workflows/ci.yml/runs", "GET")]


def test_select_run_rejects_untrusted_explicit_id_without_api_calls(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "123;echo unsafe")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    calls: list[str] = []
    monkeypatch.setattr(ci_cd, "_request", lambda url, **kwargs: calls.append(url))
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Release stopped safely: CI run ID must contain digits only\n"
    assert calls == []


def test_select_run_preserves_explicit_run_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    responses = {
        f"/repos/{REPOSITORY}/actions/runs/123456": run(),
        f"/repos/{REPOSITORY}/compare/{SHA}...main": {"status": "ahead"},
        f"/repos/{REPOSITORY}/actions/runs/123456/artifacts": {"artifacts": [{"id": 12, "name": f"frontend-{SHA}", "expired": False}]},
    }
    def request(url: str, **kwargs: Any) -> tuple[int, Any]:
        from urllib.parse import urlparse
        return 200, responses[urlparse(url).path]

    monkeypatch.setattr(ci_cd, "_request", request)
    monkeypatch.setattr("sys.argv", ["ci_cd.py", "select-run"])
    assert ci_cd.main() == 0
    assert capsys.readouterr().out == f"123456 {SHA}\n"


def test_run_id_accepts_digits_only() -> None:
    assert validate_run_id("123456") == "123456"
    for value in ("", "12x", "1/../2", " 12"):
        with pytest.raises(ReleaseError):
            validate_run_id(value)


def test_preflight_accepts_exact_main_artifact_without_environment_api_access(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.ci_cd as ci_cd

    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("CI_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only-test-token")
    monkeypatch.setenv("RELEASE_PREFLIGHT_TOKEN", "must-not-override")
    responses = {
        f"/repos/{REPOSITORY}/actions/runs/123456": run(),
        f"/repos/{REPOSITORY}/compare/{SHA}...main": {"status": "ahead"},
        f"/repos/{REPOSITORY}/actions/runs/123456/artifacts": {"artifacts": [{"id": 12, "name": f"frontend-{SHA}", "expired": False}]},
    }
    calls: list[tuple[str, str | None, str]] = []

    def request(url: str, *, token: str | None = None, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        from urllib.parse import urlparse
        path = urlparse(url).path
        calls.append((path, token, method))
        if path.endswith("/environments/production") or "/deployment-branch-policies" in path:
            return 404, {"message": "unavailable"}
        return 200, responses[path]

    monkeypatch.setattr(ci_cd, "_request", request)
    result = preflight()
    assert result == SHA
    assert capsys.readouterr().out == ""
    assert calls == [
        (f"/repos/{REPOSITORY}/actions/runs/123456", "read-only-test-token", "GET"),
        (f"/repos/{REPOSITORY}/compare/{SHA}...main", "read-only-test-token", "GET"),
        (f"/repos/{REPOSITORY}/actions/runs/123456/artifacts", "read-only-test-token", "GET"),
    ]


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
