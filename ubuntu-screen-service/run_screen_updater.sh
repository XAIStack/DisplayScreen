#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
  exit 1
fi

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [ -x /opt/displayshow/.venv/bin/python ]; then
  PYTHON=/opt/displayshow/.venv/bin/python
elif [ -x /home/displayshow/.venv/bin/python ]; then
  PYTHON=/home/displayshow/.venv/bin/python
elif [ -x /root/displayScreen-venv/bin/python ]; then
  PYTHON=/root/displayScreen-venv/bin/python
else
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi

exec "$PYTHON" -u screen_updater.py "$@"
