#!/usr/bin/env bash
#
# Arranca el mismo runner en N generadores: mismo --ramp, mismo run_id,
# reloj NTP y (opcional) git pull.
#
# Desde el notebook, con collector local y túnel reverso, el comando único es
# deploy/up.sh (este script lo invoca al final):
#
#   ./deploy/up.sh --hosts deploy/hosts.txt --lite \
#       --ramp 10@2m,50@5m --flow flows/example.yaml
#
# Si el collector ya tiene IP pública, usá este script directo:
#
#   ./deploy/fleet.sh --hosts deploy/hosts.txt --pull --lite \
#       --ramp 10@2m,50@5m --flow flows/example.yaml \
#       --controller-url http://IP_COLLECTOR:8080
#
# hosts.txt: una línea por máquina
#   root@10.0.0.11
#   root@10.0.0.12 gen-02
#
set -euo pipefail

usage() {
  cat <<'EOF'
Uso: ./deploy/fleet.sh --hosts FILE [opciones] [args del runner...]

  --hosts FILE         Lista de generadores (obligatorio salvo --stop/--check
                       que también la piden)
  --remote-dir DIR     Directorio del repo en el servidor. Default: ARGOS
  --at HH:MM:SS        Esperar esa hora (reloj del servidor) antes de arrancar
  --pull               git pull en cada generador antes de lanzar
  --data FILE          CSV local: se copia y se pasa como --data
  --check              Solo verifica SSH, Python y el desfase de reloj
  --stop               pkill runner.py en todos los hosts
  --help

El resto de argumentos van a runner.py (--ramp, --flow, --lite, --users...).
Si no pasás --run-id, fleet genera UNO y lo comparte. instance-id es gen-01,
gen-02... salvo que la línea del hosts.txt traiga un nombre.

ARGOS_CONTROLLER_URL o --controller-url es obligatorio para arrancar.
ARGOS_TOKEN se reenvía si está definido en esta máquina.
EOF
}

HOSTS_FILE=""
REMOTE_DIR="${ARGOS_REMOTE_DIR:-ARGOS}"
AT=""
PULL=0
CHECK=0
STOP=0
DATA=""
RUNNER_ARGS=()
SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --at) AT="$2"; shift 2 ;;
    --pull) PULL=1; shift ;;
    --data) DATA="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --stop) STOP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) RUNNER_ARGS+=("$1"); shift ;;
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
ids=()
i=1
for line in "${HOST_LINES[@]}"; do
  # shellcheck disable=SC2086
  set -- $line
  host="$1"
  id="${2:-$(printf 'gen-%02d' "$i")}"
  hosts+=("$host")
  ids+=("$id")
  i=$((i + 1))
done

ssh_host() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "$host" "$@"
}

if [[ "$STOP" == "1" ]]; then
  echo "==> Deteniendo runner.py"
  for host in "${hosts[@]}"; do
    echo "  $host"
    ssh_host "$host" "pkill -f runner.py || true" &
  done
  wait
  echo "Listo."
  exit 0
fi

echo "==> Reloj y Python"
local_now=$(date -u +%s)
skew_bad=0
i=0
for host in "${hosts[@]}"; do
  remote_now=$(ssh_host "$host" "date -u +%s") || {
    echo "  $host FALLA ssh"
    exit 1
  }
  skew=$((remote_now - local_now))
  [[ $skew -lt 0 ]] && abs=$((-skew)) || abs=$skew
  py_ok=$(ssh_host "$host" "cd $REMOTE_DIR && ./venv/bin/python -c 'import argos; print(\"ok\")'" 2>/dev/null || echo "FALLA")
  printf '  %s  %s  desfase %+ds\n' "$host" "$py_ok" "$skew"
  if [[ "$py_ok" != "ok" ]]; then
    echo "No sigas: ese generador no tiene ARGOS listo." >&2
    exit 1
  fi
  if [[ $abs -gt 2 ]]; then
    echo "    aviso: desfase > 2s. Activando NTP..."
    ssh_host "$host" "timedatectl set-ntp true 2>/dev/null || chronyc waitsync 5 0.1 2>/dev/null || true" || true
    skew_bad=1
  fi
  i=$((i + 1))
done
if [[ "$CHECK" == "1" ]]; then
  [[ "$skew_bad" == "1" ]] && echo "Volvé a correr --check cuando NTP asiente." && exit 1
  echo "Los ${#hosts[@]} generadores responden."
  exit 0
fi

has_controller=0
has_run_id=0
filtered=()
skip_next=0
for arg in "${RUNNER_ARGS[@]+"${RUNNER_ARGS[@]}"}"; do
  if [[ "$skip_next" == "1" ]]; then
    skip_next=0
    continue
  fi
  case "$arg" in
    --instance-id) skip_next=1; continue ;;
    --instance-id=*) continue ;;
    --controller-url) has_controller=1 ;;
    --controller-url=*) has_controller=1 ;;
    --run-id) has_run_id=1 ;;
    --run-id=*) has_run_id=1 ;;
  esac
  filtered+=("$arg")
done
RUNNER_ARGS=("${filtered[@]+"${filtered[@]}"}")
if [[ "$has_controller" != "1" && -z "${ARGOS_CONTROLLER_URL:-}" ]]; then
  echo "Falta --controller-url o ARGOS_CONTROLLER_URL" >&2
  exit 1
fi

if [[ "$has_run_id" != "1" ]]; then
  RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
  RUNNER_ARGS+=(--run-id "$RUN_ID")
else
  RUN_ID="(el que pasaste en --run-id)"
fi

if [[ "$PULL" == "1" ]]; then
  echo "==> git pull"
  for host in "${hosts[@]}"; do
    ssh_host "$host" "cd $REMOTE_DIR && git pull --ff-only" &
  done
  wait
fi

if [[ -n "$DATA" ]]; then
  if [[ ! -f "$DATA" ]]; then
    echo "No existe el CSV $DATA" >&2
    exit 1
  fi
  remote_rel="flows/$(basename "$DATA")"
  echo "==> copiando CSV → $REMOTE_DIR/$remote_rel"
  for host in "${hosts[@]}"; do
    scp "${SSH_OPTS[@]}" "$DATA" "$host:$REMOTE_DIR/$remote_rel"
  done
  RUNNER_ARGS+=(--data "$remote_rel")
fi

echo "==> Arranque  run_id=$RUN_ID"
i=0
for host in "${hosts[@]}"; do
  gen="${ids[$i]}"
  echo "  $host  --instance-id $gen"
  wait_cmd=""
  if [[ -n "$AT" ]]; then
    wait_cmd="while [ \$(date +%H:%M:%S) \\< $(printf '%q' "$AT") ]; do sleep 0.2; done; "
  fi
  token_export=""
  [[ -n "${ARGOS_TOKEN:-}" ]] && token_export="ARGOS_TOKEN=$(printf '%q' "$ARGOS_TOKEN") "
  url_export=""
  [[ -n "${ARGOS_CONTROLLER_URL:-}" ]] && url_export="ARGOS_CONTROLLER_URL=$(printf '%q' "$ARGOS_CONTROLLER_URL") "
  cmd="./venv/bin/python runner.py"
  for arg in "${RUNNER_ARGS[@]+"${RUNNER_ARGS[@]}"}"; do
    cmd+=" $(printf '%q' "$arg")"
  done
  cmd+=" --instance-id $(printf '%q' "$gen")"
  remote="${wait_cmd}${token_export}${url_export}${cmd}"
  ssh_host "$host" "cd $REMOTE_DIR && nohup bash -c $(printf '%q' "$remote") > carga.log 2>&1 &"
  i=$((i + 1))
done

echo
echo "Lanzados ${#hosts[@]} generadores."
[[ -n "$AT" ]] && echo "Esperan el reloj a las $AT en cada servidor."
echo "Dashboard: ${ARGOS_CONTROLLER_URL:-el --controller-url que pasaste}"
echo "Para cortar: ./deploy/fleet.sh --hosts $HOSTS_FILE --stop"
