"""Fail-closed validation and Render release operations for the CD workflow."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"
RENDER_ORIGIN = "https://wikigraph.onrender.com"
CI_WORKFLOW = ".github/workflows/ci.yml"
PENDING_STATUSES = {
    "created", "queued", "build_in_progress", "update_in_progress",
    "pre_deploy_in_progress",
}
FAILED_STATUSES = {
    "deactivated", "build_failed", "update_failed", "canceled",
    "pre_deploy_failed",
}


class ReleaseError(RuntimeError):
    """A release precondition or provider response cannot be trusted."""


def _request(url: str, *, token: str | None = None, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return error.code, payload
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise ReleaseError(f"Provider request failed: {type(error).__name__}") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def validate_run_id(value: str) -> str:
    _require(bool(re.fullmatch(r"[0-9]+", value)), "CI run ID must contain digits only")
    return value


def validate_source_run(run: dict[str, Any], repository: str, sha: str, comparison: str) -> None:
    _require(bool(re.fullmatch(r"[0-9a-fA-F]{40}", sha)), "Source commit must be exactly 40 hexadecimal characters")
    _require(run.get("repository", {}).get("full_name") == repository, "CI run belongs to a different repository")
    _require(run.get("path") == CI_WORKFLOW, "CI run did not use the canonical CI workflow")
    _require(run.get("event") == "push" and run.get("head_branch") == "main", "CI run must be a push to main")
    _require(run.get("status") == "completed" and run.get("conclusion") == "success", "CI run is not completed successfully")
    _require(run.get("head_sha") == sha, "CI run SHA is invalid or inconsistent")
    _require(comparison in {"ahead", "identical"}, "CI commit is not an ancestor of current main")


def validate_artifact(artifact: dict[str, Any], sha: str) -> None:
    _require(artifact.get("name") == f"frontend-{sha}", "Successful CI run has no exact-SHA frontend artifact")
    _require(artifact.get("expired") is False and isinstance(artifact.get("id"), int), "Frontend artifact is expired or malformed")


def validate_environment(environment: dict[str, Any], policies: list[dict[str, Any]]) -> None:
    rules = environment.get("protection_rules")
    _require(
        isinstance(rules, list)
        and any(
            rule.get("type") == "required_reviewers"
            and isinstance(rule.get("reviewers"), list)
            and bool(rule["reviewers"])
            for rule in rules
            if isinstance(rule, dict)
        ),
        "Production environment has no configured required reviewers",
    )
    policy = environment.get("deployment_branch_policy")
    _require(isinstance(policy, dict) and policy.get("custom_branch_policies") is True, "Production environment is not restricted to a custom branch allowlist")
    _require(isinstance(policies, list) and len(policies) == 1 and policies[0].get("name") == "main", "Production environment must allow only main")


def _github_json(path: str, token: str) -> Any:
    status, payload = _request(f"{GITHUB_API}{path}", token=token)
    _require(status == 200 and isinstance(payload, (dict, list)), f"GitHub read failed with HTTP {status}")
    return payload


def preflight() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = validate_run_id(os.environ.get("CI_RUN_ID", ""))
    token = os.environ.get("RELEASE_PREFLIGHT_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    _require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)) and bool(token), "Repository or read-only GitHub token is missing")
    _require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Manual releases may run only from main")
    root = f"/repos/{repository}"
    run = _github_json(f"{root}/actions/runs/{run_id}", token)
    sha = run.get("head_sha", "") if isinstance(run, dict) else ""
    _require(bool(re.fullmatch(r"[0-9a-fA-F]{40}", sha)), "CI run has no valid source SHA")
    comparison = _github_json(f"{root}/compare/{sha}...main", token)
    _require(isinstance(comparison, dict), "GitHub commit comparison is malformed")
    validate_source_run(run, repository, sha, comparison.get("status", ""))
    artifacts = _github_json(f"{root}/actions/runs/{run_id}/artifacts", token)
    _require(isinstance(artifacts, dict) and isinstance(artifacts.get("artifacts"), list), "GitHub artifact listing is malformed")
    matches = [item for item in artifacts["artifacts"] if isinstance(item, dict) and item.get("name") == f"frontend-{sha}"]
    _require(len(matches) == 1, "CI run must contain exactly one matching frontend artifact")
    validate_artifact(matches[0], sha)
    environment = _github_json(f"{root}/environments/production", token)
    policy_data = _github_json(f"{root}/environments/production/deployment-branch-policies", token)
    _require(isinstance(policy_data, dict) and isinstance(policy_data.get("branch_policies"), list), "Production branch policy response is malformed")
    validate_environment(environment, policy_data["branch_policies"])
    return sha


class RenderClient:
    """Small provider boundary with fixed trusted API origin."""

    def __init__(self, token: str):
        _require(bool(token), "Render API key is missing")
        self.token = token

    def _call(self, path: str, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return _request(f"{RENDER_API}{path}", token=self.token, method=method, body=body)

    def get_service(self, service_id: str) -> dict[str, Any]:
        status, payload = self._call(f"/services/{quote(service_id, safe='')}")
        _require(status == 200 and isinstance(payload, dict), f"Render service read failed with HTTP {status}")
        return payload

    def post_deploy(self, service_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, response = self._call(f"/services/{quote(service_id, safe='')}/deploys", "POST", payload)
        return status, response if isinstance(response, dict) else {}

    def get_deploy(self, service_id: str, deploy_id: str) -> tuple[int, dict[str, Any]]:
        status, response = self._call(f"/services/{quote(service_id, safe='')}/deploys/{quote(deploy_id, safe='')}")
        return status, response if isinstance(response, dict) else {}


def deploy_render(api: Any, service_id: str, sha: str, repository: str, branch: str) -> str:
    _require(bool(re.fullmatch(r"[0-9a-fA-F]{40}", sha)), "Render commit SHA is malformed")
    service = api.get_service(service_id)
    repo = service.get("repo")
    expected_repo = f"https://github.com/{repository}"
    _require(repo == expected_repo and service.get("branch") == branch, "Render service repository or branch does not match the release source")
    _require(service.get("autoDeploy") in ("no", "off", False), "Render automatic deployments are not disabled")
    status, response = api.post_deploy(service_id, {"commitId": sha, "clearCache": "do_not_clear"})
    _require(status == 201, f"Render deploy creation was not confirmed (HTTP {status}); reconcile before retrying")
    _require(isinstance(response.get("id"), str) and response.get("id"), "Render create response has no deploy identity; reconcile before retrying")
    commit = response.get("commit")
    _require(isinstance(commit, dict) and commit.get("id") == sha, "Render create response commit differs from requested SHA; reconcile before retrying")
    return response["id"]


def poll_render_deploy(api: Any, service_id: str, deploy_id: str, sha: str, *, max_polls: int = 60, interval: float = 10, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    for attempt in range(max_polls):
        status_code, deploy = api.get_deploy(service_id, deploy_id)
        _require(status_code == 200 and isinstance(deploy, dict), f"Render deploy read failed with HTTP {status_code}")
        _require(deploy.get("id") == deploy_id, "Render returned a different deploy identity")
        commit = deploy.get("commit")
        _require(isinstance(commit, dict) and commit.get("id") == sha, "Render deploy commit differs from selected SHA")
        state = deploy.get("status")
        if state == "live":
            return deploy
        _require(state in PENDING_STATUSES, f"Render deploy entered terminal or unknown state: {state!r}")
        if attempt + 1 < max_polls:
            sleep(interval)
    raise ReleaseError("Render deploy did not become live before the polling deadline")


def check_readiness(url: str, get_status: Callable[[str], int] | None = None) -> None:
    parsed = urlparse(url)
    _require(parsed.scheme == "https" and parsed.netloc == "wikigraph.onrender.com" and parsed.path == "/readyz" and not parsed.query and not parsed.fragment, "Readiness URL is not the fixed trusted Render origin")
    if get_status is None:
        def get_status(target: str) -> int:
            try:
                with urlopen(Request(target, method="GET"), timeout=10) as response:
                    return response.status
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                raise ReleaseError(f"Render readiness request failed: {type(error).__name__}") from error
    _require(get_status(url) == 200, "Render /readyz did not return HTTP 200")


def deploy_backend() -> None:
    service_id = os.environ.get("RENDER_SERVICE_ID", "")
    sha = os.environ.get("RELEASE_SHA", "")
    _require(bool(service_id), "Render service ID is missing")
    client = RenderClient(os.environ.get("RENDER_API_KEY", ""))
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    deploy_id = deploy_render(client, service_id, sha, repository, "main")
    print(f"Render deploy accepted: {deploy_id}")
    poll_render_deploy(client, service_id, deploy_id, sha)
    check_readiness(f"{RENDER_ORIGIN}/readyz")
    print("Render deploy is live and /readyz is healthy")


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"preflight", "render"}:
            raise ReleaseError("Usage: ci_cd.py preflight|render")
        if sys.argv[1] == "preflight":
            print(preflight())
        else:
            deploy_backend()
        return 0
    except ReleaseError as error:
        print(f"Release stopped safely: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
