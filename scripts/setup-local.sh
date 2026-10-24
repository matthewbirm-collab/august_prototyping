#!/usr/bin/env bash
# Install and start local Postgres (with TimescaleDB) and Redis through Homebrew,
# then create the dev and test databases. Safe to rerun.
set -euo pipefail

DATABASES=(propline propline_test)

if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

echo "==> Installing TimescaleDB and Redis"
brew tap timescale/tap >/dev/null
brew list timescaledb >/dev/null 2>&1 || brew install timescale/tap/timescaledb
brew list redis >/dev/null 2>&1 || brew install redis

# Use whichever Postgres version the TimescaleDB formula is built against. The tap lists it
# only as a build dependency, so install it explicitly to keep `brew autoremove` from removing it.
PG_FORMULA=$("$(dirname "$0")/pg-formula.sh")
if [[ -z "$PG_FORMULA" ]]; then
  echo "Could not find the Postgres version TimescaleDB is built for." >&2
  exit 1
fi
brew list "$PG_FORMULA" >/dev/null 2>&1 || brew install "$PG_FORMULA"
PG_PREFIX=$(brew --prefix "$PG_FORMULA")
export PATH="$PG_PREFIX/bin:$PATH"
echo "==> Using $PG_FORMULA"

# The tap installs the extension files separately; move them into Postgres if missing.
if [[ ! -f "$(pg_config --sharedir)/extension/timescaledb.control" ]]; then
  echo "==> Installing the TimescaleDB extension into $PG_FORMULA"
  "$(brew --prefix timescaledb)/bin/timescaledb_move.sh"
fi

echo "==> Starting $PG_FORMULA"
brew services start "$PG_FORMULA" >/dev/null
for _ in $(seq 1 30); do
  pg_isready -q -h localhost && break
  sleep 1
done
pg_isready -q -h localhost || { echo "Postgres did not start." >&2; exit 1; }

# TimescaleDB must be preloaded; timescaledb-tune sets this and sensible memory settings.
if [[ "$(psql -h localhost -d postgres -Atc 'SHOW shared_preload_libraries')" != *timescaledb* ]]; then
  echo "==> Configuring Postgres for TimescaleDB"
  CONF=$(psql -h localhost -d postgres -Atc 'SHOW config_file')
  "$(brew --prefix timescaledb-tools)/bin/timescaledb-tune" --quiet --yes --conf-path="$CONF" \
    --pg-config="$PG_PREFIX/bin/pg_config"
  brew services restart "$PG_FORMULA" >/dev/null
  for _ in $(seq 1 30); do
    pg_isready -q -h localhost && break
    sleep 1
  done
fi

for db in "${DATABASES[@]}"; do
  if ! psql -h localhost -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    echo "==> Creating database $db"
    createdb -h localhost "$db"
  fi
  PGOPTIONS="-c client_min_messages=warning" psql -h localhost -d "$db" -qc "CREATE EXTENSION IF NOT EXISTS timescaledb"
done

echo "==> Starting Redis"
brew services start redis >/dev/null

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env from .env.example"
fi
if [[ ! -f frontend/.env.local ]]; then
  cp frontend/.env.example frontend/.env.local
fi

echo "==> Done. Postgres ($PG_FORMULA) and Redis are running. Next: make up"
