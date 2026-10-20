"""Read system metrics on Linux for LEVETOP serial screen updater."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

DiskStatus = Literal["ok", "empty", "error"]

NetworkStatus = Literal["connected", "NoInternet", "Disconnected"]

_VIRTUAL_IFACE_MARKERS = (
    "lo",
    "loopback",
    "docker",
    "veth",
    "br-",
    "virbr",
    "vmnet",
    "vboxnet",
    "tun",
    "tap",
    "tailscale",
    "wg",
    "dummy",
)
_PHYSICAL_IFACE_MARKERS = (
    "eth",
    "enp",
    "ens",
    "eno",
    "enx",
    "wlan",
    "wlp",
    "wl",
    "bond",
    "team",
)

# Real CPU sensors first. acpitz is a coarse ACPI board zone and often stuck
# (this machine reports a constant 42C) — never prefer it over coretemp.
_CPU_TEMP_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")
_CPU_TEMP_ZONE_PREFER = ("x86_pkg_temp", "cpu_thermal", "cpu")
_CPU_TEMP_AVOID = (
    "acpitz",
    "nvme",
    "r8169",
    "iwlwifi",
    "spd5118",
    "amdgpu",
    "i915",
    "nouveau",
    "nvidia",
    "xe",
)


def memory_usage_percent() -> int:
    try:
        import psutil

        return max(0, min(100, int(round(psutil.virtual_memory().percent))))
    except ImportError:
        pass

    detail = memory_usage_detail()
    return int(detail["percent"])


def memory_usage_detail() -> dict[str, int | str]:
    """Return used/total RAM for screen text like 16GB/32GB plus percent."""
    total_b = used_b = 0
    percent = 0
    try:
        import psutil

        mem = psutil.virtual_memory()
        total_b = int(mem.total)
        used_b = int(mem.total - mem.available)
        percent = max(0, min(100, int(round(mem.percent))))
    except ImportError:
        try:
            info: dict[str, int] = {}
            with Path("/proc/meminfo").open(encoding="utf-8") as handle:
                for line in handle:
                    key, value = line.split(":", 1)
                    info[key.strip()] = int(value.split()[0])
            total_kb = info.get("MemTotal", 0)
            available_kb = info.get("MemAvailable", info.get("MemFree", 0))
            total_b = total_kb * 1024
            used_b = max(0, (total_kb - available_kb) * 1024)
            percent = 0 if total_kb <= 0 else max(0, min(100, int(round(used_b * 100.0 / total_b))))
        except (OSError, ValueError, KeyError):
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
    """NAS storage used/total for screen text like 12.6TB/24TB."""
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
    zpool = _storage_from_zpool()
    if zpool is not None:
        return zpool
    fs_used, fs_total = _storage_from_df()
    disk_total = _physical_disk_total_bytes()
    total = disk_total if disk_total >= 1_000_000_000_000 else max(disk_total, fs_total)
    used = fs_used
    if total <= 0:
        total = fs_total
    if used > total > 0:
        used = total
    return used, total


def _storage_from_zpool() -> Optional[tuple[int, int]]:
    try:
        result = subprocess.run(
            ["zpool", "list", "-Hp", "-o", "size,alloc"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    total = alloc = 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            total += int(float(parts[0]))
            alloc += int(float(parts[1]))
        except ValueError:
            continue
    if total <= 0:
        return None
    return alloc, total


def _storage_from_df() -> tuple[int, int]:
    skip_fs = {"tmpfs", "devtmpfs", "overlay", "squashfs", "efivarfs", "proc", "sysfs", "cgroup"}
    skip_mount_prefix = ("/boot", "/sys", "/proc", "/dev", "/run", "/snap")
    used = total = 0
    seen: set[str] = set()
    try:
        result = subprocess.run(
            ["df", "-B1", "-T", "-P"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        device, fstype, size_s, used_s, _avail, _pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
        if fstype.lower() in skip_fs:
            continue
        if any(mount == p or mount.startswith(p + "/") for p in skip_mount_prefix):
            continue
        if device in seen:
            continue
        try:
            size_b = int(size_s)
            used_b = int(used_s)
        except ValueError:
            continue
        seen.add(device)
        total += size_b
        used += used_b
    return used, total


SSD_SLOT_COUNT = 4
HDD_SLOT_COUNT = 6
_PCI_BDF_RE = re.compile(
    r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$"
)


def disk_bay_status(
    *,
    ssd_slot_pci_roots: Optional[list[str]] = None,
    hdd_slot_pci: Optional[str] = None,
    hdd_ata_ports: Optional[list[int]] = None,
    ssd_slots: int = SSD_SLOT_COUNT,
    hdd_slots: int = HDD_SLOT_COUNT,
    check_smart: bool = True,
) -> dict[str, list[DiskStatus]]:
    """Return per-bay status for motherboard M.2 SSDs and mechanical disks.

    ok = present and healthy, empty = not installed, error = present but failed.
    SSD bays map to NVMe controllers (optional PCI root-port order).
    HDD bays map to SATA ports on the controller that actually has disks;
    machines with two 6-port HBAs often put the cage on ata7-ata12, not ata1-ata6.
    """
    ssd_n = max(0, int(ssd_slots))
    hdd_n = max(0, int(hdd_slots))
    return {
        "ssd": _ssd_bay_status(ssd_n, ssd_slot_pci_roots, check_smart=check_smart),
        "hdd": _hdd_bay_status(
            hdd_n,
            hdd_slot_pci=hdd_slot_pci,
            hdd_ata_ports=hdd_ata_ports,
            check_smart=check_smart,
        ),
    }


def _ssd_bay_status(
    slot_count: int,
    pci_roots: Optional[list[str]],
    *,
    check_smart: bool = True,
) -> list[DiskStatus]:
    slots: list[DiskStatus] = ["empty"] * slot_count
    if slot_count <= 0:
        return slots
    nvmes = _nvme_controllers()
    used: set[str] = set()
    roots = [str(item).strip().lower() for item in (pci_roots or []) if str(item).strip()]
    for index, root in enumerate(roots[:slot_count]):
        for nvme in nvmes:
            if nvme["name"] in used:
                continue
            if _nvme_matches_root(nvme, root):
                slots[index] = _block_device_status(
                    nvme["dev"], nvme["block"], check_smart=check_smart
                )
                used.add(nvme["name"])
                break
    leftover = [item for item in nvmes if item["name"] not in used]
    leftover.sort(key=lambda item: (item.get("pci") or item["name"]))
    for nvme in leftover:
        try:
            index = slots.index("empty")
        except ValueError:
            break
        slots[index] = _block_device_status(nvme["dev"], nvme["block"], check_smart=check_smart)
        used.add(nvme["name"])
    return slots


def _nvme_controllers() -> list[dict[str, str]]:
    root = Path("/sys/class/nvme")
    if not root.is_dir():
        return []
    controllers: list[dict[str, str]] = []
    for ctrl in sorted(root.iterdir()):
        if not re.fullmatch(r"nvme\d+", ctrl.name):
            continue
        resolved = ctrl.resolve()
        pci_bits = [part for part in resolved.parts if _PCI_BDF_RE.match(part)]
        block = ""
        for ns in sorted(ctrl.glob("nvme*n*")):
            if re.fullmatch(r"nvme\d+n\d+", ns.name):
                block = ns.name
                break
        if not block:
            block_root = Path("/sys/block")
            if block_root.is_dir():
                for ns in sorted(block_root.glob(f"{ctrl.name}n*")):
                    block = ns.name
                    break
        controllers.append(
            {
                "name": ctrl.name,
                "dev": f"/dev/{ctrl.name}",
                "block": block,
                "pci": (pci_bits[-1] if pci_bits else _read_text(ctrl / "address")).lower(),
                "root": (pci_bits[0] if pci_bits else "").lower(),
                "path": str(resolved).lower(),
                "model": _read_text(ctrl / "model"),
            }
        )
    return controllers


def _nvme_matches_root(nvme: dict[str, str], root: str) -> bool:
    root = root.lower()
    if not root:
        return False
    return root in (nvme.get("path") or "") or nvme.get("pci") == root or nvme.get("root") == root


def _hdd_bay_status(
    slot_count: int,
    *,
    hdd_slot_pci: Optional[str] = None,
    hdd_ata_ports: Optional[list[int]] = None,
    check_smart: bool = True,
) -> list[DiskStatus]:
    slots: list[DiskStatus] = ["empty"] * slot_count
    if slot_count <= 0:
        return slots
    used_blocks: set[str] = set()
    entries = _ata_port_entries()
    if entries:
        port_list = _hdd_ata_ports_for_slots(
            entries,
            slot_count,
            hdd_slot_pci=hdd_slot_pci,
            hdd_ata_ports=hdd_ata_ports,
        )
        by_port = {item["port"]: str(item["block"]) for item in entries}
        for index, port_no in enumerate(port_list[:slot_count]):
            block = by_port.get(port_no, "")
            if block:
                slots[index] = _block_device_status(
                    f"/dev/{block}", block, check_smart=check_smart
                )
                used_blocks.add(block)

    leftovers = [name for name in _scsi_hdd_block_devices() if name not in used_blocks]
    for index, status in enumerate(slots):
        if status != "empty" or not leftovers:
            continue
        block = leftovers.pop(0)
        slots[index] = _block_device_status(f"/dev/{block}", block, check_smart=check_smart)
        used_blocks.add(block)
    if any(status != "empty" for status in slots):
        return slots

    disks = [name for name in _scsi_hdd_block_devices() if name not in used_blocks]
    for index, block in enumerate(disks[:slot_count]):
        slots[index] = _block_device_status(f"/dev/{block}", block, check_smart=check_smart)
    return slots


def _ata_port_entries() -> list[dict[str, str | int]]:
    """One record per ataN port: port number, sdX name (or ''), PCI controller."""
    port_root = Path("/sys/class/ata_port")
    if not port_root.is_dir():
        return []
    entries: list[dict[str, str | int]] = []
    for port in port_root.glob("ata*"):
        try:
            port_no = int(port.name.replace("ata", ""))
        except ValueError:
            continue
        entries.append(
            {
                "port": port_no,
                "block": _ata_port_sd_name(port),
                "pci": _ata_controller_id(port),
            }
        )
    entries.sort(key=lambda item: int(item["port"]))
    return entries


def _ata_port_block_devices() -> dict[int, str]:
    """ataN port number -> sdX name, empty string if the port has no disk."""
    return {int(item["port"]): str(item["block"]) for item in _ata_port_entries()}


def _ata_controller_id(port: Path) -> str:
    try:
        resolved = port.resolve()
    except OSError:
        return ""
    pci_bits = [part for part in resolved.parts if _PCI_BDF_RE.match(part)]
    return (pci_bits[-1] if pci_bits else "").lower()


def _ata_port_sd_name(port: Path) -> str:
    """Block device bound to this ATA port only; do not follow subsystem links."""
    device = port / "device"
    if not device.exists():
        return ""
    try:
        search_root = device.resolve()
    except OSError:
        search_root = device
    for host in sorted(search_root.glob("host*")):
        if host.is_symlink() or not host.is_dir():
            continue
        for target in sorted(host.glob("target*")):
            if target.is_symlink() or not target.is_dir():
                continue
            for scsi_dev in sorted(target.iterdir()):
                if scsi_dev.is_symlink() or not scsi_dev.is_dir():
                    continue
                block_dir = scsi_dev / "block"
                if not block_dir.is_dir():
                    continue
                for cand in sorted(block_dir.iterdir()):
                    name = cand.name
                    if name.startswith("sd") and name[2:].isalpha():
                        return name
    return ""


def _hdd_ata_ports_for_slots(
    entries: list[dict[str, str | int]],
    slot_count: int,
    *,
    hdd_slot_pci: Optional[str] = None,
    hdd_ata_ports: Optional[list[int]] = None,
) -> list[int]:
    """Pick ATA ports that correspond to the mechanical disk icons on the screen."""
    if hdd_ata_ports:
        ports: list[int] = []
        for item in hdd_ata_ports:
            try:
                ports.append(int(item))
            except (TypeError, ValueError):
                continue
        if ports:
            return ports[:slot_count]

    groups: dict[str, list[dict[str, str | int]]] = {}
    for item in entries:
        groups.setdefault(str(item.get("pci") or ""), []).append(item)
    for ports_in_group in groups.values():
        ports_in_group.sort(key=lambda item: int(item["port"]))

    wanted_pci = str(hdd_slot_pci or "").strip().lower()
    chosen: Optional[list[dict[str, str | int]]] = None
    if wanted_pci:
        for pci, ports_in_group in groups.items():
            if pci == wanted_pci or wanted_pci in pci:
                chosen = ports_in_group
                break
    def group_score(ports_in_group: list[dict[str, str | int]]) -> tuple[int, int]:
        disks = sum(1 for item in ports_in_group if item.get("block"))
        first = int(ports_in_group[0]["port"]) if ports_in_group else 0
        return (disks, -first)

    if chosen is None or not any(item.get("block") for item in chosen):
        nonempty = [group for group in groups.values() if any(item.get("block") for item in group)]
        if nonempty:
            chosen = max(nonempty, key=group_score)
        elif chosen is None:
            chosen = max(groups.values(), key=group_score) if groups else []

    return _select_ata_window(chosen or [], slot_count)


def _select_ata_window(group: list[dict[str, str | int]], slot_count: int) -> list[int]:
    ports = [int(item["port"]) for item in group]
    if not ports:
        return []
    if len(ports) <= slot_count:
        return ports
    occupied = {int(item["port"]) for item in group if item.get("block")}
    head = ports[:slot_count]
    if any(port in occupied for port in head):
        return head
    best_window = head
    best_count = 0
    for index in range(0, len(ports) - slot_count + 1):
        window = ports[index : index + slot_count]
        count = sum(1 for port in window if port in occupied)
        if count > best_count:
            best_count = count
            best_window = window
    return best_window


def _scsi_hdd_block_devices() -> list[str]:
    """SATA/SAS/scsi disks for mechanical bays, excluding NVMe and USB."""
    disks: list[tuple[str, str]] = []
    try:
        result = subprocess.run(
            ["lsblk", "-d", "-n", "-o", "NAME,TYPE,ROTA,TRAN,HCTL"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, dtype = parts[0], parts[1]
            rota = parts[2] if len(parts) > 2 else "1"
            tran = parts[3].lower() if len(parts) > 3 else ""
            hctl = parts[4] if len(parts) > 4 else name
            if dtype != "disk":
                continue
            if name.startswith(("nvme", "loop", "sr", "zram", "ram", "fd", "md", "dm-")):
                continue
            if tran in ("nvme", "usb"):
                continue
            if tran in ("sata", "sas", "ata", "spi") or name.startswith("sd"):
                disks.append((hctl, name))
            elif rota == "1":
                disks.append((hctl, name))
    if disks:
        disks.sort()
        return [name for _hctl, name in disks]

    block_root = Path("/sys/block")
    if not block_root.is_dir():
        return []
    names: list[str] = []
    for path in sorted(block_root.glob("sd*")):
        if path.name.startswith("sd") and path.name[2:].isalpha():
            names.append(path.name)
    return names


def _block_device_status(dev: str, block: str = "", *, check_smart: bool = True) -> DiskStatus:
    """Present disk: green unless SMART/md/sysfs reports a failure."""
    block = block or Path(dev).name
    if block.startswith("nvme") and not re.search(r"n\d+$", block):
        ns = Path("/sys/block")
        if ns.is_dir():
            for candidate in sorted(ns.glob(f"{block}n*")):
                block = candidate.name
                break
    if _md_faulty_names() & {block, Path(dev).name, re.sub(r"p?\d+$", "", block)}:
        return "error"
    state = _sysfs_block_state(block)
    if state in ("offline", "blocked", "transport-offline"):
        return "error"
    if not check_smart:
        return "ok"
    return _smart_health_status(dev, block)


def _sysfs_block_state(block: str) -> str:
    if not block:
        return ""
    for rel in ("device/state", "device/device/state"):
        path = Path("/sys/block") / block / rel
        if path.is_file():
            return _read_text(path).lower()
    return ""


def _md_faulty_names() -> set[str]:
    try:
        text = Path("/proc/mdstat").read_text(encoding="utf-8")
    except OSError:
        return set()
    names: set[str] = set()
    for match in re.finditer(r"([A-Za-z0-9_]+)\[\d+\]\(F\)", text):
        names.add(match.group(1))
        names.add(re.sub(r"p\d+$", "", match.group(1)))
        names.add(re.sub(r"\d+$", "", match.group(1)))
    return names


def _smart_health_status(dev: str, block: str = "") -> DiskStatus:
    targets = [dev]
    if block:
        ns_dev = f"/dev/{block}"
        if ns_dev not in targets:
            targets.append(ns_dev)
    last_unknown = False
    for target in targets:
        if not Path(target).exists():
            continue
        try:
            result = subprocess.run(
                ["smartctl", "-H", "-n", "standby", target],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            last_unknown = True
            continue
        text = f"{result.stdout}\n{result.stderr}"
        if re.search(r"self-assessment test result:\s*FAILED", text, re.I):
            return "error"
        if re.search(r"SMART Health Status:\s*FAILED", text, re.I):
            return "error"
        if result.returncode & 0x08:
            return "error"
        if re.search(r"self-assessment test result:\s*PASSED", text, re.I):
            return "ok"
        if "STANDBY" in text.upper() or "sleeping" in text.lower():
            return "ok"
        last_unknown = True
    critical = _nvme_sysfs_critical_warning(dev, block)
    if critical:
        return "error"
    if last_unknown:
        return "ok"
    return "ok"


def _nvme_sysfs_critical_warning(dev: str, block: str) -> bool:
    names = [Path(dev).name, block]
    for name in names:
        match = re.match(r"(nvme\d+)", name)
        if not match:
            continue
        ctrl = Path("/sys/class/nvme") / match.group(1)
        for rel in (
            "device/critical_warning",
            "critical_warning",
            f"{name}/critical_warning",
        ):
            path = ctrl / rel if not rel.startswith(name) else Path("/sys/class/nvme") / rel
            if path.is_file():
                try:
                    return int(_read_text(path), 0) != 0
                except ValueError:
                    continue
    return False


def _physical_disk_total_bytes() -> int:
    total = 0
    try:
        result = subprocess.run(
            ["lsblk", "-b", "-d", "-n", "-o", "NAME,SIZE,TYPE"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, size_s, dtype = parts[0], parts[1], parts[2]
        if dtype != "disk":
            continue
        if name.startswith(("loop", "sr", "zram", "ram", "fd")):
            continue
        try:
            total += int(size_s)
        except ValueError:
            continue
    return total


def format_byte_rate(bytes_per_sec: int) -> str:
    """Human network rate for 字符型, e.g. 12KB/s or 1.2MB/s."""
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


def gpu_usage_percent() -> int:
    usage, _temp = gpu_usage_and_temp()
    return usage


def gpu_temperature_c() -> Optional[float]:
    _usage, temp = gpu_usage_and_temp()
    return temp


def gpu_usage_and_temp() -> tuple[int, Optional[float]]:
    """Return GPU busy 0-100 and discrete-GPU temperature C (None if no dGPU)."""
    temp = _read_discrete_gpu_temp_c()
    for reader in (_gpu_nvidia, _gpu_intel_top):
        try:
            usage, reader_temp = reader()
        except Exception:  # noqa: BLE001
            continue
        if usage is None and reader_temp is None:
            continue
        if reader_temp is not None:
            temp = reader_temp
        if usage is not None:
            return max(0, min(100, int(usage))), temp
    return 0, temp


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _pci_address(device: Path) -> str:
    try:
        return device.resolve().name
    except OSError:
        return ""


def _is_discrete_gpu(vendor: str, pci: str) -> bool:
    """True for add-in GPUs. Intel/AMD iGPUs on PCI bus 0 are not discrete."""
    vendor = vendor.lower()
    if vendor == "0x10de":
        return True
    if pci.startswith("0000:00:"):
        return False
    return vendor in ("0x1002", "0x8086")


def _iter_discrete_drm_cards() -> list[Path]:
    drm = Path("/sys/class/drm")
    if not drm.is_dir():
        return []
    cards: list[Path] = []
    for card in drm.glob("card*"):
        if "-" in card.name:
            continue
        vendor = _read_text(card / "device" / "vendor")
        pci = _pci_address(card / "device")
        if _is_discrete_gpu(vendor, pci):
            cards.append(card)
    return cards


def _read_discrete_gpu_temp_c() -> Optional[float]:
    """Temperature of a plugged-in discrete GPU only. No iGPU / CPU fallback."""
    for name, _label, temp_c in _iter_hwmon_temps():
        if name in ("nvidia", "nouveau"):
            return temp_c

    temps: list[float] = []
    for card in _iter_discrete_drm_cards():
        for input_file in card.glob("device/hwmon/hwmon*/temp*_input"):
            try:
                temp_c = int(input_file.read_text(encoding="utf-8").strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if temp_c > 0:
                temps.append(temp_c)
    if temps:
        return max(temps)
    return None


def _gpu_nvidia() -> tuple[Optional[int], Optional[float]]:
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
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
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
    if not usages:
        return None, None
    return max(usages), max(temps) if temps else None


def _gpu_intel_top() -> tuple[Optional[int], Optional[float]]:
    try:
        result = subprocess.run(
            ["intel_gpu_top", "-J", "-s", "250", "-o", "-"],
            capture_output=True,
            text=True,
            timeout=3.5,
            check=False,
        )
        text = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        text = out + err
    except OSError:
        return None, None
    busies = [float(x) for x in re.findall(r'"busy"\s*:\s*([\d.]+)', text)]
    if not busies:
        return None, None
    return max(0, min(100, int(round(max(busies))))), None


def cpu_load_percent(sample_seconds: float = 0.5) -> int:
    try:
        import psutil

        value = psutil.cpu_percent(interval=sample_seconds)
        return max(0, min(100, int(round(value))))
    except ImportError:
        pass

    try:
        with Path("/proc/stat").open(encoding="utf-8") as handle:
            first = _parse_proc_stat_cpu(handle.readline())
        time.sleep(max(0.2, sample_seconds))
        with Path("/proc/stat").open(encoding="utf-8") as handle:
            second = _parse_proc_stat_cpu(handle.readline())
        idle_delta = second[3] - first[3]
        total_delta = sum(b - a for a, b in zip(first, second))
        if total_delta <= 0:
            return 0
        usage = 100.0 * (1.0 - idle_delta / total_delta)
        return max(0, min(100, int(round(usage))))
    except (OSError, ValueError, IndexError):
        return 0


def _parse_proc_stat_cpu(line: str) -> tuple[int, ...]:
    parts = line.split()
    if parts[0] != "cpu":
        raise ValueError("not aggregate cpu line")
    return tuple(int(x) for x in parts[1:8])


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
    if lowered == "lo" or lowered.startswith("lo"):
        return True
    return any(marker in lowered for marker in _VIRTUAL_IFACE_MARKERS if marker != "lo")


def _is_physical_iface(name: str) -> bool:
    if _is_virtual_iface(name):
        return False
    lowered = name.lower()
    return any(lowered.startswith(marker) or marker in lowered for marker in _PHYSICAL_IFACE_MARKERS)


def _iface_has_usable_ipv4(family: int, address: str) -> bool:
    if family != socket.AF_INET:
        return False
    if address.startswith("127.") or address == "0.0.0.0":
        return False
    if address.startswith("169.254."):
        return False
    return True


def _has_physical_link_psutil() -> bool:
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


def _has_physical_link_sysfs() -> bool:
    net_root = Path("/sys/class/net")
    if not net_root.is_dir():
        return False
    for iface in net_root.iterdir():
        name = iface.name
        if not _is_physical_iface(name):
            continue
        operstate = iface / "operstate"
        if operstate.is_file():
            state = operstate.read_text(encoding="utf-8").strip().lower()
            if state in ("up", "unknown"):
                return True
    return False


def has_physical_network_link() -> bool:
    if _has_physical_link_psutil():
        return True
    return _has_physical_link_sysfs()


def has_internet_access(timeout: float = 2.0) -> bool:
    targets = (("223.5.5.5", 53), ("1.1.1.1", 443), ("8.8.8.8", 53))
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _pci_sort_key(pci: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in pci.replace(".", ":").split(":"):
        try:
            parts.append(int(token, 16))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _iface_pci_id(name: str) -> str:
    device = Path("/sys/class/net", name, "device")
    try:
        if device.exists():
            return device.resolve().name.lower()
    except OSError:
        pass
    return name.lower()


def _ethernet_ifaces(prefixes: tuple[str, ...]) -> list[str]:
    net_root = Path("/sys/class/net")
    if not net_root.is_dir():
        return []
    items: list[tuple[tuple[int, ...], str, str]] = []
    for iface in net_root.iterdir():
        name = iface.name
        lowered = name.lower()
        if _is_virtual_iface(name):
            continue
        if not lowered.startswith(prefixes):
            continue
        pci = _iface_pci_id(name)
        items.append((_pci_sort_key(pci), pci, name))
    items.sort()
    return [name for _key, _pci, name in items]


def onboard_ethernet_ifaces() -> list[str]:
    """Board Ethernet ports in PCI order, excluding USB (enx) and virtual NICs."""
    return _ethernet_ifaces(("enp", "ens", "eno", "eth"))


def display_ethernet_ifaces(slots: int = 2) -> list[str]:
    """NICs shown on the panel: onboard PCI order, then USB Ethernet if slots remain."""
    slots = max(0, int(slots))
    nics = onboard_ethernet_ifaces()
    if len(nics) < slots:
        for name in _ethernet_ifaces(("enx",)):
            if name not in nics:
                nics.append(name)
            if len(nics) >= slots:
                break
    return nics[:slots] if slots else nics


def _iface_exists(name: str) -> bool:
    return bool(name) and Path("/sys/class/net", name).is_dir()


def _iface_operstate(name: str) -> str:
    path = Path("/sys/class/net", name, "operstate")
    try:
        return path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def resolve_display_iface(preferred: str, index: int, *, auto: bool = True) -> str:
    """Map panel net1/net2 to this machine's Ethernet.

    auto=True (default): ignore configured names and use onboard NIC PCI order,
    so the same code works on machines whose iface names differ.
    auto=False: use preferred if that NIC exists, else fall back to PCI order.
    """
    nics = display_ethernet_ifaces(slots=max(index + 1, 2))
    if auto:
        if 0 <= index < len(nics):
            return nics[index]
        return ""
    preferred = (preferred or "").strip()
    if preferred and _iface_exists(preferred):
        return preferred
    if 0 <= index < len(nics):
        return nics[index]
    return preferred


def iface_ipv4(name: str) -> str:
    """Return the first usable IPv4 of a NIC, or 0.0.0.0 if none."""
    if not name:
        return "0.0.0.0"

    try:
        import psutil

        for addr in psutil.net_if_addrs().get(name, []):
            family = addr.family
            if family != socket.AF_INET and family != getattr(socket, "AF_INET", 2):
                continue
            if _iface_has_usable_ipv4(socket.AF_INET, addr.address):
                return addr.address
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout)
        if match and _iface_has_usable_ipv4(socket.AF_INET, match.group(1)):
            return match.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass

    return "0.0.0.0"


def iface_ipv4_wait(name: str, timeout: float = 8.0, interval: float = 0.5) -> str:
    """Wait briefly for DHCP when the NIC has link but no IPv4 yet."""
    timeout = max(0.0, float(timeout))
    interval = max(0.1, float(interval))
    ip = iface_ipv4(name)
    if ip != "0.0.0.0" or not name or timeout <= 0:
        return ip
    if _iface_operstate(name) not in ("up", "unknown"):
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


def _disk_io_metrics_iostat() -> Optional[tuple[float, float, float]]:
    try:
        result = subprocess.run(
            ["iostat", "-dx", "1", "2"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        device_lines: list[str] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("linux"):
                continue
            if stripped.lower().startswith("device"):
                device_lines = []
                continue
            if re.match(r"^(sd|nvme|vd|xvd|mmcblk)", stripped, re.I):
                device_lines.append(stripped)

        if not device_lines:
            return None

        queue_vals: list[float] = []
        busy_vals: list[float] = []
        await_vals: list[float] = []
        for line in device_lines:
            parts = line.split()
            if len(parts) < 14:
                continue
            try:
                r_await = float(parts[9])
                w_await = float(parts[10])
                queue_vals.append(float(parts[11]))
                busy_vals.append(float(parts[-1]))
                await_vals.append(max(r_await, w_await))
            except ValueError:
                continue

        if not queue_vals:
            return None
        return (
            sum(queue_vals) / len(queue_vals),
            sum(busy_vals) / len(busy_vals),
            sum(await_vals) / len(await_vals),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _disk_io_metrics_proc() -> tuple[float, float, float]:
    try:
        import psutil

        first = psutil.disk_io_counters()
        time.sleep(0.5)
        second = psutil.disk_io_counters()
        if not first or not second:
            return 0.0, 0.0, 10.0

        read_ios = max(0, second.read_count - first.read_count)
        write_ios = max(0, second.write_count - first.write_count)
        busy_ms = max(0, second.busy_time - first.busy_time)
        total_ios = max(1, read_ios + write_ios)
        latency_ms = busy_ms / total_ios if total_ios else 10.0
        busy_pct = min(100.0, busy_ms / 5.0)
        queue = min(4.0, total_ios / 10.0)
        return queue, busy_pct, latency_ms
    except ImportError:
        return 0.0, 0.0, 10.0


def _disk_io_metrics() -> tuple[float, float, float]:
    iostat = _disk_io_metrics_iostat()
    if iostat is not None:
        return iostat
    return _disk_io_metrics_proc()


def _network_ping_metrics(host: str = "223.5.5.5", count: int = 4) -> tuple[float, float]:
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        loss_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet loss|packet loss,)", output, re.I)
        if not loss_match:
            loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*loss", output, re.I)
        avg_match = re.search(
            r"(?:rtt min/avg/max/(?:mdev|stddev)\s*=\s*[\d.]+/)([\d.]+)/",
            output,
            re.I,
        )
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
    """Runnable threads waiting for CPU (from /proc/loadavg)."""
    try:
        parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        runnable = int(parts[3].split("/")[0])
        return max(0, min(65535, runnable))
    except (OSError, ValueError, IndexError):
        pass

    try:
        load1 = os.getloadavg()[0]  # type: ignore[name-defined]
        return max(0, min(65535, int(round(load1 * 100))))
    except OSError:
        return 0


def _iter_hwmon_temps() -> list[tuple[str, str, float]]:
    root = Path("/sys/class/hwmon")
    if not root.is_dir():
        return []

    readings: list[tuple[str, str, float]] = []
    for hwmon in root.glob("hwmon*"):
        name = ""
        name_file = hwmon / "name"
        if name_file.is_file():
            try:
                name = name_file.read_text(encoding="utf-8").strip().lower()
            except OSError:
                name = ""
        for input_file in hwmon.glob("temp*_input"):
            try:
                temp_c = int(input_file.read_text(encoding="utf-8").strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if temp_c <= 0:
                continue
            label = ""
            label_file = input_file.with_name(input_file.name.replace("_input", "_label"))
            if label_file.is_file():
                try:
                    label = label_file.read_text(encoding="utf-8").strip().lower()
                except OSError:
                    label = ""
            readings.append((name, label, temp_c))
    return readings


def _pick_cpu_hwmon_temp(readings: list[tuple[str, str, float]]) -> Optional[float]:
    if not readings:
        return None

    package = [
        t
        for n, label, t in readings
        if n == "coretemp" and "package" in label
    ]
    if package:
        return max(package)

    for chip in _CPU_TEMP_CHIPS:
        labeled = [(label, t) for n, label, t in readings if n == chip]
        if not labeled:
            continue
        for prefer_label in ("tdie", "tctl", "tccd"):
            hits = [t for label, t in labeled if prefer_label in label]
            if hits:
                return max(hits)
        return max(t for _label, t in labeled)

    cpu_like = [
        t
        for n, _label, t in readings
        if n not in _CPU_TEMP_AVOID and any(k in n for k in ("core", "cpu", "k10", "zen"))
    ]
    if cpu_like:
        return max(cpu_like)
    return None


def _read_thermal_zone_temp_c() -> Optional[float]:
    root = Path("/sys/class/thermal")
    if not root.is_dir():
        return None

    readings: list[tuple[str, float]] = []
    for zone in root.glob("thermal_zone*"):
        temp_file = zone / "temp"
        if not temp_file.is_file():
            continue
        try:
            temp_c = int(temp_file.read_text(encoding="utf-8").strip()) / 1000.0
            zone_type = ""
            type_file = zone / "type"
            if type_file.is_file():
                zone_type = type_file.read_text(encoding="utf-8").strip().lower()
            readings.append((zone_type, temp_c))
        except (OSError, ValueError):
            continue

    if not readings:
        return None

    for prefer in _CPU_TEMP_ZONE_PREFER:
        for zone_type, temp_c in readings:
            if prefer in zone_type and zone_type not in _CPU_TEMP_AVOID:
                return temp_c
    others = [t for zone_type, t in readings if zone_type not in _CPU_TEMP_AVOID]
    if others:
        return max(others)
    return None


def _read_hwmon_temp_c() -> Optional[float]:
    return _pick_cpu_hwmon_temp(_iter_hwmon_temps())


def _read_sensors_c() -> Optional[float]:
    try:
        result = subprocess.run(
            ["sensors", "-u"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return None
        values = [float(x) for x in re.findall(r"temp\d+_input:\s*([\d.]+)", result.stdout)]
        if not values:
            values = [float(x) for x in re.findall(r":\s*\+?([\d.]+)(?:\s|°)", result.stdout)]
        return max(values) if values else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def cpu_temperature_c() -> Optional[float]:
    for reader in (_read_hwmon_temp_c, _read_thermal_zone_temp_c, _read_sensors_c):
        temp = reader()
        if temp is not None:
            return temp
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
