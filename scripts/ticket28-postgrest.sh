#!/usr/bin/env bash
set -euo pipefail

# Disposable, local-only role-shaped acceptance harness for ticket 28. It
# writes credentials to a mode-600 file and never prints JWT values.
network=ticket28-net
postgres=ticket28-postgres
postgrest=ticket28-postgrest
secret=${WIKIGRAPH_TICKET28_LOCAL_SECRET:-ticket28-local-only-secret-32-bytes!!}
env_file=${WIKIGRAPH_TICKET28_ENV_FILE:-/tmp/wikigraph-ticket28.env}

token() {
  role="$1" SECRET="$secret" python - <<'PY'
import base64, hashlib, hmac, json, os

def enc(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

header = enc(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
payload = enc(json.dumps({"role": os.environ["role"], "iss": "supabase", "exp": 4102444800}, separators=(",", ":")).encode())
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
    podman exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d wikigraph <<'SQL' >/dev/null
create role authenticator noinherit login password 'authenticator';
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
grant anon, authenticated, service_role to authenticator;
SQL
    for migration in "$(dirname "$0")"/../supabase/migrations/*.sql; do
      podman exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d wikigraph \
        < "$migration" >/dev/null
    done
    podman run -d --name "$postgrest" --network "$network" -p 127.0.0.1:33028:3000 \
      -e PGRST_DB_URI=postgres://authenticator:authenticator@ticket28-postgres:5432/wikigraph \
      -e PGRST_DB_SCHEMA=public -e PGRST_DB_ANON_ROLE=anon \
      -e PGRST_JWT_SECRET="$secret" postgrest/postgrest:v12.2.3 >/dev/null
    until [[ "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:33028/crawl_runs)" != "000" ]]; do sleep 1; done
    umask 077
    {
      printf 'WIKIGRAPH_TICKET28_POSTGREST_URL=http://127.0.0.1:33028\n'
      printf 'WIKIGRAPH_TICKET28_SERVICE_ROLE_JWT=%s\n' "$(role=service_role token service_role)"
      printf 'WIKIGRAPH_TICKET28_ANON_JWT=%s\n' "$(role=anon token anon)"
      printf 'WIKIGRAPH_TICKET28_AUTHENTICATED_JWT=%s\n' "$(role=authenticated token authenticated)"
    } > "$env_file"
    printf 'Harness ready; source credentials from %s without printing its contents.\n' "$env_file"
    ;;
  stop)
    podman rm -f "$postgrest" "$postgres" >/dev/null 2>&1 || true
    podman network rm "$network" >/dev/null 2>&1 || true
    rm -f "$env_file"
    printf 'Harness stopped.\n'
    ;;
  *)
    printf 'usage: %s start|stop\n' "$0" >&2
    exit 2
    ;;
esac
