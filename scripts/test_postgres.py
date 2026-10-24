#!/usr/bin/env python3
"""Run a child command against a fresh, local PostgreSQL/PostgREST harness."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "docker.io/library/postgres:16-alpine"
POSTGREST_IMAGE = "docker.io/postgrest/postgrest:v12.2.3"
STATE_ROOT = ROOT / "odd" / "test-postgres"


class HarnessError(Exception):
    pass


def _command(args: Sequence[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["podman", *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise HarnessError("Podman could not be started") from error
    if result.returncode != 0:
        raise HarnessError("Podman operation failed")
    return result


def _jwt(secret: str, role: str) -> str:
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = encode(
        json.dumps(
            {"role": role, "iss": "supabase", "exp": int(time.time()) + 3600},
            separators=(",", ":"),
        ).encode()
    )
    unsigned = f"{header}.{payload}"
    signature = hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
    return f"{unsigned}.{encode(signature)}"


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_for_postgres(container: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            _command(["exec", container, "pg_isready", "-U", "postgres", "-d", "wikigraph"])
            return
        except HarnessError:
            time.sleep(0.5)
    raise HarnessError("PostgreSQL did not become ready")


def _wait_for_postgrest(url: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with opener.open(f"{url}/", timeout=1):
                return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.5)
    raise HarnessError("PostgREST did not become ready")


def _verify_owned_target(
    state_dir: Path, run_id: str, url: str, network: str, postgres: str, postgrest: str
) -> None:
    expected_dir = STATE_ROOT / run_id
    if state_dir != expected_dir or state_dir.is_symlink() or not state_dir.is_dir():
        raise HarnessError("Owned PostgreSQL harness state could not be verified")
    try:
        owner = json.loads(state_dir.joinpath("owner.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError("Owned PostgreSQL harness receipt could not be verified") from error
    if owner != {
        "run_id": run_id,
        "url": url,
        "network": network,
        "postgres": postgres,
        "postgrest": postgrest,
    }:
        raise HarnessError("PostgreSQL target does not match its owned run receipt")


def _provision_database(container: str, migrations: list[Path], auth_password: str) -> None:
    roles = f"""CREATE ROLE authenticator NOINHERIT LOGIN PASSWORD '{auth_password}';
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
GRANT anon, authenticated, service_role TO authenticator;
"""
    _command(
        ["exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
        input_text=roles,
    )
    for migration in migrations:
        try:
            sql = migration.read_text(encoding="utf-8")
        except OSError as error:
            raise HarnessError("A database migration could not be read") from error
        _command(
            ["exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "wikigraph"],
            input_text=sql,
        )


def _child_environment(url: str, run_id: str, state_dir: Path, secret: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WIKIGRAPH_", "SUPABASE_", "PG"))
        and key not in {"DATABASE_URL"}
    }
    environment.update(
        {
            "WIKIGRAPH_TICKET28_POSTGREST_URL": url,
            "WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT": _jwt(secret, "service_role"),
            "WIKIGRAPH_TICKET28_ANON_JWT": _jwt(secret, "anon"),
            "WIKIGRAPH_TICKET28_AUTHENTICATED_JWT": _jwt(secret, "authenticated"),
            "WIKIGRAPH_POSTGRES_HARNESS_RUN_ID": run_id,
            "WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR": str(state_dir),
        }
    )
    return environment


def _cleanup(containers: list[str], network: str) -> bool:
    clean = True
    if containers:
        try:
            _command(["rm", "--force", *containers])
        except HarnessError:
            clean = False
    try:
        _command(["network", "rm", network])
    except HarnessError:
        clean = False
    return clean


def _run(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--" or not argv[1]:
        print("usage: test_postgres.py -- <command> [args...]", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex
    network = f"wikigraph-test-{run_id}-net"
    postgres = f"wikigraph-test-{run_id}-postgres"
    postgrest = f"wikigraph-test-{run_id}-postgrest"
    state_dir = STATE_ROOT / run_id
    containers: list[str] = []
    network_created = False
    state_created = False
    result_code = 1
    error: str | None = None
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir()
        state_created = True
        port = _available_port()
        url = f"http://127.0.0.1:{port}"
        secret = secrets.token_urlsafe(48)
        admin_password = secrets.token_urlsafe(32)
        auth_password = secrets.token_urlsafe(32)
        state_dir.joinpath("owner.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "url": url,
                    "network": network,
                    "postgres": postgres,
                    "postgrest": postgrest,
                }
            ),
            encoding="utf-8",
        )
        _verify_owned_target(state_dir, run_id, url, network, postgres, postgrest)
        _command(["image", "exists", POSTGRES_IMAGE])
        _command(["image", "exists", POSTGREST_IMAGE])
        _command(["network", "create", "--label", f"org.wikigraph.harness={run_id}", network])
        network_created = True
        _command(
            [
                "run", "--pull=never", "--detach", "--name", postgres, "--network", network,
                "--label", f"org.wikigraph.harness={run_id}",
                "--env", f"POSTGRES_PASSWORD={admin_password}",
                "--env", "POSTGRES_DB=wikigraph", POSTGRES_IMAGE,
            ]
        )
        containers.append(postgres)
        _wait_for_postgres(postgres)
        _verify_owned_target(state_dir, run_id, url, network, postgres, postgrest)
        _provision_database(
            postgres, sorted((ROOT / "supabase" / "migrations").glob("*.sql")), auth_password
        )
        _command(
            [
                "run", "--pull=never", "--detach", "--name", postgrest, "--network", network,
                "--label", f"org.wikigraph.harness={run_id}",
                "-p", f"127.0.0.1:{port}:3000",
                "--env", f"PGRST_DB_URI=postgres://authenticator:{auth_password}@"
                f"{postgres}:5432/wikigraph",
                "--env", "PGRST_DB_SCHEMA=public", "--env", "PGRST_DB_ANON_ROLE=anon",
                "--env", f"PGRST_JWT_SECRET={secret}", POSTGREST_IMAGE,
            ]
        )
        containers.append(postgrest)
        _wait_for_postgrest(url)
        _verify_owned_target(state_dir, run_id, url, network, postgres, postgrest)
        child_env = _child_environment(url, run_id, state_dir, secret)
        try:
            child = subprocess.run(argv[1:], cwd=ROOT, env=child_env, check=False)
            result_code = child.returncode
        except OSError:
            error = "Child command could not be started"
    except (HarnessError, OSError) as exception:
        error = str(exception) if isinstance(exception, HarnessError) else "Harness state could not be created"
    finally:
        resources_clean = True
        if network_created:
            try:
                _verify_owned_target(state_dir, run_id, url, network, postgres, postgrest)
            except (HarnessError, UnboundLocalError):
                resources_clean = False
                error = error or "Owned PostgreSQL harness receipt could not be verified for cleanup"
            else:
                resources_clean = _cleanup(containers, network)
            if not resources_clean:
                error = error or "Owned PostgreSQL harness resources could not be fully cleaned"
        if state_created and resources_clean:
            try:
                shutil.rmtree(state_dir, ignore_errors=False)
            except OSError:
                error = error or "Owned PostgreSQL harness state could not be cleaned"

    if error:
        print(f"PostgreSQL harness setup failed: {error}", file=sys.stderr)
        return 1
    return result_code


def main() -> int:
    return _run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
