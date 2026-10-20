#!/opt/displayshow/.venv/bin/python
"""Launch screen_updater with the project venv. Safe for systemd (no bash shebang)."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.argv[0] = str(ROOT / "screen_updater.py")
runpy.run_path(str(ROOT / "screen_updater.py"), run_name="__main__")
