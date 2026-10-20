# displayshow Deployment Guide (English)

Front-panel LEVETOP serial display service: the host collects system metrics, writes them to the screen over USB-UART, and starts automatically via systemd.

| Item | Value |
|---|---|
| Version | **1.1.1** |
| Install path | `/opt/displayshow` |
| systemd unit | `display-screen.service` |
| Target OS | Ubuntu 20.04+ / Debian / other systemd Linux |

Chinese version: [部署手册.md](./部署手册.md)

---

## 1. Requirements

| Item | Requirement |
|---|---|
| OS | Ubuntu (or any systemd-based Linux) |
| Python | 3.9+, with `python3-venv` and `python3-pip` |
| Privileges | root (write `/opt`, install unit, access serial) |
| Hardware | Front-panel serial screen (usually `/dev/ttyUSB1`, 115200 8N1); optional LED strip (`/dev/ttyUSB0`) |
| Group | Runtime user must access the serial port (typically `dialout`) |

NIC IPs are auto-detected from onboard Ethernet by default; renaming interfaces on a new machine is usually unnecessary.

---

## 2. Package contents

```text
ubuntu-screen-service/
  INSTALL.sh                 # One-shot installer
  deploy.sh                  # Create venv and enable systemd
  wait_serial.sh             # Wait for USB serial at boot
  display-screen.service     # systemd unit template
  screen_updater.py          # Main program
  system_metrics.py          # Metric collectors
  levetop_protocol.py        # Screen protocol framing
  config.json                # Serial / VP addresses / page cycle
  requirements.txt           # pyserial, psutil
  run_screen_updater.sh|.py  # Manual launch helpers
  部署手册.md                # Chinese guide
  DEPLOYMENT.en.md           # This document
  VERSION
```

The package does **not** ship a `.venv`; `deploy.sh` creates it on the target host.

---

## 3. Installation

### 3.1 System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
```

### 3.2 Check serial devices

```bash
ls -l /dev/ttyUSB* /dev/serial/by-id
```

Typical mapping:

- `/dev/ttyUSB1`: screen
- `/dev/ttyUSB0`: LED strip (do not point the screen app at this port)

### 3.3 One-shot install

```bash
cd ubuntu-screen-service
sudo ./INSTALL.sh
```

Default path is `/opt/displayshow`. The installer will:

1. Copy program files  
2. Create a Python venv and install `pyserial`, `psutil`  
3. Install `/etc/systemd/system/display-screen.service`  
4. Run `systemctl enable --now display-screen.service`

Custom prefix:

```bash
sudo PREFIX=/opt/displayshow ./INSTALL.sh
```

Overwrite an existing `config.json` with the packaged defaults:

```bash
sudo ./INSTALL.sh --force-config
```

---

## 4. Verification

```bash
systemctl is-enabled display-screen.service   # should be enabled
systemctl status display-screen.service
journalctl -u display-screen.service -n 50 --no-pager
```

Successful logs look like:

```text
Opened /dev/ttyUSB1 @ 115200 baud
NIC auto=True discovered=enp86s0,enp87s0 -> net1=enp86s0 net2=enp87s0
net1_ip@0x1116=10.x.x.x (...)
  <- ACK OK (CRC passed)
```

Page 4 should show this host’s NIC IPs. If only one NIC is up, the other showing `0.0.0.0` is normal.

Dry run (no serial writes):

```bash
sudo /opt/displayshow/.venv/bin/python -u /opt/displayshow/screen_updater.py --dry-run --once
```

---

## 5. Day-to-day operations

```bash
sudo systemctl status display-screen.service
sudo systemctl restart display-screen.service
sudo systemctl stop display-screen.service
sudo journalctl -u display-screen.service -f

sudo /opt/displayshow/deploy.sh status
sudo /opt/displayshow/deploy.sh restart
```

After editing `config.json`, restart the service:

```bash
sudo systemctl restart display-screen.service
```

Backlight tests:

```bash
sudo /opt/displayshow/.venv/bin/python -u /opt/displayshow/screen_updater.py --blank
sudo /opt/displayshow/.venv/bin/python -u /opt/displayshow/screen_updater.py --unblank
```

---

## 6. Upgrade

From the new package directory:

```bash
sudo ./INSTALL.sh
```

By default the installer **keeps** the existing `/opt/displayshow/config.json`. To replace it with package defaults:

```bash
sudo ./INSTALL.sh --force-config
```

---

## 7. Uninstall

Stop and remove the systemd unit only (files remain):

```bash
sudo ./INSTALL.sh uninstall
# or
sudo /opt/displayshow/deploy.sh uninstall
```

Remove the unit and the program tree:

```bash
sudo /opt/displayshow/deploy.sh uninstall
sudo rm -rf /opt/displayshow
```

---

## 8. Configuration

File: `/opt/displayshow/config.json`

### 8.1 Serial ports

| Key | Default | Notes |
|---|---|---|
| `serial_port` / `serial_ports` | `/dev/ttyUSB1` | Screen port / probe order |
| `led_serial_port` | `/dev/ttyUSB0` | LED strip; skipped during screen probe |
| `baudrate` | `115200` | Must match the screen project |

If the screen is on `ttyUSB0`, swap these two entries and `restart`.

### 8.2 NIC IPs

| Key | Default | Notes |
|---|---|---|
| `net_iface_auto` | `true` | Map panel net1/net2 by onboard NIC PCI order |
| `net_ip_wait_seconds` | `8` | Max wait for DHCP at boot (seconds) |
| `net1_iface` / `net2_iface` | empty | Used only when `net_iface_auto` is `false` |

Pin specific interfaces:

```json
"net_iface_auto": false,
"net1_iface": "enp86s0",
"net2_iface": "enp87s0"
```

### 8.3 Page cycle and refresh

| Key | Notes |
|---|---|
| `page_cycle` | Page numbers and dwell seconds (default 4→5→6→7→8→9) |
| `refresh_interval_seconds` | Overview / full-page rewrite interval (default 60) |

### 8.4 Disk icons (page 9)

Set `ssd_slot_pci_roots` to match the board’s PCIe M.2 roots. After a motherboard change, SSD slot order may need realignment. Unlisted NVMe drives fill remaining empty slots.

---

## 9. systemd unit

Installed path: `/etc/systemd/system/display-screen.service`

```ini
[Unit]
Description=LEVETOP serial front-panel display (displayshow)
After=local-fs.target network-online.target systemd-udev-settle.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=/opt/displayshow
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/opt/displayshow/wait_serial.sh
ExecStart=/opt/displayshow/.venv/bin/python -u /opt/displayshow/screen_updater.py
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
SupplementaryGroups=dialout
StandardOutput=journal
StandardError=journal
SyslogIdentifier=display-screen

[Install]
WantedBy=multi-user.target
```

`INSTALL.sh` runs `systemctl enable`. The service starts at multi-user boot; no `rc.local` or crontab is required.

---

## 10. Troubleshooting

| Symptom | Action |
|---|---|
| Service fails to start | `journalctl -u display-screen.service -n 80 --no-pager` |
| No screen update / no ACK | Wrong port (swap USB0/USB1), baud rate, or permissions (`dialout`) |
| IP shows `0.0.0.0` | Cable unplugged or no address yet; check `ip -br addr` |
| Old iface names still used | Ensure `net_iface_auto` is `true`, then `restart` |
| Temperature stuck at 42 | Reading ACPI `acpitz`, or wrong character control address/type |
| Blank screen for a few seconds at boot | USB enumerate delay; `wait_serial.sh` waits up to 20 s — normal |
| `python3-venv` / pip errors | Install `python3-venv python3-pip`, re-run `INSTALL.sh` |
| Permission denied | `sudo usermod -aG dialout $USER`, then re-login |
| `restart` hangs ~10–30 s | Serial still held; unit `TimeoutStopSec=30`, new process starts after stop |

Inspect NICs:

```bash
ip -br addr
ls -l /sys/class/net/
```

---

## 11. Pages (summary)

| Page | Dwell | Content |
|---|---|---|
| 4 | 10 s | Overview: clock, NIC IPs, health, network status |
| 5 | 2 s | CPU usage, temperature, temp bar |
| 6 | 2 s | GPU usage, temperature, temp bar |
| 7 | 2 s | Memory usage, capacity, battery (fixed 0) |
| 8 | 2 s | Storage usage, network rates |
| 9 | 2 s | M.2 SSD ×4 + HDD ×6 status icons |

Protocol and VP address details are in the original project design doc (`实现方案.md`).
