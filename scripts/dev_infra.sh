#!/usr/bin/env bash
# Start PostgreSQL + pgvector and Redis natively, without Docker.
#
# Compose is the supported path (`make up`). This exists for environments
# where a container registry is not reachable but the packages are installed
# locally -- CI sandboxes, locked-down laptops -- so the integration tier can
# still run against real infrastructure instead of being skipped into
# irrelevance.
#
#   ./scripts/dev_infra.sh start | stop | status
set -uo pipefail

PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${PGDATA:-/var/lib/postgresql/16/main}"
PGCONF="${PGCONF:-/etc/postgresql/16/main/postgresql.conf}"
DB_USER="${DB_USER:-agentmarket}"
DB_PASS="${DB_PASS:-agentmarket}"
DB_NAME="${DB_NAME:-agentmarket}"

start_postgres() {
  if pg_isready -h 127.0.0.1 -q 2>/dev/null; then
    echo "  postgres already running"
  else
    sudo -u postgres "${PGBIN}/pg_ctl" -D "$PGDATA" -l /tmp/pg.log \
      -o "-c config_file=${PGCONF}" start >/dev/null 2>&1 \
      || pg_ctlcluster 16 main start >/dev/null 2>&1
    for _ in $(seq 1 30); do
      pg_isready -h 127.0.0.1 -q 2>/dev/null && break
      sleep 0.5
    done
    pg_isready -h 127.0.0.1 -q 2>/dev/null && echo "  postgres up" || {
      echo "  postgres FAILED to start (see /tmp/pg.log)"; return 1; }
  fi

  # Role, database and extension are created idempotently: a fresh data
  # directory is indistinguishable from a first run, and the whole point of
  # this script is that it works either way.
  sudo -u postgres psql -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" 2>/dev/null | grep -q 1 \
    || sudo -u postgres psql -q -c \
       "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}' SUPERUSER" >/dev/null
  sudo -u postgres psql -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null | grep -q 1 \
    || sudo -u postgres psql -q -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}" >/dev/null
  PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -q \
    -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null 2>&1 \
    && echo "  pgvector ready" || echo "  WARNING: pgvector extension unavailable"
}

start_redis() {
  if redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then
    echo "  redis already running"
    return
  fi
  # No persistence: this is a cache and a broker for a demo stack, and an
  # unexpected dump file in the working directory is a worse surprise than a
  # cold start.
  redis-server --daemonize yes --save '' --appendonly no >/dev/null 2>&1
  for _ in $(seq 1 20); do
    redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 && break
    sleep 0.25
  done
  redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 && echo "  redis up" || echo "  redis FAILED"
}

case "${1:-start}" in
  start)
    echo "starting local infrastructure..."
    start_postgres
    start_redis
    echo
    echo "next:  export DATABASE_URL=postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}"
    echo "       python scripts/seed.py && ./scripts/run_local.sh start"
    ;;
  stop)
    redis-cli -h 127.0.0.1 shutdown nosave >/dev/null 2>&1 && echo "  redis stopped"
    sudo -u postgres "${PGBIN}/pg_ctl" -D "$PGDATA" stop -m fast >/dev/null 2>&1 \
      && echo "  postgres stopped"
    ;;
  status)
    pg_isready -h 127.0.0.1 2>/dev/null || echo "  postgres down"
    redis-cli -h 127.0.0.1 ping 2>/dev/null || echo "  redis down"
    ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2; exit 1 ;;
esac
