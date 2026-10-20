# displayshow Windows Deployment Guide (English)

Front-panel LEVETOP serial display service: the host collects system metrics, writes them to the screen over USB-UART, and starts automatically as a **Windows service** (`DisplayScreen`).

| Item | Value |
|---|---|
| Version | **1.1.1** |
| Default install path | `%ProgramData%\displayshow` (typically `C:\ProgramData\displayshow`) |
| Windows service name | `DisplayScreen` |
| Target OS | Windows 10 / 11 / Server 2019+ (x64) |

Chinese version: [部署手册.md](./部署手册.md)

---

## 1. Requirements

| Item | Requirement |
|---|---|
| OS | Windows 10 or newer |
| Python | 3.9+, with **Add python.exe to PATH** enabled |
| Privileges | Administrator (install service, access serial ports) |
| Hardware | Front-panel serial screen (USB-UART, often `COM3`, 115200 8N1); optional LED strip (often `COM4`) |
| Optional | NVIDIA driver (`nvidia-smi` for dGPU); LibreHardwareMonitor (better CPU temperature via WMI) |

---

## 2. Package contents

```text
windows-screen-service/
  install.ps1              # One-shot installer (elevated)
  uninstall.ps1            # Remove service
  win_service.py           # Windows service entry (pywin32)
  screen_updater.py        # Main program (COM-port adapted)
  system_metrics.py        # Windows metric collectors
  levetop_protocol.py      # Screen protocol framing
  config.json              # Serial / VP addresses / page cycle
  requirements.txt         # pyserial, psutil, pywin32
  run_screen_updater.bat   # Foreground manual run
  部署手册.md              # Chinese guide
  DEPLOYMENT.en.md         # This document
  README.md
  VERSION
```

---

## 3. Installation

### 3.1 Install Python

Install Python 3.9+ from [python.org](https://www.python.org/downloads/) and enable **Add python.exe to PATH**.

Verify:

```powershell
python --version
```

### 3.2 Identify COM ports

Device Manager → Ports (COM & LPT), note which COM is the screen and which is the LED strip.

Or in PowerShell:

```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
# or
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Name
```

Typical mapping (check your machine):

- Screen: `COM3`
- LED strip: `COM4`

Update `serial_port` / `led_serial_port` in `config.json` accordingly.

### 3.3 One-shot install (Administrator PowerShell)

```powershell
cd windows-screen-service
# If scripts are blocked:
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The installer will:

1. Copy files to `%ProgramData%\displayshow`
2. Create a `.venv` and install `pyserial`, `psutil`, `pywin32`
3. Register and start the `DisplayScreen` service (Automatic start)

Custom path / overwrite config:

```powershell
.\install.ps1 -InstallDir "C:\displayshow"
.\install.ps1 -ForceConfig
```

---

## 4. Verification

```powershell
Get-Service DisplayScreen
# Status should be Running

# Foreground dry run (no serial writes)
& "$env:ProgramData\displayshow\.venv\Scripts\python.exe" -u "$env:ProgramData\displayshow\screen_updater.py" --dry-run --once
```

Or:

```powershell
cd $env:ProgramData\displayshow
.\run_screen_updater.bat --dry-run --once
```

Page 4 should show this host’s NIC IPs. Check **Event Viewer → Windows Logs → Application** for Python Service messages.

---

## 5. Day-to-day operations

```powershell
Get-Service DisplayScreen
Restart-Service DisplayScreen
Stop-Service DisplayScreen
Start-Service DisplayScreen
```

After editing `config.json`, restart the service:

```powershell
Restart-Service DisplayScreen
```

Backlight tests:

```powershell
cd $env:ProgramData\displayshow
.\run_screen_updater.bat --blank
.\run_screen_updater.bat --unblank
```

---

## 6. Upgrade

From the new package directory:

```powershell
cd <new>\windows-screen-service
.\install.ps1
```

By default the installer **keeps** the existing `config.json`. To replace it:

```powershell
.\install.ps1 -ForceConfig
```

---

## 7. Uninstall

```powershell
cd $env:ProgramData\displayshow
.\uninstall.ps1                 # Remove service only; keep files
.\uninstall.ps1 -RemoveFiles    # Also delete the install directory
```

---

## 8. Configuration

File: `%ProgramData%\displayshow\config.json`

### 8.1 Serial ports

| Key | Default | Notes |
|---|---|---|
| `serial_port` / `serial_ports` | `COM3` | Screen port / probe order |
| `led_serial_port` | `COM4` | LED strip; skipped during screen probe |
| `baudrate` | `115200` | Must match the screen project |

COM10 and above are automatically normalized to `\\.\COMx`.

### 8.2 NIC IPs

| Key | Default | Notes |
|---|---|---|
| `net_iface_auto` | `true` | Auto-pick physical Ethernet adapters |
| `net_ip_wait_seconds` | `8` | Seconds to wait for IPv4 at startup |
| `net1_iface` / `net2_iface` | empty | Used only when `net_iface_auto` is `false` |

Pin specific adapters (names as shown in Network Connections):

```json
"net_iface_auto": false,
"net1_iface": "Ethernet",
"net2_iface": "Ethernet 2"
```

### 8.3 Page cycle and refresh

| Key | Notes |
|---|---|
| `page_cycle` | Page numbers and dwell seconds (default 4→5→6→7→8→9) |
| `refresh_interval_seconds` | Overview refresh interval (default 60) |

---

## 9. Windows service notes

| Item | Value |
|---|---|
| Service name | `DisplayScreen` |
| Display name | LEVETOP Front Panel Display (displayshow) |
| Start type | Automatic |
| Implementation | `win_service.py` + pywin32 |

Manual register (normally use `install.ps1`):

```powershell
cd $env:ProgramData\displayshow
.\.venv\Scripts\python.exe win_service.py --startup auto install
Start-Service DisplayScreen
```

---

## 10. Differences from the Linux build

| Item | Windows | Linux |
|---|---|---|
| Serial device | `COMx` | `/dev/ttyUSB*` |
| Service manager | Windows Service | systemd |
| CPU temperature | LibreHardwareMonitor / ACPI | `coretemp` hwmon |
| Disk bay mapping | `Get-PhysicalDisk` order | PCIe / ATA port mapping |
| Install path | `%ProgramData%\displayshow` | `/opt/displayshow` |

Screen protocol and VP addresses are the same as the Linux build.

---

## 11. Troubleshooting

| Symptom | Action |
|---|---|
| Service will not start | `Get-Service DisplayScreen`; Event Viewer Application log |
| No ACK / screen unchanged | Wrong COM number, port in use, or baud mismatch |
| Access denied opening COM | Install/run service as Administrator; close other serial tools |
| IP shows `0.0.0.0` | Cable unplugged or no address; check `ipconfig` / `Get-NetIPAddress` |
| Temperature always 0 | Install/run LibreHardwareMonitor with WMI enabled; or accept ACPI estimate |
| `pywin32` install failed | Use 64-bit Python; re-run `install.ps1` |
| Script execution blocked | `Set-ExecutionPolicy -Scope Process Bypass` |

List COM ports:

```powershell
python -c "import serial.tools.list_ports as lp; print([p.device for p in lp.comports()])"
```

---

## 12. Pages (summary)

| Page | Dwell | Content |
|---|---|---|
| 4 | 10 s | Overview: clock, NIC IPs, health, network status |
| 5 | 2 s | CPU usage, temperature, temp bar |
| 6 | 2 s | GPU usage, temperature, temp bar |
| 7 | 2 s | Memory usage, capacity, battery (fixed 0) |
| 8 | 2 s | Storage usage, network rates |
| 9 | 2 s | SSD ×4 + HDD ×6 status icons |
