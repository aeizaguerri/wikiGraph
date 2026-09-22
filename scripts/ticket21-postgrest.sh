#!/usr/bin/env bash
set -euo pipefail

# Disposable, local-only acceptance harness for ticket 21. It owns only these
# names and ports; the caller supplies the resulting JWT to pytest.
network=ticket21-net
postgres=ticket21-postgres
postgrest=ticket21-postgrest
secret=${WIKIGRAPH_TICKET21_LOCAL_SECRET:-ticket21-local-only-secret-32-bytes!!}

token() {
  SECRET="$secret" python - <<'PY'
import base64, hashlib, hmac, json, os

def enc(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

header = enc(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
payload = enc(json.dumps({"role": "postgres", "iss": "supabase", "exp": 4102444800}, separators=(",", ":")).encode())
unsigned = f"{header}.{payload}".encode()
signature = hmac.new(os.environ["SECRET"].encode(), unsigned, hashlib.sha256).digest()
print(f"{unsigned.decode()}.{enc(signature)}")
PY
}

case "${1:-}" in
  start)
    podman network exists "$network" || podman network create "$network" >/dev/null
    podman rm -f "$postgrest" "$postgres" >/dev/null 2>&1 || true
    podman run -d --name "$postgres" --network "$network" \
      -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=wikigraph \
      postgres:16-alpine >/dev/null
    until podman exec "$postgres" pg_isready -U postgres -d wikigraph >/dev/null 2>&1; do sleep 1; done
    for migration in "$(dirname "$0")"/../supabase/migrations/*.sql; do
      podman exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d wikigraph \
        < "$migration" >/dev/null
    done
    podman run -d --name "$postgrest" --network "$network" -p 127.0.0.1:33002:3000 \
      -e PGRST_DB_URI=postgres://postgres:postgres@ticket21-postgres:5432/wikigraph \
      -e PGRST_DB_SCHEMA=public -e PGRST_DB_ANON_ROLE=postgres \
      -e PGRST_JWT_SECRET="$secret" postgrest/postgrest:v12.2.3 >/dev/null
    until curl --fail --silent http://127.0.0.1:33002/crawl_runs >/dev/null; do sleep 1; done
    printf 'export WIKIGRAPH_TICKET21_POSTGREST_URL=http://127.0.0.1:33002\n'
    printf 'export WIKIGRAPH_TICKET21_POSTGREST_KEY=%s\n' "$(token)"
    ;;
  stop)
    podman rm -f "$postgrest" "$postgres" >/dev/null 2>&1 || true
    podman network rm "$network" >/dev/null 2>&1 || true
    ;;
  *)
    printf 'usage: %s start|stop\n' "$0" >&2
    exit 2
    ;;
esac
