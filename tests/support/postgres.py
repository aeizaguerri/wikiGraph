"""Validated configuration for the owned local PostgreSQL integration harness."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class PostgresHarness:
    url: str
    service_role_jwt: str
    anon_jwt: str
    authenticated_jwt: str
    run_id: str
    state_dir: Path


def load_postgres_harness() -> PostgresHarness | None:
    """Return owned harness credentials, or None for ordinary non-harness pytest."""
    run_id = os.environ.get("WIKIGRAPH_POSTGRES_HARNESS_RUN_ID")
    if run_id is None:
        return None
    if not _RUN_ID.fullmatch(run_id):
        raise RuntimeError("invalid owned PostgreSQL harness run identifier")

    expected_dir = PROJECT_ROOT / "odd" / "test-postgres" / run_id
    state_value = os.environ.get("WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR")
    if state_value is None or Path(state_value) != expected_dir:
        raise RuntimeError("PostgreSQL harness state is not the expected owned run directory")
    if expected_dir.is_symlink() or not expected_dir.is_dir():
        raise RuntimeError("owned PostgreSQL harness state is unavailable")

    owner_path = expected_dir / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("owned PostgreSQL harness receipt is unavailable") from error

    url = os.environ.get("WIKIGRAPH_TICKET28_POSTGREST_URL")
    parsed = urlsplit(url or "")
    if (
        owner != {
            "run_id": run_id,
            "url": url,
            "network": f"wikigraph-test-{run_id}-net",
            "postgres": f"wikigraph-test-{run_id}-postgres",
            "postgrest": f"wikigraph-test-{run_id}-postgrest",
        }
        or owner.get("url") != url
        or parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("PostgreSQL harness target does not match its owned local receipt")

    service = os.environ.get("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT")
    anon = os.environ.get("WIKIGRAPH_TICKET28_ANON_JWT")
    authenticated = os.environ.get("WIKIGRAPH_TICKET28_AUTHENTICATED_JWT")
    if not all((service, anon, authenticated)):
        raise RuntimeError("owned PostgreSQL harness role credentials are incomplete")
    return PostgresHarness(url, service, anon, authenticated, run_id, expected_dir)
