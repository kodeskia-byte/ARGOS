#!/usr/bin/env bash
#
# Un comando desde el notebook: collector local + túnel reverso a cada
# generador + (opcional) git pull + el mismo ramp en toda la flota.
#
#   ./deploy/up.sh --hosts deploy/hosts.txt --lite \
#       --ramp 5@1m,15@3m,30@5m --flow flows/example.yaml
#
# Corta runners y túneles (el collector sigue para mirar el informe):
#   ./deploy/up.sh --hosts deploy/hosts.txt --down
#
# El collector queda en 127.0.0.1:8080. Cada generador lo ve como
# localhost:8080 gracias al ssh -R. Dashboard: http://127.0.0.1:8080
#
# Si el collector ya corre en un VPS con IP pública, no uses este script:
# usá deploy/fleet.sh con --controller-url http://IP:8080.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/data/argos-up"
PY="$ROOT/venv/bin/python"
COLLECTOR_HOST="127.0.0.1"
COLLECTOR_PORT="8080"
HOSTS_FILE=""
DOWN=0
KILL_COLLECTOR=0
OBSERVE=0
PULL=1
SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)
FLEET_ARGS=()

usage() {
  cat <<'EOF'
Uso: ./deploy/up.sh --hosts FILE [opciones] [args del runner...]

  --hosts FILE         Lista de generadores (igual que fleet.sh)
  --down               pkill runner.py + cierra los túneles
  --kill-collector     Con --down, también para el collector que arrancó up.sh
  --observe            Túnel extra -R 5080 (OpenObserve en el notebook)
  --no-pull            No hace git pull en los generadores
  --help

El resto va a fleet.sh / runner.py (--ramp, --flow, --lite, --at, --data,
--check, --stop...). Si no pasás --run-id, fleet genera UNO y lo comparte.

Dashboard: http://127.0.0.1:8080
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --down) DOWN=1; shift ;;
    --kill-collector) KILL_COLLECTOR=1; shift ;;
    --observe) OBSERVE=1; shift ;;
    --no-pull) PULL=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) FLEET_ARGS+=("$1"); shift ;;
  esac
done

if [[ -z "$HOSTS_FILE" ]]; then
  echo "Falta --hosts FILE" >&2
  usage
  exit 1
fi
if [[ ! -f "$HOSTS_FILE" ]]; then
  echo "No existe $HOSTS_FILE" >&2
  exit 1
fi

mapfile -t HOST_LINES < <(grep -vE '^\s*(#|$)' "$HOSTS_FILE")
if [[ ${#HOST_LINES[@]} -eq 0 ]]; then
  echo "$HOSTS_FILE no tiene hosts" >&2
  exit 1
fi

hosts=()
for line in "${HOST_LINES[@]}"; do
  # shellcheck disable=SC2086
  set -- $line
  hosts+=("$1")
done

ssh_host() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "$host" "$@"
}

live_ok() {
  local url="${1:-http://127.0.0.1:${COLLECTOR_PORT}/api/live}"
  curl -sf -m 2 "$url" >/dev/null 2>&1
}

mkdir -p "$STATE" "$ROOT/data"

stop_tunnels() {
  local pidfile pid
  shopt -s nullglob
  for pidfile in "$STATE"/tunnel-*.pid; do
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "  túnel pid $pid"
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
  shopt -u nullglob
}

stop_collector() {
  local pidfile="$STATE/collector.pid"
  local pid
  pid=$(cat "$pidfile" 2>/dev/null || true)
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "  collector pid $pid"
    kill "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  else
    echo "  collector: no hay pid de up.sh (si lo arrancaste a mano, dejalo)"
  fi
}

if [[ "$DOWN" == "1" ]]; then
  echo "==> Stop runners"
  "$ROOT/deploy/fleet.sh" --hosts "$HOSTS_FILE" --stop || true
  echo "==> Cerrando túneles"
  stop_tunnels
  if [[ "$KILL_COLLECTOR" == "1" ]]; then
    echo "==> Collector"
    stop_collector
  else
    echo "Collector sigue en http://${COLLECTOR_HOST}:${COLLECTOR_PORT} (informe)."
    echo "Para pararlo: ./deploy/up.sh --hosts $HOSTS_FILE --down --kill-collector"
  fi
  exit 0
fi

# --stop en los args: no levantar nada, solo fleet.
for arg in "${FLEET_ARGS[@]+"${FLEET_ARGS[@]}"}"; do
  if [[ "$arg" == "--stop" ]]; then
    exec "$ROOT/deploy/fleet.sh" --hosts "$HOSTS_FILE" "${FLEET_ARGS[@]}"
  fi
done

if [[ ! -x "$PY" ]]; then
  echo "No hay $PY. En este notebook: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

echo "==> Collector  http://${COLLECTOR_HOST}:${COLLECTOR_PORT}"
if live_ok; then
  echo "  ya responde /api/live"
  echo "  si el informe no tiene «Adjuntar access.log», era un collector viejo:"
  echo "  ./deploy/up.sh --hosts $HOSTS_FILE --down --kill-collector  y reintentá"
else
  echo "  arrancando…"
  nohup "$PY" -m argos.controller.server --host "$COLLECTOR_HOST" --port "$COLLECTOR_PORT" \
    > "$ROOT/data/collector.log" 2>&1 &
  echo $! > "$STATE/collector.pid"
  ok=0
  for _ in $(seq 1 25); do
    if live_ok; then
      ok=1
      break
    fi
    sleep 0.2
  done
  if [[ "$ok" != "1" ]]; then
    echo "El collector no levantó. Mirá $ROOT/data/collector.log" >&2
    exit 1
  fi
  echo "  pid $(cat "$STATE/collector.pid")"
fi

echo "==> Túneles reversos  -R ${COLLECTOR_PORT}:127.0.0.1:${COLLECTOR_PORT}"
i=0
for host in "${hosts[@]}"; do
  if ssh_host "$host" "curl -sf -m 2 http://127.0.0.1:${COLLECTOR_PORT}/api/live >/dev/null"; then
    echo "  $host  ya llega al collector"
    i=$((i + 1))
    continue
  fi
  extra=()
  if [[ "$OBSERVE" == "1" ]]; then
    extra+=(-R "5080:127.0.0.1:5080")
  fi
  echo "  $host  abriendo túnel"
  # stdin cerrado: si no, ssh puede robar el TTY y colgar el script.
  ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes \
    -R "${COLLECTOR_PORT}:127.0.0.1:${COLLECTOR_PORT}" \
    "${extra[@]+"${extra[@]}"}" \
    "$host" < /dev/null > "$STATE/tunnel-$i.log" 2>&1 &
  echo $! > "$STATE/tunnel-$i.pid"
  ready=0
  for _ in $(seq 1 20); do
    if ! kill -0 "$(cat "$STATE/tunnel-$i.pid")" 2>/dev/null; then
      break
    fi
    if ssh_host "$host" "curl -sf -m 2 http://127.0.0.1:${COLLECTOR_PORT}/api/live >/dev/null"; then
      ready=1
      break
    fi
    sleep 0.3
  done
  if [[ "$ready" != "1" ]]; then
    echo "  $host  no llega. log: $STATE/tunnel-$i.log" >&2
    echo "  ¿Puerto ${COLLECTOR_PORT} ocupado en el servidor? --down y reintentá." >&2
    cat "$STATE/tunnel-$i.log" >&2 || true
    exit 1
  fi
  echo "  $host  ok"
  i=$((i + 1))
done

export ARGOS_CONTROLLER_URL="http://127.0.0.1:${COLLECTOR_PORT}"

echo "==> Flota"
fleet=("$ROOT/deploy/fleet.sh" --hosts "$HOSTS_FILE")
if [[ "$PULL" == "1" ]]; then
  fleet+=(--pull)
fi
fleet+=("${FLEET_ARGS[@]+"${FLEET_ARGS[@]}"}")
"${fleet[@]}"

echo
echo "Dashboard: http://${COLLECTOR_HOST}:${COLLECTOR_PORT}"
echo "Para cortar: ./deploy/up.sh --hosts $HOSTS_FILE --down"
