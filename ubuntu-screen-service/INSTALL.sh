#!/usr/bin/env bash
# Install displayshow to /opt/displayshow and enable systemd autostart.
# Usage (from extracted package):
#   sudo ./INSTALL.sh
#   sudo PREFIX=/opt/displayshow ./INSTALL.sh
#   sudo ./INSTALL.sh --force-config    # overwrite config.json
set -euo pipefail

PREFIX="${PREFIX:-/opt/displayshow}"
FORCE_CONFIG=0
ACTION="install"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "please run as root: sudo $0 $*"
  fi
}

usage() {
  cat <<'EOF'
displayshow installer

Usage:
  sudo ./INSTALL.sh                 Install/upgrade to /opt/displayshow and enable boot start
  sudo PREFIX=/opt/displayshow ./INSTALL.sh
  sudo ./INSTALL.sh --force-config  Also replace config.json (default: keep existing)
  sudo ./INSTALL.sh uninstall       Stop service and remove unit (files under PREFIX kept)
  sudo ./INSTALL.sh --help

After install:
  systemctl status display-screen.service
  journalctl -u display-screen.service -f
EOF
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --force-config) FORCE_CONFIG=1 ;;
    --prefix)
      PREFIX="${2:?}"
      shift
      ;;
    --prefix=*) PREFIX="${1#--prefix=}" ;;
    uninstall|remove) ACTION="uninstall" ;;
    -h|--help|help) usage; exit 0 ;;
    install|upgrade) ACTION="install" ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

if [ -f "${HERE}/files/screen_updater.py" ]; then
  SRC="${HERE}/files"
elif [ -f "${HERE}/screen_updater.py" ]; then
  SRC="$HERE"
else
  die "cannot find screen_updater.py (run this from the extracted package)"
fi

VERSION="unknown"
if [ -f "${HERE}/VERSION" ]; then
  VERSION="$(tr -d '[:space:]' <"${HERE}/VERSION")"
elif [ -f "${SRC}/VERSION" ]; then
  VERSION="$(tr -d '[:space:]' <"${SRC}/VERSION")"
fi

need_root

if [ "$ACTION" = "uninstall" ]; then
  if [ -x "${PREFIX}/deploy.sh" ]; then
    "${PREFIX}/deploy.sh" uninstall
  else
    systemctl disable --now display-screen.service 2>/dev/null || true
    rm -f /etc/systemd/system/display-screen.service
    systemctl daemon-reload
  fi
  log "uninstalled systemd unit. files remain in ${PREFIX}"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "need Python >= 3.9"

log "version ${VERSION}"
log "installing to ${PREFIX}"
mkdir -p "$PREFIX"

KEEP_CONFIG=0
if [ -f "${PREFIX}/config.json" ] && [ "$FORCE_CONFIG" -eq 0 ]; then
  KEEP_CONFIG=1
  log "keeping existing ${PREFIX}/config.json (use --force-config to replace)"
fi

copy_one() {
  local name="$1"
  local from="${SRC}/${name}"
  local to="${PREFIX}/${name}"
  if [ ! -e "$from" ]; then
    return 0
  fi
  if [ "$name" = "config.json" ] && [ "$KEEP_CONFIG" -eq 1 ]; then
    return 0
  fi
  install -m 0644 "$from" "$to"
}

# Runtime files
for name in \
  screen_updater.py \
  system_metrics.py \
  levetop_protocol.py \
  config.json \
  requirements.txt \
  display-screen.service \
  VERSION \
  部署手册.md \
  DEPLOYMENT.en.md \
  README-Ubuntu.md \
  实现方案.md
do
  copy_one "$name"
done

for name in deploy.sh wait_serial.sh run_screen_updater.sh run_screen_updater.py INSTALL.sh pack.sh; do
  if [ -e "${SRC}/${name}" ]; then
    install -m 0755 "${SRC}/${name}" "${PREFIX}/${name}"
  fi
done

chmod +x "${PREFIX}/deploy.sh" "${PREFIX}/wait_serial.sh" "${PREFIX}/run_screen_updater.sh" || true

log "running ${PREFIX}/deploy.sh install"
"${PREFIX}/deploy.sh" install

log "displayshow ${VERSION} installed at ${PREFIX}"
log "boot autostart: systemctl is-enabled display-screen.service"
log "docs: ${PREFIX}/部署手册.md  |  ${PREFIX}/DEPLOYMENT.en.md"
