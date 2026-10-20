#!/usr/bin/env bash
# Deploy /opt/displayshow and enable systemd autostart (display-screen.service).
# Usage:
#   sudo /opt/displayshow/deploy.sh            # install + enable + start
#   sudo /opt/displayshow/deploy.sh restart    # restart running service
#   sudo /opt/displayshow/deploy.sh status     # show status / recent logs
#   sudo /opt/displayshow/deploy.sh uninstall  # disable and remove unit
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="display-screen"
UNIT_SRC="${APP_DIR}/display-screen.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV="${APP_DIR}/.venv"
PYTHON3="${PYTHON3:-python3}"
ACTION="${1:-install}"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "please run as root: sudo $0 $*"
  fi
}

ensure_python() {
  command -v "$PYTHON3" >/dev/null 2>&1 || die "python3 not found"
  "$PYTHON3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "need Python >= 3.9"
}

ensure_venv() {
  mkdir -p "$APP_DIR"
  local pybin="${VENV}/bin/python"
  if [ -x "$pybin" ] && "$pybin" -c "import serial, psutil" >/dev/null 2>&1; then
    log "venv ok: $pybin"
    return
  fi

  log "creating / repairing venv at ${VENV}"
  if "$PYTHON3" -m venv "$VENV" >/tmp/displayshow-venv.log 2>&1; then
    :
  elif "$PYTHON3" -m venv --without-pip "$VENV" >/tmp/displayshow-venv.log 2>&1; then
    :
  else
    mkdir -p "${VENV}/bin"
    ln -sfn "$(command -v "$PYTHON3")" "${VENV}/bin/python"
    ln -sfn python "${VENV}/bin/python3"
    cat > "${VENV}/pyvenv.cfg" <<EOF
home = /usr/bin
include-system-site-packages = false
version = $("$PYTHON3" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
executable = $(command -v "$PYTHON3")
command = $PYTHON3 -m venv ${VENV}
EOF
  fi

  pybin="${VENV}/bin/python"
  if [ ! -x "$pybin" ]; then
    ln -sfn "$(command -v "$PYTHON3")" "${VENV}/bin/python"
    ln -sfn python "${VENV}/bin/python3"
  fi
  [ -x "$pybin" ] || die "failed to create ${pybin}"

  if ! "$pybin" -c "import serial, psutil" >/dev/null 2>&1; then
    log "installing Python deps from requirements.txt"
    if "$pybin" -m pip --version >/dev/null 2>&1; then
      "$pybin" -m pip install -r "${APP_DIR}/requirements.txt"
    elif "$PYTHON3" -m pip --version >/dev/null 2>&1; then
      "$PYTHON3" -m pip install --target="${VENV}/lib/$("$PYTHON3" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')/site-packages" \
        -r "${APP_DIR}/requirements.txt"
    else
      die "pip not available; install python3-pip or python3-venv"
    fi
  fi

  "$pybin" -c "import serial, psutil" >/dev/null 2>&1 \
    || die "venv missing pyserial/psutil after install"
  log "venv ready: $pybin ($("$pybin" -c 'import serial,psutil,sys; print(sys.executable)'))"
}

install_unit() {
  [ -f "$UNIT_SRC" ] || die "missing unit file: $UNIT_SRC"
  [ -f "${APP_DIR}/screen_updater.py" ] || die "missing ${APP_DIR}/screen_updater.py"
  [ -f "${APP_DIR}/config.json" ] || die "missing ${APP_DIR}/config.json"
  local tmp
  tmp="$(mktemp)"
  sed "s|/opt/displayshow|${APP_DIR}|g" "$UNIT_SRC" >"$tmp"
  install -m 0644 "$tmp" "$UNIT_DST"
  rm -f "$tmp"
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  log "enabled ${SERVICE_NAME}.service (WantedBy=multi-user.target)"
}

start_service() {
  systemctl restart "${SERVICE_NAME}.service"
  sleep 1
  if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    log "started ${SERVICE_NAME}.service"
  else
    log "service not active yet (serial port may still be enumerating); showing status"
    systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
    return 1
  fi
}

show_status() {
  echo
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  echo
  log "recent logs:"
  journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager || true
  echo
  log "serial devices:"
  ls -l /dev/ttyUSB* /dev/serial/by-id 2>/dev/null || log "(no /dev/ttyUSB* yet)"
}

uninstall() {
  if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
  fi
  rm -f "$UNIT_DST"
  systemctl daemon-reload
  log "removed ${UNIT_DST} and disabled autostart"
}

case "$ACTION" in
  install|enable|deploy|"")
    need_root "$ACTION"
    ensure_python
    ensure_venv
    install_unit
    start_service || true
    show_status
    log "done. autostart is on; manage with:"
    log "  systemctl status|restart|stop ${SERVICE_NAME}.service"
    log "  journalctl -u ${SERVICE_NAME}.service -f"
    ;;
  restart)
    need_root "$ACTION"
    start_service
    show_status
    ;;
  status)
    show_status
    ;;
  uninstall|remove|disable)
    need_root "$ACTION"
    uninstall
    ;;
  *)
    die "unknown action: $ACTION (install|restart|status|uninstall)"
    ;;
esac
