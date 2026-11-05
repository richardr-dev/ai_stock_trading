#!/usr/bin/env bash
# Expose the jarvis_stocks Postgres DB to your Tailscale network ONLY (password required).
#  - listen_addresses='*'  (so the Tailscale interface can connect)
#  - pg_hba: only 100.64.0.0/10 + fd7a:115c:a1e0::/48 (Tailscale ranges) may log in, as role 'jarvis'
#  - everything else on your LAN has no matching hba rule -> rejected
# Re-runnable. Backups: postgresql.conf.bak-jarvis / pg_hba.conf.bak-jarvis
set -euo pipefail
cd "$(dirname "$0")/.."

PGDATA=$(psql -d postgres -Atc "show data_directory")
CONF="$PGDATA/postgresql.conf"
HBA="$PGDATA/pg_hba.conf"
DB=jarvis_stocks
ROLE=jarvis

[[ -f "$CONF.bak-jarvis" ]] || cp "$CONF" "$CONF.bak-jarvis"
[[ -f "$HBA.bak-jarvis"  ]] || cp "$HBA"  "$HBA.bak-jarvis"

# 1. password for the remote role (kept in .env, which is gitignored)
if grep -q '^JARVIS_DB_PASSWORD=' .env 2>/dev/null; then
  PW=$(grep '^JARVIS_DB_PASSWORD=' .env | cut -d= -f2-)
else
  PW=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)
  echo "JARVIS_DB_PASSWORD=$PW" >> .env
fi

psql -d postgres -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$ROLE') THEN
    CREATE ROLE $ROLE LOGIN PASSWORD '$PW';
  ELSE
    ALTER ROLE $ROLE WITH LOGIN PASSWORD '$PW';
  END IF;
END \$\$;
GRANT CONNECT ON DATABASE $DB TO $ROLE;
SQL
psql -d "$DB" -v ON_ERROR_STOP=1 -q <<SQL
GRANT USAGE ON SCHEMA market TO $ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA market TO $ROLE;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA market TO $ROLE;
ALTER DEFAULT PRIVILEGES IN SCHEMA market GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $ROLE;
ALTER DEFAULT PRIVILEGES IN SCHEMA market GRANT USAGE, SELECT ON SEQUENCES TO $ROLE;
SQL

# 2. listen on all interfaces (auth is restricted by pg_hba below)
if ! grep -qE "^listen_addresses\s*=\s*'\*'" "$CONF"; then
  echo "listen_addresses = '*'   # jarvis: Tailscale access (restricted in pg_hba.conf)" >> "$CONF"
fi

# 3. Tailscale-only hba rules
if ! grep -q "# jarvis-tailscale" "$HBA"; then
  cat >> "$HBA" <<EOF
# jarvis-tailscale: only Tailscale CGNAT/ULA ranges, only the jarvis DB/role, password required
host    $DB    $ROLE    100.64.0.0/10          scram-sha-256   # jarvis-tailscale
host    $DB    $ROLE    fd7a:115c:a1e0::/48    scram-sha-256   # jarvis-tailscale
EOF
fi

brew services restart postgresql@14 >/dev/null
for i in {1..20}; do pg_isready -q && break; sleep 0.5; done

TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
TSIP=$("$TS" ip -4 2>/dev/null | head -1 || true)
TSNAME=$("$TS" status --json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
echo
echo "Postgres is reachable over Tailscale as role '$ROLE' (password in .env: JARVIS_DB_PASSWORD)."
if [[ -n "$TSIP" ]]; then
  echo "  psql \"postgresql://$ROLE:<password>@${TSNAME:-$TSIP}:5432/$DB\""
else
  echo "  Tailscale is not running right now — start it, then connect to <this-mac>.<tailnet>.ts.net:5432"
fi
