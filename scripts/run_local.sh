#!/usr/bin/env bash
# Run the whole backend without Docker: five uvicorn processes against a
# local Postgres and Redis. This is the fastest edit/reload loop, and it is
# how the integration tests drive the system.
#
#   ./scripts/run_local.sh start | stop | status | logs <service>
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT}/.run"
mkdir -p "$RUN_DIR"

export DATABASE_URL="${DATABASE_URL:-postgresql://agentmarket:agentmarket@127.0.0.1:5432/agentmarket}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
# Redis Streams is the default here: it is a real network broker that is
# already running for the feature store, so local dev exercises the
# distributed code path without a Kafka cluster. Compose uses Kafka.
export EVENT_BUS_BACKEND="${EVENT_BUS_BACKEND:-redis}"
export EMBEDDINGS_BACKEND="${EMBEDDINGS_BACKEND:-ollama}"
export SIGNING_KEY_PATH="${SIGNING_KEY_PATH:-${RUN_DIR}/keys.json}"
export GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8080}"
export PRICING_URL="${PRICING_URL:-http://127.0.0.1:8081}"
export STOREFRONT_URL="${STOREFRONT_URL:-http://127.0.0.1:8082}"
export VERIFICATION_URL="${VERIFICATION_URL:-http://127.0.0.1:8083}"
export PAYMENTS_URL="${PAYMENTS_URL:-http://127.0.0.1:8084}"
export PYTHONUNBUFFERED=1

declare -A PORTS=( [gateway]=8080 [pricing]=8081 [storefront]=8082 [verification]=8083 [payments]=8084 )

start_one() {
  local name="$1" port="${PORTS[$1]}"
  # setsid + full fd redirection detaches the service from this shell's
  # process group and its stdout. Without it the started processes keep the
  # caller's pipe open and `./run_local.sh start | tee` never returns.
  ( cd "${ROOT}/services/${name}" && \
    SERVICE_NAME="$name" setsid python3 -m uvicorn app:app --host 0.0.0.0 --port "$port" \
      > "${RUN_DIR}/${name}.log" 2>&1 < /dev/null & )
  # The PID is resolved from the port rather than from `$!`. setsid forks when
  # the shell has already made it a process-group leader, so `$!` is often the
  # pid of a process that has already exited -- which makes `stop` silently do
  # nothing and leaves the next `start` binding against a service that is
  # still running the previous build. That failure is invisible until you
  # wonder why a code change had no effect.
  local pid=""
  for _ in $(seq 1 40); do
    pid="$(pgrep -f "uvicorn app:app --host 0.0.0.0 --port ${port}$" | head -1)"
    [ -n "$pid" ] && break
    sleep 0.25
  done
  [ -n "$pid" ] && echo "$pid" > "${RUN_DIR}/${name}.pid"
  echo "  ${name} -> :${port} (pid ${pid:-unknown})"
}

case "${1:-start}" in
  start)
    echo "starting services..."
    # Order matters only for readiness, not correctness -- every service
    # retries its dependencies. Leaf services first keeps the logs quiet.
    for s in verification pricing payments storefront gateway; do start_one "$s"; done
    echo "waiting for health..."
    for s in verification pricing payments storefront gateway; do
      for _ in $(seq 1 40); do
        if curl -sf "http://127.0.0.1:${PORTS[$s]}/live" > /dev/null 2>&1; then
          echo "  ${s} up"; break
        fi
        sleep 0.5
      done
    done
    ;;
  stop)
    for s in "${!PORTS[@]}"; do
      # By port, not by recorded pid: the pid file can be stale or wrong, and
      # a "stopped" message for a process that is still serving requests is
      # worse than no message at all.
      pids="$(pgrep -f "uvicorn app:app --host 0.0.0.0 --port ${PORTS[$s]}$" || true)"
      if [ -n "$pids" ]; then
        kill $pids 2>/dev/null && echo "stopped ${s} (${pids})"
      fi
      rm -f "${RUN_DIR}/${s}.pid"
    done
    ;;
  status)
    for s in "${!PORTS[@]}"; do
      code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORTS[$s]}/health" 2>/dev/null)
      echo "  ${s} (:${PORTS[$s]}) -> ${code:-down}"
    done
    ;;
  logs)
    tail -n 100 -f "${RUN_DIR}/${2:-gateway}.log"
    ;;
  *)
    echo "usage: $0 {start|stop|status|logs <service>}" >&2; exit 1 ;;
esac
