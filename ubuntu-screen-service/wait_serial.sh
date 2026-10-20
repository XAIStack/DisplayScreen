#!/usr/bin/env bash
# Wait for the screen serial port before starting screen_updater.
# USB-UART often enumerates a few seconds after systemd reaches multi-user.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${APP_DIR}/config.json"
TIMEOUT="${SERIAL_WAIT_TIMEOUT:-20}"

ports=()
if command -v python3 >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
  mapfile -t ports < <(python3 - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
seen = []
led = str(cfg.get("led_serial_port") or "/dev/ttyUSB0")
for key in ("serial_port",):
    v = cfg.get(key)
    if v and str(v) not in seen:
        seen.append(str(v))
extra = cfg.get("serial_ports")
if isinstance(extra, list):
    for v in extra:
        s = str(v)
        if s not in seen:
            seen.append(s)
print("\n".join(p for p in seen if p != led) or "\n".join(seen))
PY
)
fi
if [ "${#ports[@]}" -eq 0 ]; then
  ports=("/dev/ttyUSB1" "/dev/ttyUSB0")
fi

i=0
while [ "$i" -lt "$TIMEOUT" ]; do
  for p in "${ports[@]}"; do
    if [ -e "$p" ]; then
      echo "serial ready: $p"
      exit 0
    fi
  done
  sleep 1
  i=$((i + 1))
done

echo "serial not ready after ${TIMEOUT}s (looked for: ${ports[*]}); starting anyway" >&2
exit 0
