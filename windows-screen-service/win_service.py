"""Windows Service wrapper for displayshow screen_updater.

Install (elevated):
  .\\.venv\\Scripts\\python.exe win_service.py install
  .\\.venv\\Scripts\\python.exe win_service.py start

Manage:
  win_service.py stop | restart | remove
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
os.chdir(APP_DIR)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pywin32 is required for the Windows service. "
        "Run: .\\.venv\\Scripts\\pip install pywin32\n"
        f"Import error: {exc}"
    ) from exc


class DisplayScreenService(win32serviceutil.ServiceFramework):
    _svc_name_ = "DisplayScreen"
    _svc_display_name_ = "LEVETOP Front Panel Display (displayshow)"
    _svc_description_ = (
        "Reads host metrics and updates the LEVETOP serial front-panel screen."
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._worker: threading.Thread | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        try:
            from screen_updater import dim_screen_now

            dim_screen_now("windows service stop")
        except Exception:  # noqa: BLE001
            pass
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._worker = threading.Thread(target=self._run_updater, name="screen-updater", daemon=True)
        self._worker.start()
        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
        if self._worker.is_alive():
            self._worker.join(timeout=25)
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )

    def _run_updater(self) -> None:
        try:
            from screen_updater import main

            main([])
        except Exception as exc:  # noqa: BLE001
            servicemanager.LogErrorMsg(f"displayshow failed: {exc}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(DisplayScreenService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(DisplayScreenService)
