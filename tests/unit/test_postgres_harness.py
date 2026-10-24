from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.support.postgres import (
    PostgresHarness,
    load_postgres_harness,
    run_postgres_admin_sql,
    start_postgres_admin_sql,
)


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "test_postgres.py"


@pytest.fixture
def podman(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Provide a controlled Podman process seam and a fake loopback PostgREST."""
    log = tmp_path / "podman.jsonl"
    server = tmp_path / "server.py"
    server.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import sys\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        " def log_message(self, *args): pass\n"
        "HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    executable = tmp_path / "podman"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys, time\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['FAKE_PODMAN_LOG'], 'a', encoding='utf-8') as f: f.write(json.dumps(args)+'\\n')\n"
        "fail=os.environ.get('FAKE_PODMAN_FAIL', '')\n"
        "if fail and (fail in args or (fail == 'image-exists' and args[:2] == ['image','exists'])): sys.exit(42)\n"
        "if args[:1] == ['run']:\n"
        " name=args[args.index('--name')+1]\n"
        " if any(value.endswith('postgrest/postgrest:v12.2.3') for value in args):\n"
        "  mapping=args[args.index('-p')+1]; port=mapping.split(':')[1]\n"
        "  proc=subprocess.Popen([sys.executable, os.environ['FAKE_POSTGREST_SERVER'], port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "  open(os.path.join(os.environ['FAKE_PODMAN_STATE'], name), 'w').write(str(proc.pid))\n"
        " print(name)\n"
        "elif args[:1] == ['rm']:\n"
        " for name in args[2:]:\n"
        "  path=os.path.join(os.environ['FAKE_PODMAN_STATE'], name)\n"
        "  if os.path.exists(path):\n"
        "   pid=int(open(path).read()); subprocess.run(['kill', str(pid)], check=False); os.unlink(path)\n"
        "elif args[:2] == ['network', 'create'] or args[:2] == ['network', 'rm']:\n"
        " pass\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    state = tmp_path / "podman-state"
    state.mkdir()
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_PODMAN_LOG": str(log),
        "FAKE_POSTGREST_SERVER": str(server),
        "FAKE_PODMAN_STATE": str(state),
    }
    return env, log


def invoke(env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *argv],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def commands(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_requires_explicit_child_argv_without_creating_resources(podman: tuple[dict[str, str], Path]) -> None:
    env, log = podman
    result = invoke(env)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "usage: test_postgres.py -- <command> [args...]\n"
    assert not log.exists()


def test_runs_child_argv_verbatim_and_cleans_only_fresh_owned_resources(
    podman: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = podman
    sentinel = tmp_path / "unrelated-resource"
    sentinel.write_text("leave unchanged", encoding="utf-8")
    env.update(
        {
            "WIKIGRAPH_SUPABASE_URL": "https://production.invalid",
            "SUPABASE_URL": "https://production.invalid",
            "DATABASE_URL": "postgres://production.invalid/db",
            "WIKIGRAPH_TICKET28_POSTGREST_URL": "https://production.invalid",
            "PGHOST": "production.invalid",
        }
    )
    code = (
        "import json,os,sys; from tests.support.postgres import load_postgres_harness; "
        "config=load_postgres_harness(); print(json.dumps({'args':sys.argv[1:],"
        "'url':os.environ.get('WIKIGRAPH_TICKET28_POSTGREST_URL'),"
        "'config_url':config.url if config else None,"
        "'run':os.environ.get('WIKIGRAPH_POSTGRES_HARNESS_RUN_ID'),"
        "'state':os.environ.get('WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR'),"
        "'unsafe':[k for k in ('DATABASE_URL','SUPABASE_URL','WIKIGRAPH_SUPABASE_URL','PGHOST') "
        "if k in os.environ]}))"
    )
    result = invoke(env, "--", sys.executable, "-c", code, "two words", "$(not-a-shell)")

    assert result.returncode == 0
    child = json.loads(result.stdout)
    assert child["args"] == ["two words", "$(not-a-shell)"]
    assert child["url"].startswith("http://127.0.0.1:")
    assert child["config_url"] == child["url"]
    assert child["run"]
    assert not Path(child["state"], "owner.json").exists()
    assert child["unsafe"] == []
    assert result.stderr == ""
    assert sentinel.read_text(encoding="utf-8") == "leave unchanged"
    logged = commands(log)
    assert any(command[:2] == ["network", "create"] for command in logged)
    assert any(command[:1] == ["rm"] for command in logged)
    assert any(command[:2] == ["network", "rm"] for command in logged)


def test_preserves_child_output_and_exit_status_while_cleaning_owned_resources(
    podman: tuple[dict[str, str], Path]
) -> None:
    env, log = podman
    result = invoke(env, "--", sys.executable, "-c", "import sys; print('child-out'); print('child-err', file=sys.stderr); sys.exit(23)")

    assert result.returncode == 23
    assert result.stdout == "child-out\n"
    assert result.stderr == "child-err\n"
    assert any(command[:1] == ["rm"] for command in commands(log))


def test_setup_failure_cleans_only_successfully_created_resources(
    podman: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = podman
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    env["FAKE_PODMAN_FAIL"] = "psql"
    result = invoke(env, "--", sys.executable, "-c", "print('must-not-run')")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "PostgreSQL harness setup failed" in result.stderr
    assert "production.invalid" not in result.stderr
    assert "wikigraph-local-auth" not in result.stderr
    logged = commands(log)
    assert any(command[:1] == ["rm"] for command in logged)
    assert any(command[:2] == ["network", "rm"] for command in logged)
    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_missing_cached_image_fails_before_creating_resources(
    podman: tuple[dict[str, str], Path]
) -> None:
    env, log = podman
    env["FAKE_PODMAN_FAIL"] = "image-exists"
    result = invoke(env, "--", sys.executable, "-c", "print('must-not-run')")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "PostgreSQL harness setup failed: Podman operation failed\n"
    logged = commands(log)
    assert [command[:2] for command in logged] == [["image", "exists"]]


def test_external_database_url_is_never_used_as_the_child_target(
    podman: tuple[dict[str, str], Path]
) -> None:
    env, _ = podman
    env["DATABASE_URL"] = "postgres://attacker.example/existing"
    env["WIKIGRAPH_TICKET28_POSTGREST_URL"] = "https://production.invalid"
    result = invoke(
        env,
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['WIKIGRAPH_TICKET28_POSTGREST_URL'])",
    )

    assert result.returncode == 0
    assert result.stdout.startswith("http://127.0.0.1:")
    assert "production.invalid" not in result.stdout
    assert result.stderr == ""


@pytest.fixture
def owned_postgres_context(monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, bytes]:
    """Create only a fresh excluded receipt fixture for the public config seam."""
    environment_names = (
        "WIKIGRAPH_POSTGRES_HARNESS_RUN_ID",
        "WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR",
        "WIKIGRAPH_TICKET28_POSTGREST_URL",
        "WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT",
        "WIKIGRAPH_TICKET28_ANON_JWT",
        "WIKIGRAPH_TICKET28_AUTHENTICATED_JWT",
    )
    for name in environment_names:
        monkeypatch.delenv(name, raising=False)

    run_id = uuid.uuid4().hex
    root = ROOT / "odd" / "test-postgres"
    state_dir = root / run_id
    state_dir.mkdir(parents=True, exist_ok=False)
    url = "http://127.0.0.1:54321"
    owner_path = state_dir / "owner.json"
    try:
        owner_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "url": url,
                    "network": f"wikigraph-test-{run_id}-net",
                    "postgres": f"wikigraph-test-{run_id}-postgres",
                    "postgrest": f"wikigraph-test-{run_id}-postgrest",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("WIKIGRAPH_POSTGRES_HARNESS_RUN_ID", run_id)
        monkeypatch.setenv("WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR", str(state_dir))
        monkeypatch.setenv("WIKIGRAPH_TICKET28_POSTGREST_URL", url)
        monkeypatch.setenv("WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT", "fake-service-token")
        monkeypatch.setenv("WIKIGRAPH_TICKET28_ANON_JWT", "fake-anon-token")
        monkeypatch.setenv("WIKIGRAPH_TICKET28_AUTHENTICATED_JWT", "fake-authenticated-token")
        yield run_id, owner_path, url.encode()
    finally:
        if owner_path.exists():
            owner_path.unlink()
        state_dir.rmdir()


def test_admin_sql_refuses_to_run_without_owned_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKIGRAPH_POSTGRES_HARNESS_RUN_ID", raising=False)

    with pytest.raises(RuntimeError, match="requires the owned PostgreSQL harness"):
        run_postgres_admin_sql("select 1")

    with pytest.raises(RuntimeError, match="requires the owned PostgreSQL harness"):
        start_postgres_admin_sql("select 1")


def test_admin_sql_targets_only_container_from_validated_owned_receipt(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
) -> None:
    run_id, _, _ = owned_postgres_context
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0, "result\\n", "")

    monkeypatch.setattr("tests.support.postgres.subprocess.run", fake_run)

    result = run_postgres_admin_sql("select 1")

    assert result.stdout == "result\\n"
    assert observed["command"] == [
        "podman", "exec", "-i", f"wikigraph-test-{run_id}-postgres",
        "psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", "wikigraph",
    ]
    assert observed["kwargs"]["input"] == "select 1"
    assert observed["kwargs"]["check"] is True


def test_harness_credentials_are_not_in_repr() -> None:
    config = PostgresHarness(
        "http://127.0.0.1:54321", "service-secret", "anon-secret",
        "authenticated-secret", "a" * 32, ROOT / "odd" / "test-postgres",
    )

    assert "secret" not in repr(config)


def test_missing_harness_context_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKIGRAPH_POSTGRES_HARNESS_RUN_ID", raising=False)
    monkeypatch.setenv("WIKIGRAPH_TICKET28_POSTGREST_URL", "https://external.invalid")

    assert load_postgres_harness() is None


def test_invalid_run_id_is_rejected_without_changing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
) -> None:
    _, receipt, _ = owned_postgres_context
    original = receipt.read_bytes()
    monkeypatch.setenv("WIKIGRAPH_POSTGRES_HARNESS_RUN_ID", "not-a-run-id")

    with pytest.raises(RuntimeError, match="invalid owned PostgreSQL harness run identifier"):
        load_postgres_harness()

    assert receipt.read_bytes() == original


def test_wrong_state_directory_is_rejected_without_changing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
) -> None:
    _, receipt, _ = owned_postgres_context
    original = receipt.read_bytes()
    monkeypatch.setenv("WIKIGRAPH_POSTGRES_HARNESS_STATE_DIR", str(receipt.parent.parent))

    with pytest.raises(RuntimeError, match="state is not the expected owned run directory"):
        load_postgres_harness()

    assert receipt.read_bytes() == original


def test_environment_url_must_match_owned_receipt(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
) -> None:
    _, receipt, _ = owned_postgres_context
    original = receipt.read_bytes()
    monkeypatch.setenv("WIKIGRAPH_TICKET28_POSTGREST_URL", "http://127.0.0.1:54322")

    with pytest.raises(RuntimeError, match="does not match its owned local receipt"):
        load_postgres_harness()

    assert receipt.read_bytes() == original


@pytest.mark.parametrize(
    "url",
    (
        "http://database.example:54321",
        "http://192.0.2.10:54321",
        "https://127.0.0.1:54321",
        "http://user:password@127.0.0.1:54321",
    ),
    ids=("external-host", "non-loopback-address", "https", "url-credentials"),
)
def test_matching_but_unsafe_owned_urls_are_rejected_without_receipt_changes(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
    url: str,
) -> None:
    run_id, receipt, _ = owned_postgres_context
    owner = json.loads(receipt.read_text(encoding="utf-8"))
    owner["url"] = url
    receipt.write_text(json.dumps(owner), encoding="utf-8")
    original = receipt.read_bytes()
    monkeypatch.setenv("WIKIGRAPH_TICKET28_POSTGREST_URL", url)

    with pytest.raises(RuntimeError, match="does not match its owned local receipt"):
        load_postgres_harness()

    assert receipt.read_bytes() == original


@pytest.mark.parametrize(
    "missing_role_variable",
    (
        "WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT",
        "WIKIGRAPH_TICKET28_ANON_JWT",
        "WIKIGRAPH_TICKET28_AUTHENTICATED_JWT",
    ),
)
def test_each_missing_role_jwt_is_rejected_without_changing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    owned_postgres_context: tuple[str, Path, bytes],
    missing_role_variable: str,
) -> None:
    _, receipt, _ = owned_postgres_context
    original = receipt.read_bytes()
    monkeypatch.delenv(missing_role_variable)

    with pytest.raises(RuntimeError, match="role credentials are incomplete"):
        load_postgres_harness()

    assert receipt.read_bytes() == original


def test_valid_owned_loopback_receipt_is_accepted(
    owned_postgres_context: tuple[str, Path, bytes],
) -> None:
    run_id, receipt, url_bytes = owned_postgres_context
    original = receipt.read_bytes()

    config = load_postgres_harness()

    assert config is not None
    assert config.run_id == run_id
    assert config.url == url_bytes.decode()
    assert config.service_role_jwt == "fake-service-token"
    assert config.anon_jwt == "fake-anon-token"
    assert config.authenticated_jwt == "fake-authenticated-token"
    assert receipt.read_bytes() == original
