"""Fail-closed validation and Render release operations for the CD workflow."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class _GitHubContext:
    repository: str
    token: str = field(repr=False)

    @property
    def root(self) -> str:
        return f"/repos/{self.repository}"


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


def _github_json(path: str, token: str) -> Any:
    status, payload = _request(f"{GITHUB_API}{path}", token=token)
    _require(status == 200 and isinstance(payload, (dict, list)), f"GitHub GET {path} failed with HTTP {status}")
    return payload


def _release_context() -> _GitHubContext:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    _require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)) and bool(token), "Repository or read-only GitHub token is missing")
    _require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Manual releases may run only from main")
    return _GitHubContext(repository, token)


def _artifact_listing(run_id: str, context: _GitHubContext) -> list[Any]:
    listing = _github_json(f"{context.root}/actions/runs/{run_id}/artifacts", context.token)
    _require(isinstance(listing, dict) and isinstance(listing.get("artifacts"), list), "GitHub artifact listing is malformed")
    return listing["artifacts"]


def _matching_artifact(artifacts: list[Any], sha: str) -> list[dict[str, Any]]:
    return [item for item in artifacts if isinstance(item, dict) and item.get("name") == f"frontend-{sha}"]


def _release_identity(run_id: str, context: _GitHubContext) -> str:
    root = context.root
    repository = context.repository
    token = context.token
    run = _github_json(f"{root}/actions/runs/{run_id}", token)
    sha = run.get("head_sha", "") if isinstance(run, dict) else ""
    _require(bool(re.fullmatch(r"[0-9a-fA-F]{40}", sha)), "CI run has no valid source SHA")
    comparison = _github_json(f"{root}/compare/{sha}...main", token)
    _require(isinstance(comparison, dict), "GitHub commit comparison is malformed")
    validate_source_run(run, repository, sha, comparison.get("status", ""))
    matches = _matching_artifact(_artifact_listing(run_id, context), sha)
    _require(len(matches) == 1, "CI run must contain exactly one matching frontend artifact")
    validate_artifact(matches[0], sha)
    return sha


def _run_created_at(run: dict[str, Any]) -> datetime:
    value = run.get("created_at")
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ReleaseError("GitHub workflow run has malformed created_at ordering metadata")
    try:
        created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseError("GitHub workflow run has malformed created_at ordering metadata") from error
    _require(created_at.utcoffset() is not None, "GitHub workflow run has malformed created_at ordering metadata")
    return created_at


def preflight() -> str:
    run_id = validate_run_id(os.environ.get("CI_RUN_ID", ""))
    context = _release_context()
    return _release_identity(run_id, context)


def select_run() -> tuple[str, str]:
    context = _release_context()
    repository, token, root = context.repository, context.token, context.root
    requested = os.environ.get("CI_RUN_ID", "")
    if requested:
        run_id = validate_run_id(requested)
        return run_id, _release_identity(run_id, context)

    page_size = 100
    max_pages = 10
    candidates: dict[int, tuple[datetime, dict[str, Any]]] = {}
    workflow_file = CI_WORKFLOW.rsplit("/", 1)[-1]
    for page in range(1, max_pages + 1):
        query = f"?branch=main&event=push&status=completed&per_page={page_size}&page={page}"
        listing = _github_json(f"{root}/actions/workflows/{workflow_file}/runs{query}", token)
        _require(isinstance(listing, dict) and isinstance(listing.get("workflow_runs"), list), "GitHub workflow run listing is malformed")
        runs = listing["workflow_runs"]
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = run.get("id")
            sha = run.get("head_sha", "")
            if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0 or not isinstance(sha, str):
                continue
            try:
                validate_source_run(run, repository, sha, "ahead")
            except (ReleaseError, AttributeError):
                continue
            created_at = _run_created_at(run)
            previous = candidates.get(run_id)
            if previous is not None and previous[1] != run:
                raise ReleaseError(f"GitHub workflow run listing has conflicting metadata for run ID {run_id}")
            candidates[run_id] = (created_at, run)
        if not runs or len(runs) < page_size:
            break

    ordered = sorted(candidates.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
    for run_id, (_, run) in ordered:
        sha = run["head_sha"]
        comparison = _github_json(f"{root}/compare/{sha}...main", token)
        _require(isinstance(comparison, dict), "GitHub commit comparison is malformed")
        try:
            validate_source_run(run, repository, sha, comparison.get("status", ""))
        except ReleaseError:
            continue
        matches = _matching_artifact(_artifact_listing(str(run_id), context), sha)
        if len(matches) != 1:
            continue
        try:
            validate_artifact(matches[0], sha)
        except ReleaseError:
            continue
        return str(run_id), sha
    raise ReleaseError("No successful main CI run has a valid unexpired exact-SHA frontend artifact")


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
        if len(sys.argv) != 2 or sys.argv[1] not in {"preflight", "render", "select-run"}:
            raise ReleaseError("Usage: ci_cd.py preflight|select-run|render")
        if sys.argv[1] == "preflight":
            print(preflight())
        elif sys.argv[1] == "select-run":
            run_id, sha = select_run()
            print(run_id, sha)
        else:
            deploy_backend()
        return 0
    except ReleaseError as error:
        print(f"Release stopped safely: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
