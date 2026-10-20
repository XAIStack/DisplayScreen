"""Read system metrics on Windows for LEVETOP serial screen updater."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from typing import Literal, Optional

DiskStatus = Literal["ok", "empty", "error"]
NetworkStatus = Literal["connected", "NoInternet", "Disconnected"]

_VIRTUAL_IFACE_MARKERS = (
    "loopback",
    "docker",
    "vethernet",
    "hyper-v",
    "vmware",
    "virtualbox",
    "tap",
    "tun",
    "wsl",
    "bluetooth",
    "teredo",
    "isatap",
    "npcap",
)


def memory_usage_percent() -> int:
    try:
        import psutil

        return max(0, min(100, int(round(psutil.virtual_memory().percent))))
    except ImportError:
        return 0


def memory_usage_detail() -> dict[str, int | str]:
    total_b = used_b = 0
    percent = 0
    try:
        import psutil

        mem = psutil.virtual_memory()
        total_b = int(mem.total)
        used_b = int(mem.total - mem.available)
        percent = max(0, min(100, int(round(mem.percent))))
    except ImportError:
        pass

    total_gb = _bytes_to_gb(total_b, snap_common=True)
    used_gb = _bytes_to_gb(used_b, snap_common=False)
    if total_gb > 0 and used_gb > total_gb:
        used_gb = total_gb
    return {
        "percent": percent,
        "used_gb": used_gb,
        "total_gb": total_gb,
        "text": f"{used_gb}GB/{total_gb}GB",
    }


def _bytes_to_gb(n_bytes: int, *, snap_common: bool) -> int:
    if n_bytes <= 0:
        return 0
    gb = n_bytes / (1024 ** 3)
    if snap_common:
        for size in (4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512):
            if size * 0.90 <= gb <= size:
                return size
        return max(1, int(round(gb)))
    return max(0, int(round(gb)))


def storage_usage_detail() -> dict[str, int | str]:
    used_b, total_b = _storage_bytes()
    percent = 0 if total_b <= 0 else max(0, min(100, int(round(used_b * 100.0 / total_b))))
    return {
        "percent": percent,
        "text": _format_used_total_tb(used_b, total_b),
        "used_b": used_b,
        "total_b": total_b,
    }


def _format_used_total_tb(used_b: int, total_b: int) -> str:
    used_tb = max(0.0, used_b / 1_000_000_000_000)
    total_tb = max(0.0, total_b / 1_000_000_000_000)
    used_s = f"{used_tb:.1f}TB"
    if abs(total_tb - round(total_tb)) < 0.15:
        total_s = f"{int(round(total_tb))}TB"
    else:
        total_s = f"{total_tb:.1f}TB"
    return f"{used_s}/{total_s}"


def _storage_bytes() -> tuple[int, int]:
    try:
        import psutil

        used = total = 0
        for part in psutil.disk_partitions(all=False):
            if part.fstype.upper() in ("", "CDFS", "UDF"):
                continue
            drive = (part.mountpoint or "").upper()
            if not re.match(r"^[A-Z]:\\?$", drive):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            used += int(usage.used)
            total += int(usage.total)
        if total > 0:
            return used, total
    except ImportError:
        pass
    return 0, 0


SSD_SLOT_COUNT = 4
HDD_SLOT_COUNT = 6


def disk_bay_status(
    *,
    ssd_slot_pci_roots: Optional[list[str]] = None,
    hdd_slot_pci: Optional[str] = None,
    hdd_ata_ports: Optional[list[int]] = None,
    ssd_slots: int = SSD_SLOT_COUNT,
    hdd_slots: int = HDD_SLOT_COUNT,
    check_smart: bool = True,
) -> dict[str, list[DiskStatus]]:
    """Map physical disks to SSD/HDD icon slots on Windows."""
    del ssd_slot_pci_roots, hdd_slot_pci, hdd_ata_ports  # Linux-oriented hints unused
    ssd_n = max(0, int(ssd_slots))
    hdd_n = max(0, int(hdd_slots))
    nvme, hdd = _enumerate_windows_disks(check_smart=check_smart)
    ssd_slots_list: list[DiskStatus] = ["empty"] * ssd_n
    hdd_slots_list: list[DiskStatus] = ["empty"] * hdd_n
    for index, status in enumerate(nvme[:ssd_n]):
        ssd_slots_list[index] = status
    for index, status in enumerate(hdd[:hdd_n]):
        hdd_slots_list[index] = status
    return {"ssd": ssd_slots_list, "hdd": hdd_slots_list}


def _enumerate_windows_disks(*, check_smart: bool) -> tuple[list[DiskStatus], list[DiskStatus]]:
    nvme: list[DiskStatus] = []
    hdd: list[DiskStatus] = []
    for disk in _wmi_physical_disks():
        media = disk.get("MediaType", 0)
        bus = (disk.get("BusType") or "").upper()
        model = (disk.get("Model") or "").upper()
        status = _windows_disk_health(disk, check_smart=check_smart)
        is_nvme = "NVME" in bus or "NVME" in model or media == 4 and "NVME" in model
        is_ssd = media == 4 or "SSD" in model or is_nvme
        if is_nvme or (is_ssd and "USB" not in bus):
            if is_nvme or "NVME" in model:
                nvme.append(status)
            else:
                # SATA SSD: still show under SSD icons if NVMe slots remain empty later
                nvme.append(status)
        elif "USB" in bus:
            continue
        else:
            hdd.append(status)
    return nvme, hdd


def _wmi_physical_disks() -> list[dict]:
    disks: list[dict] = []
    # Prefer Get-PhysicalDisk (Storage module)
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,"
                    "HealthStatus,OperationalStatus,DeviceId,Size | ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json

            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]
            for item in data or []:
                media = item.get("MediaType")
                # 3=HDD 4=SSD 0=Unspecified
                media_code = media if isinstance(media, int) else {"HDD": 3, "SSD": 4}.get(str(media), 0)
                disks.append(
                    {
                        "Model": str(item.get("FriendlyName") or ""),
                        "MediaType": media_code,
                        "BusType": str(item.get("BusType") or ""),
                        "HealthStatus": str(item.get("HealthStatus") or ""),
                        "OperationalStatus": str(item.get("OperationalStatus") or ""),
                        "DeviceId": str(item.get("DeviceId") or ""),
                        "Size": int(item.get("Size") or 0),
                    }
                )
            if disks:
                return disks
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    try:
        import wmi  # type: ignore

        c = wmi.WMI()
        for d in c.Win32_DiskDrive():
            disks.append(
                {
                    "Model": str(d.Model or ""),
                    "MediaType": 0,
                    "BusType": str(d.InterfaceType or ""),
                    "HealthStatus": str(d.Status or ""),
                    "OperationalStatus": str(d.Status or ""),
                    "DeviceId": str(d.DeviceID or ""),
                    "Size": int(d.Size or 0),
                }
            )
    except Exception:  # noqa: BLE001
        pass
    return disks


def _windows_disk_health(disk: dict, *, check_smart: bool) -> DiskStatus:
    health = (disk.get("HealthStatus") or "").lower()
    oper = str(disk.get("OperationalStatus") or "").lower()
    if "unhealthy" in health or "fail" in health or "error" in oper or "lost" in oper:
        return "error"
    if not check_smart:
        return "ok"
    if health in ("healthy", "warning", ""):
        return "ok" if "fail" not in oper else "error"
    return "ok"


def format_byte_rate(bytes_per_sec: int) -> str:
    bps = max(0, int(bytes_per_sec))
    if bps < 1024:
        return "0KB/s"
    kb = bps / 1024.0
    if kb < 1024:
        return f"{int(round(kb))}KB/s"
    mb = kb / 1024.0
    if mb < 1024:
        if mb < 10:
            return f"{mb:.1f}MB/s"
        return f"{int(round(mb))}MB/s"
    gb = mb / 1024.0
    return f"{gb:.1f}GB/s"


def network_rate_texts(sample_seconds: float = 0.5) -> dict[str, str]:
    down_bps, up_bps = network_throughput_bps(sample_seconds)
    total_bps = down_bps + up_bps
    return {
        "speed": format_byte_rate(total_bps),
        "upload": format_byte_rate(up_bps),
        "download": format_byte_rate(down_bps),
    }


def gpu_usage_and_temp() -> tuple[int, Optional[float]]:
    temp = None
    usage = None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            usages: list[int] = []
            temps: list[float] = []
            for line in result.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                try:
                    usages.append(int(float(parts[0])))
                    temps.append(float(parts[1]))
                except ValueError:
                    continue
            if usages:
                usage = max(usages)
                temp = max(temps) if temps else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (max(0, min(100, int(usage))) if usage is not None else 0), temp


def cpu_load_percent(sample_seconds: float = 0.5) -> int:
    try:
        import psutil

        value = psutil.cpu_percent(interval=sample_seconds)
        return max(0, min(100, int(round(value))))
    except ImportError:
        return 0


def _net_bytes_totals() -> tuple[int, int]:
    try:
        import psutil

        down = up = 0
        for name, counters in psutil.net_io_counters(pernic=True).items():
            if _is_virtual_iface(name):
                continue
            down += counters.bytes_recv
            up += counters.bytes_sent
        return down, up
    except ImportError:
        return 0, 0


def network_throughput_bps(sample_seconds: float = 1.0) -> tuple[int, int]:
    down1, up1 = _net_bytes_totals()
    time.sleep(max(0.2, sample_seconds))
    down2, up2 = _net_bytes_totals()
    span = max(sample_seconds, 0.2)
    return int(max(0, down2 - down1) / span), int(max(0, up2 - up1) / span)


def network_speed_display_value(
    *,
    mode: str = "download_kbps",
    sample_seconds: float = 1.0,
    max_kbps: int = 102400,
    max_mbps: int = 100,
) -> int:
    down_bps, up_bps = network_throughput_bps(sample_seconds)
    down_kbps = down_bps // 1024
    up_kbps = up_bps // 1024

    if mode == "percent_100m":
        mbps = down_bps * 8 / 1_000_000
        cap = max(max_mbps, 1)
        return max(0, min(100, int(round(mbps * 100 / cap))))
    if mode == "upload_kbps":
        value = up_kbps
    elif mode == "total_kbps":
        value = down_kbps + up_kbps
    elif mode == "percent":
        cap = max(max_kbps // 1024, 1)
        return max(0, min(100, int(round(down_kbps * 100 / cap))))
    else:
        value = down_kbps
    return max(0, min(65535, value))


def _is_virtual_iface(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("lo") or "loopback" in lowered:
        return True
    return any(marker in lowered for marker in _VIRTUAL_IFACE_MARKERS)


def _is_physical_iface(name: str) -> bool:
    if _is_virtual_iface(name):
        return False
    lowered = name.lower()
    # Prefer Ethernet / Wi-Fi style names on Windows
    if any(k in lowered for k in ("ethernet", "以太网", "local area", "wi-fi", "wifi", "wlan")):
        return True
    if any(lowered.startswith(p) for p in ("eth", "enp", "ens", "eno", "enx", "wlan", "wlp")):
        return True
    return False


def _iface_has_usable_ipv4(family: int, address: str) -> bool:
    if family != socket.AF_INET:
        return False
    if address.startswith("127.") or address == "0.0.0.0":
        return False
    if address.startswith("169.254."):
        return False
    return True


def has_physical_network_link() -> bool:
    try:
        import psutil

        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for name, st in stats.items():
            if not st.isup or not _is_physical_iface(name):
                continue
            for addr in addrs.get(name, []):
                if _iface_has_usable_ipv4(addr.family, addr.address):
                    return True
            return True
        return False
    except ImportError:
        return False


def has_internet_access(timeout: float = 2.0) -> bool:
    targets = (("223.5.5.5", 53), ("1.1.1.1", 443), ("8.8.8.8", 53))
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def display_ethernet_ifaces(slots: int = 2) -> list[str]:
    """Physical Ethernet adapters, Ethernet first then others."""
    slots = max(0, int(slots))
    try:
        import psutil

        stats = psutil.net_if_stats()
        names = [n for n in stats if _is_physical_iface(n) and "wi-fi" not in n.lower() and "wifi" not in n.lower() and "wlan" not in n.lower()]
        # Prefer names containing Ethernet
        names.sort(key=lambda n: (0 if "ethernet" in n.lower() or "以太网" in n else 1, n.lower()))
        # Prefer adapters that are up
        names.sort(key=lambda n: (0 if stats.get(n) and stats[n].isup else 1))
        return names[:slots] if slots else names
    except ImportError:
        return []


def resolve_display_iface(preferred: str, index: int, *, auto: bool = True) -> str:
    nics = display_ethernet_ifaces(slots=max(index + 1, 2))
    if auto:
        if 0 <= index < len(nics):
            return nics[index]
        return ""
    preferred = (preferred or "").strip()
    if preferred:
        try:
            import psutil

            if preferred in psutil.net_if_addrs():
                return preferred
        except ImportError:
            pass
    if 0 <= index < len(nics):
        return nics[index]
    return preferred


def iface_ipv4(name: str) -> str:
    if not name:
        return "0.0.0.0"
    try:
        import psutil

        for addr in psutil.net_if_addrs().get(name, []):
            if _iface_has_usable_ipv4(addr.family, addr.address):
                return addr.address
    except ImportError:
        pass
    return "0.0.0.0"


def iface_ipv4_wait(name: str, timeout: float = 8.0, interval: float = 0.5) -> str:
    timeout = max(0.0, float(timeout))
    interval = max(0.1, float(interval))
    ip = iface_ipv4(name)
    if ip != "0.0.0.0" or not name or timeout <= 0:
        return ip
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        ip = iface_ipv4(name)
        if ip != "0.0.0.0":
            return ip
    return ip


def network_connection_status() -> NetworkStatus:
    if not has_physical_network_link():
        return "Disconnected"
    if has_internet_access():
        return "connected"
    return "NoInternet"


def network_status_display_word(status: NetworkStatus | None = None) -> int:
    status = status or network_connection_status()
    return {"Disconnected": 0, "NoInternet": 1, "connected": 2}[status]


def _linear_points(value: float, good: float, bad: float, max_points: float) -> float:
    if value <= good:
        return max_points
    if value >= bad:
        return 0.0
    return max_points * (bad - value) / (bad - good)


def _disk_io_metrics() -> tuple[float, float, float]:
    try:
        import psutil

        first = psutil.disk_io_counters()
        time.sleep(0.5)
        second = psutil.disk_io_counters()
        if not first or not second:
            return 0.0, 0.0, 10.0
        read_ios = max(0, second.read_count - first.read_count)
        write_ios = max(0, second.write_count - first.write_count)
        busy_ms = max(0, getattr(second, "busy_time", 0) - getattr(first, "busy_time", 0))
        total_ios = max(1, read_ios + write_ios)
        latency_ms = busy_ms / total_ios if total_ios else 10.0
        busy_pct = min(100.0, busy_ms / 5.0) if busy_ms else min(100.0, total_ios * 2.0)
        queue = min(4.0, total_ios / 10.0)
        return queue, busy_pct, latency_ms
    except ImportError:
        return 0.0, 0.0, 10.0


def _network_ping_metrics(host: str = "223.5.5.5", count: int = 4) -> tuple[float, float]:
    try:
        # Windows: ping -n count -w timeout_ms
        result = subprocess.run(
            ["ping", "-n", str(count), "-w", "2000", host],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output = result.stdout + result.stderr
        loss_match = re.search(r"\((\d+)%\s*(?:loss|丢失)", output, re.I)
        if not loss_match:
            loss_match = re.search(r"(\d+)%\s*(?:loss|丢失)", output, re.I)
        avg_match = re.search(r"(?:Average|平均)\s*=\s*(\d+)\s*ms", output, re.I)
        if not avg_match:
            avg_match = re.search(r"Average =\s*(\d+)ms", output, re.I)
        loss = float(loss_match.group(1)) if loss_match else 100.0
        avg_ms = float(avg_match.group(1)) if avg_match else 999.0
        return loss, avg_ms
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 100.0, 999.0


def system_health_breakdown() -> dict[str, float]:
    cpu_load = cpu_load_percent(0.3)
    mem_used = memory_usage_percent()
    cpu_pts = 30.0 * (100.0 - cpu_load) / 100.0
    mem_pts = 25.0 * (100.0 - mem_used) / 100.0

    queue, busy, latency_ms = _disk_io_metrics()
    disk_queue_pts = _linear_points(queue, good=0.5, bad=4.0, max_points=10.0)
    disk_busy_pts = 10.0 * (100.0 - min(busy, 100.0)) / 100.0
    disk_latency_pts = _linear_points(latency_ms, good=10.0, bad=100.0, max_points=10.0)
    disk_pts = disk_queue_pts + disk_busy_pts + disk_latency_pts

    status = network_connection_status()
    if status == "Disconnected":
        net_conn_pts = 0.0
        loss, avg_ms = 100.0, 999.0
    elif status == "NoInternet":
        net_conn_pts = 2.0
        loss, avg_ms = 100.0, 999.0
    else:
        net_conn_pts = 5.0
        loss, avg_ms = _network_ping_metrics()

    net_loss_pts = 5.0 * (100.0 - min(loss, 100.0)) / 100.0
    net_latency_pts = _linear_points(avg_ms, good=30.0, bad=200.0, max_points=5.0)
    net_pts = net_conn_pts + net_loss_pts + net_latency_pts

    total = cpu_pts + mem_pts + disk_pts + net_pts
    return {
        "total": max(0.0, min(100.0, total)),
        "cpu": cpu_pts,
        "memory": mem_pts,
        "disk": disk_pts,
        "network": net_pts,
        "cpu_load": float(cpu_load),
        "mem_used": float(mem_used),
        "disk_queue": queue,
        "disk_busy": busy,
        "disk_latency_ms": latency_ms,
        "net_status": status,
        "net_loss": loss,
        "net_latency_ms": avg_ms,
    }


def system_health_score() -> int:
    return int(round(system_health_breakdown()["total"]))


def health_status_label(score: int | None = None) -> str:
    value = system_health_score() if score is None else int(score)
    if value >= 90:
        return "Excellent"
    if value >= 70:
        return "Normal"
    if value >= 50:
        return "Poor (Alert)"
    return "Critical"


def health_status_display_word(score: int | None = None) -> int:
    value = system_health_score() if score is None else int(score)
    if value >= 90:
        return 3
    if value >= 70:
        return 2
    if value >= 50:
        return 1
    return 0


def cpu_processor_queue_length() -> int:
    try:
        import psutil

        # Approximate runnable pressure from load / cpu count
        load = psutil.getloadavg()[0] if hasattr(psutil, "getloadavg") else psutil.cpu_percent(interval=0.1) / 100.0
        return max(0, min(65535, int(round(load))))
    except (ImportError, OSError, AttributeError):
        pass
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_PerfFormattedData_PerfOS_System).ProcessorQueueLength",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return max(0, min(65535, int(result.stdout.strip())))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return 0


def cpu_temperature_c() -> Optional[float]:
    # Try LibreHardwareMonitor / OpenHardwareMonitor WMI root
    for namespace in (
        "root\\LibreHardwareMonitor",
        "root\\OpenHardwareMonitor",
    ):
        temp = _ohm_cpu_temp(namespace)
        if temp is not None:
            return temp
    # MSAcpi thermal zone (often board zone, last resort)
    temp = _acpi_thermal_temp()
    return temp


def _ohm_cpu_temp(namespace: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    f"Get-CimInstance -Namespace '{namespace}' -ClassName Sensor "
                    "| Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU|Package|Tctl|Tdie' } "
                    "| Select-Object -ExpandProperty Value"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        values = []
        for line in result.stdout.splitlines():
            try:
                values.append(float(line.strip()))
            except ValueError:
                continue
        return max(values) if values else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _acpi_thermal_temp() -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                    "| Select-Object -First 1 -ExpandProperty CurrentTemperature"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        # Tenths of Kelvin
        raw = float(result.stdout.strip())
        celsius = raw / 10.0 - 273.15
        if 0 < celsius < 125:
            return celsius
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def cpu_temperature_display_value(
    *,
    mode: str = "celsius",
    temp_min_c: float = 30.0,
    temp_max_c: float = 100.0,
) -> int:
    temp_c = cpu_temperature_c()
    if temp_c is None:
        return 0
    if mode == "percent":
        span = max(temp_max_c - temp_min_c, 1.0)
        ratio = (temp_c - temp_min_c) / span
        return max(0, min(100, int(round(ratio * 100))))
    return max(0, min(999, int(round(temp_c))))
