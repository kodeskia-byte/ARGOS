#!/usr/bin/env bash
#
# Prepara una instancia limpia (Ubuntu/Debian) para correr ARGOS.
#
#   ./deploy/setup.sh                  -> solo generador de carga
#   ./deploy/setup.sh --with-collector -> además instala el collector como servicio
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/venv"
WITH_COLLECTOR=0
[[ "${1:-}" == "--with-collector" ]] && WITH_COLLECTOR=1

# Muchos VPS entregan acceso root directo y no traen sudo instalado.
if [[ "$(id -u)" == "0" ]]; then
  SUDO=""
  RUN_AS="${SUDO_USER:-root}"
else
  SUDO="sudo"
  RUN_AS="$USER"
fi

echo "==> Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
# fonts-dejavu-core es obligatorio: sin él el PDF pierde tildes y la ñ.
$SUDO apt-get install -y -qq python3-venv python3-pip fonts-dejavu-core git

echo "==> Entorno virtual"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "==> Chromium y sus librerías de sistema"
"$VENV_DIR/bin/playwright" install --with-deps chromium

echo "==> Verificación"
"$VENV_DIR/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright
import os

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    browser.close()
print("chromium: ok")

font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
print("fuente PDF:", "ok" if os.path.isfile(font) else "FALTA -> el PDF perderá tildes")

from argos.probe.resources import ResourceSampler
sample = ResourceSampler().sample()
print(f"recursos: {sample['cpu_count']} vCPU, {sample['mem_total_mb']:.0f} MB RAM")
capacity = int(sample["mem_total_mb"] * 0.75 / 167)
print(f"sondas recomendadas por RAM disponible: ~{capacity}")
PY

if [[ "$WITH_COLLECTOR" == "1" ]]; then
  echo "==> Servicio del collector"
  $SUDO tee /etc/systemd/system/argos-collector.service >/dev/null <<EOF
[Unit]
Description=ARGOS collector (Live Room)
After=network.target

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$REPO_DIR
Environment=ARGOS_DB=$REPO_DIR/data/argos.db
# Descomenta y define un token para exigir autenticación en los endpoints /ingest.
#Environment=ARGOS_TOKEN=cambia-esto
ExecStart=$VENV_DIR/bin/python -m argos.controller.server --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now argos-collector
  $SUDO systemctl --no-pager status argos-collector | head -5
fi

echo
echo "Listo. Para ejecutar una carga:"
echo "  $VENV_DIR/bin/python runner.py --users 10 --duration 5m \\"
echo "      --flow flows/example.yaml \\"
echo "      --controller-url http://IP_DEL_COLLECTOR:8080 --instance-id gen-01"
