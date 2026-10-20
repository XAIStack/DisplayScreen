#!/usr/bin/env python3
"""
Push system time, CPU load and memory usage to a LEVETOP serial screen via serial port.

Digital clocks: screen RTC 0x7002..0x7007 plus optional writeAddr (clock_vars).
Progress: 0x1003 CPU load, 0x0004 memory. Speed: 0x1005. Status: 0x1008.
Backlight: system var 0x7001 (on while host runs; off on process exit/signal).
Page switch: system var 0x7000 (default page 4, then 5-9 carousel).
NIC IPs: string controls 0x1116 (net1) and 0x112A (net2).

Protocol: UI_Editor-II_CH_V3.2 (frame 0x5AA5, cmd 0x10, Modbus CRC16).
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import serial

from levetop_protocol import (
    build_read_words,
    build_write_dword,
    build_write_packed_ascii,
    build_write_string,
    build_write_word,
    build_write_words,
    describe_feedback,
    format_hex,
    parse_read_words,
)
from system_metrics import (
    cpu_load_percent,
    cpu_processor_queue_length,
    cpu_temperature_c,
    cpu_temperature_display_value,
    disk_bay_status,
    gpu_usage_and_temp,
    health_status_display_word,
    health_status_label,
    display_ethernet_ifaces,
    iface_ipv4,
    iface_ipv4_wait,
    memory_usage_detail,
    memory_usage_percent,
    network_connection_status,
    network_rate_texts,
    network_speed_display_value,
    network_status_display_word,
    resolve_display_iface,
    storage_usage_detail,
    system_health_breakdown,
)

LOG = logging.getLogger("screen_updater")
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_SERIAL_PORT = "/dev/ttyUSB1"
LED_DEFAULT_PORT = "/dev/ttyUSB0"
_PACKED_CLOCK_FMTS = frozenset({"time_hms_packed", "date_ymd_packed"})

_RUNTIME: dict[str, Any] = {
    "cfg": None,
    "ser": None,
    "dry_run": False,
    "stop": threading.Event(),
    "dimmed": False,
    "lock": threading.RLock(),
    "page5_cpu": None,
    "page6_gpu": None,
    "page7_mem": None,
    "page8_store": None,
    "page9_disks": None,
    "led_mode": None,
    "led_lock": threading.RLock(),
}

DEFAULT_PAGE_CYCLE = [
    {"page": 4, "seconds": 10},
    {"page": 5, "seconds": 2},
    {"page": 6, "seconds": 2},
    {"page": 7, "seconds": 2},
    {"page": 8, "seconds": 2},
    {"page": 9, "seconds": 2},
]


def parse_addr(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def backlight_addr(cfg: dict) -> int:
    return parse_addr(cfg.get("backlight_addr"), 0x7001)


def backlight_on_value(cfg: dict) -> int:
    return int(cfg.get("backlight_on", 0xFF)) & 0xFFFF


def backlight_off_value(cfg: dict) -> int:
    return int(cfg.get("backlight_off", 0)) & 0xFFFF


def backlight_packet(cfg: dict, value: int) -> tuple[bytes, str]:
    addr = backlight_addr(cfg)
    value &= 0xFFFF
    return build_write_word(addr, value), f"backlight@{addr:#06x}={value}"


def write_backlight(
    ser: Optional[serial.Serial],
    cfg: dict,
    value: int,
    *,
    dry_run: bool = False,
    log_feedback: bool = True,
) -> None:
    packet, label = backlight_packet(cfg, value)
    LOG.info("%s -> %s", label, format_hex(packet))
    if dry_run or ser is None:
        return
    send_packet(ser, packet, label, log_feedback=log_feedback)


def dim_screen_now(reason: str) -> None:
    with _RUNTIME["lock"]:
        if _RUNTIME["dimmed"]:
            return
        cfg = _RUNTIME.get("cfg")
        if not cfg or not cfg.get("backlight_dim_on_shutdown", True):
            _RUNTIME["stop"].set()
            return

        off = backlight_off_value(cfg)
        dry_run = bool(_RUNTIME.get("dry_run"))
        ser = _RUNTIME.get("ser")
        try:
            if dry_run:
                write_backlight(None, cfg, off, dry_run=True)
            elif ser is not None and getattr(ser, "is_open", False):
                write_backlight(ser, cfg, off, dry_run=False, log_feedback=False)
            else:
                port = cfg.get("serial_port", DEFAULT_SERIAL_PORT)
                baud = int(cfg.get("baudrate", 115200))
                with open_serial(port, baud) as tmp:
                    write_backlight(tmp, cfg, off, dry_run=False, log_feedback=False)
            _RUNTIME["dimmed"] = True
            LOG.info("Backlight dimmed (%s)", reason)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Failed to dim backlight on %s: %s", reason, exc)
        finally:
            _RUNTIME["stop"].set()


def _install_signal_hooks() -> None:
    def _handler(signum: int, _frame: object) -> None:
        dim_screen_now(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def screen_rtc_packet(now: datetime) -> bytes:
    """Write year..second to registers 0x7002..0x7007 (digital clock data source)."""
    return build_write_words(
        0x7002,
        [now.year % 100, now.month, now.day, now.hour, now.minute, now.second],
    )


def screen_rtc_label(now: datetime) -> str:
    return now.strftime("screen RTC 0x7002: %y-%m-%d %H:%M:%S")


def legacy_string_rtc_packets(now: datetime, cfg: dict) -> list[tuple[bytes, str]]:
    """Legacy: string/word writes to per-control writeAddr (not for digital clocks)."""
    encoding = cfg.get("string_encoding", "gb2312")
    weekday_names: list[str] = cfg.get("weekday_names", [])
    packets: list[tuple[bytes, str]] = []

    rtc0_addr = int(cfg["rtc_0_addr"])
    if cfg.get("rtc_0_mode", "string") == "string":
        text = now.strftime(cfg.get("rtc_0_format", "%H:%M:%S"))
        packets.append((build_write_string(rtc0_addr, text, encoding=encoding), f"RTC_0={text}"))
    else:
        packed = (now.hour << 8) | now.minute
        packets.append((build_write_word(rtc0_addr, packed), f"RTC_0 word={packed:#06x}"))

    rtc1_addr = int(cfg["rtc_1_addr"])
    if cfg.get("rtc_1_mode", "string") == "string":
        text = now.strftime(cfg.get("rtc_1_format", "%Y-%m-%d"))
        packets.append((build_write_string(rtc1_addr, text, encoding=encoding), f"RTC_1={text}"))
    else:
        packed = ((now.year % 100) << 8) | now.month
        packets.append((build_write_word(rtc1_addr, packed), f"RTC_1 word={packed:#06x}"))

    rtc2_addr = int(cfg["rtc_2_addr"])
    weekday = now.weekday()
    if cfg.get("rtc_2_mode", "word") == "string":
        name = weekday_names[weekday] if weekday_names else str(weekday)
        packets.append((build_write_string(rtc2_addr, name, encoding=encoding), f"RTC_2={name}"))
    else:
        value = (weekday + 1) % 7
        packets.append((build_write_word(rtc2_addr, value), f"RTC_2 weekday={value}"))

    return packets


def encode_clock_var(now: datetime, fmt: str) -> int:
    """Packed values for digital clocks bound to writeAddr (not internal RTC)."""
    year = now.year % 100
    if fmt == "time_hms_packed":
        return now.hour * 10000 + now.minute * 100 + now.second
    if fmt == "date_ymd_packed":
        return year * 10000 + now.month * 100 + now.day
    if fmt == "weekday":
        return (now.weekday() + 1) % 7
    if fmt == "time_hm_word":
        return (now.hour << 8) | now.minute
    if fmt == "date_ym_word":
        return (year << 8) | now.month
    if fmt == "year":
        return year
    if fmt == "month":
        return now.month
    if fmt == "day":
        return now.day
    if fmt == "hour":
        return now.hour
    if fmt == "minute":
        return now.minute
    if fmt == "second":
        return now.second
    raise ValueError(f"Unknown clock var format: {fmt!r}")


def clock_var_packets(now: datetime, cfg: dict) -> list[tuple[bytes, str]]:
    packets: list[tuple[bytes, str]] = []
    for item in cfg.get("clock_vars", []):
        if item.get("addr") is None:
            continue
        addr = parse_addr(item.get("addr"))
        fmt = item.get("format", "time_hms_packed")
        label = item.get("label", f"clock@{addr:#06x}")
        value = encode_clock_var(now, fmt)
        if fmt in _PACKED_CLOCK_FMTS or value > 0xFFFF:
            packets.append(
                (
                    build_write_dword(addr, value),
                    f"{label}={value} ({fmt} u32 @{addr:#06x}..{addr + 1:#06x})",
                )
            )
        else:
            packets.append((build_write_word(addr, value), f"{label}={value} ({fmt})"))
    return packets


def screen_rtc_register_packets(now: datetime, cfg: dict) -> list[tuple[bytes, str]]:
    style = cfg.get("rtc_write_style", "batch")
    packets: list[tuple[bytes, str]] = []

    if style == "batch":
        packets.append((screen_rtc_packet(now), screen_rtc_label(now)))
    elif style == "split_lt168b":
        for addr, value, name in _rtc_register_values(now):
            packets.append((build_write_word(addr, value), f"RTC {name} {addr:#06x}={value}"))
    else:
        raise ValueError(f"Unknown rtc_write_style: {style!r}")

    if cfg.get("rtc_confirm_7008"):
        packets.append((build_write_word(0x7008, 1), "RTC confirm 0x7008=1"))
    return packets


def _rtc_register_values(now: datetime) -> list[tuple[int, int, str]]:
    year = now.year % 100
    return [
        (0x7002, year, "year"),
        (0x7003, now.month, "month"),
        (0x7004, now.day, "day"),
        (0x7005, now.hour, "hour"),
        (0x7006, now.minute, "minute"),
        (0x7007, now.second, "second"),
    ]


def clock_packets(now: datetime, cfg: dict) -> list[tuple[bytes, str]]:
    mode = cfg.get("clock_mode", "hybrid")
    if cfg.get("sync_screen_rtc_register") and mode == "legacy_string":
        mode = "hybrid"

    packets: list[tuple[bytes, str]] = []

    if mode in ("digital_screen_rtc", "hybrid"):
        packets.extend(screen_rtc_register_packets(now, cfg))
    if mode in ("hybrid", "clock_vars"):
        packets.extend(clock_var_packets(now, cfg))
    if mode == "legacy_string":
        return legacy_string_rtc_packets(now, cfg)
    if mode == "clock_vars":
        return clock_var_packets(now, cfg)

    if not packets:
        raise ValueError(f"Unknown clock_mode: {mode!r}")
    return packets


def probe_screen_rtc(ser: serial.Serial) -> None:
    """Read back internal RTC registers 0x7002..0x7007."""
    packet = build_read_words(0x7002, 6)
    ser.reset_input_buffer()
    ser.write(packet)
    ser.flush()
    time.sleep(0.12)
    response = ser.read(128)
    LOG.info("Read RTC -> %s", format_hex(packet))
    LOG.info("Read RTC <- %s", format_hex(response))
    values = parse_read_words(response)
    if len(values) >= 6:
        LOG.info(
            "RTC in screen RAM: 20%02d-%02d-%02d %02d:%02d:%02d",
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
        )
    else:
        LOG.warning("Could not parse RTC read response (widget may not use 0x7002)")


def send_packet(ser: serial.Serial, packet: bytes, label: str, *, log_feedback: bool = True) -> None:
    with _RUNTIME["lock"]:
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()
        if not log_feedback:
            time.sleep(0.05)
            return
        time.sleep(0.15)
        response = ser.read(64)
        if response:
            LOG.info("  <- %s", describe_feedback(response))
        else:
            LOG.warning("  <- (no response, check wiring/baud/port permissions)")


def serial_candidates(cfg: dict) -> list[str]:
    led_port = str(cfg.get("led_serial_port") or LED_DEFAULT_PORT)
    ports: list[str] = []
    extra = cfg.get("serial_ports")
    if isinstance(extra, list):
        ports.extend(str(item) for item in extra)
    primary = cfg.get("serial_port")
    if primary:
        ports.append(str(primary))
    for port in ("/dev/ttyUSB1", "/dev/ttyUSB0"):
        ports.append(port)
    unique: list[str] = []
    for port in ports:
        if port == led_port:
            continue
        if port not in unique:
            unique.append(port)
    return unique


def open_serial(port: str, baud: int) -> serial.Serial:
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.4,
        write_timeout=5,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    try:
        ser.dtr = True
        ser.rts = True
    except (OSError, ValueError, AttributeError):
        pass
    time.sleep(0.05)
    return ser


def probe_port_ack(port: str, baud: int, cfg: dict) -> tuple[bool, str]:
    try:
        ser = open_serial(port, baud)
    except Exception as exc:  # noqa: BLE001
        return False, f"open failed: {exc}"
    try:
        packet, _ = backlight_packet(cfg, backlight_on_value(cfg))
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()
        time.sleep(0.2)
        response = ser.read(64)
        if response:
            return True, describe_feedback(response)
        return False, "no ACK"
    finally:
        ser.close()


def resolve_serial_port(cfg: dict) -> str:
    baud = int(cfg.get("baudrate", 115200))
    candidates = serial_candidates(cfg)
    LOG.info("Probing serial ports: %s", ", ".join(candidates))
    first_open: Optional[str] = None
    for port in candidates:
        if not Path(port).exists():
            LOG.info("Skip missing %s", port)
            continue
        ok, detail = probe_port_ack(port, baud, cfg)
        LOG.info("Probe %s -> %s", port, detail)
        if ok:
            cfg["serial_port"] = port
            return port
        if first_open is None:
            first_open = port
    if first_open:
        LOG.warning("No ACK on any port; using %s (screen may be TX-only)", first_open)
        cfg["serial_port"] = first_open
        return first_open
    raise FileNotFoundError(f"No serial port found among {candidates}")


def page_addr(cfg: dict) -> int:
    return parse_addr(cfg.get("page_addr"), 0x7000)


def page_cycle_items(cfg: dict) -> list[tuple[int, float]]:
    raw = cfg.get("page_cycle") or DEFAULT_PAGE_CYCLE
    items: list[tuple[int, float]] = []
    for item in raw:
        items.append((int(item["page"]), max(0.2, float(item["seconds"]))))
    if not items:
        items = [(int(cfg.get("default_page", 4)), 6.0)]
    return items


def page_packet(cfg: dict, page: int) -> tuple[bytes, str]:
    addr = page_addr(cfg)
    page &= 0xFFFF
    return build_write_word(addr, page), f"page@{addr:#06x}={page}"


def switch_page(
    ser: Optional[serial.Serial],
    cfg: dict,
    page: int,
    *,
    dry_run: bool = False,
    log_feedback: bool = True,
) -> None:
    packet, label = page_packet(cfg, page)
    LOG.info("%s -> %s", label, format_hex(packet))
    if dry_run or ser is None:
        return
    send_packet(ser, packet, label, log_feedback=log_feedback)


def page_cycle_worker() -> None:
    cfg = _RUNTIME.get("cfg") or {}
    items = page_cycle_items(cfg)
    idx = 0
    LOG.info(
        "Page cycle: %s",
        " -> ".join(f"p{page}({seconds:g}s)" for page, seconds in items),
    )
    while not _RUNTIME["stop"].is_set():
        page, seconds = items[idx]
        ser = _RUNTIME.get("ser")
        dry_run = bool(_RUNTIME.get("dry_run"))
        try:
            send_page_metrics(ser, cfg, page, dry_run=dry_run, resample=True)
            switch_page(ser, cfg, page, dry_run=dry_run)
            send_page_metrics(ser, cfg, page, dry_run=dry_run, resample=False)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Page switch to %s failed: %s", page, exc)
        if _RUNTIME["stop"].wait(seconds):
            break
        idx = (idx + 1) % len(items)


def _metric_packets(
    addr: int,
    *,
    mode: str,
    word_value: int,
    text: str,
    encoding: str,
    label: str,
) -> list[tuple[bytes, str]]:
    """Write a screen metric as LEVETOP string (text) and/or 16-bit word (bar/number)."""
    if not addr:
        return []
    packets: list[tuple[bytes, str]] = []
    mode = (mode or "string").lower()
    if mode in ("word", "both"):
        packets.append(
            (build_write_word(addr, word_value & 0xFFFF), f"{label}={word_value} (word)")
        )
    if mode in ("string", "both"):
        packets.append(
            (
                build_write_string(addr, text, encoding=encoding),
                f"{label}={text!r} (string)",
            )
        )
    if not packets:
        packets.append(
            (
                build_write_string(addr, text, encoding=encoding),
                f"{label}={text!r} (string)",
            )
        )
    return packets


def sample_page5_cpu(cfg: dict) -> dict[str, int]:
    usage = cpu_load_percent(0.2)
    temp_c = cpu_temperature_c()
    temp_word = 0 if temp_c is None else max(0, min(999, int(round(temp_c))))
    full_c = max(float(cfg.get("cpu_temp_percent_full_c", 100)), 1.0)
    temp_pct = 0 if temp_c is None else max(0, min(100, int(round(temp_c * 100.0 / full_c))))
    data = {"usage": usage, "temp": temp_word, "pct": temp_pct}
    _RUNTIME["page5_cpu"] = data
    return data


def page5_cpu_packets(cfg: dict, data: Optional[dict[str, int]] = None) -> list[tuple[bytes, str]]:
    """Page 5: 0x143E/0x1452 字符型; 0x1366 CPU 温度进度条."""
    data = data or _RUNTIME.get("page5_cpu") or sample_page5_cpu(cfg)
    usage_addr = parse_addr(cfg.get("cpu_usage_addr"), 0x143E)
    temp_addr = parse_addr(cfg.get("cpu_temp_celsius_addr"), 0x1452)
    pct_addr = parse_addr(cfg.get("cpu_temp_percent_addr"), 0x1366)
    usage_text = str(int(data["usage"]))
    temp_text = str(int(data["temp"]))
    pct = int(data["pct"]) & 0xFFFF
    return [
        (
            build_write_packed_ascii(usage_addr, usage_text),
            f"cpu_usage@{usage_addr:#06x}={usage_text} (ascii8)",
        ),
        (
            build_write_packed_ascii(temp_addr, temp_text),
            f"cpu_temp_c@{temp_addr:#06x}={temp_text} (ascii8)",
        ),
        (
            build_write_word(pct_addr, pct),
            f"cpu_temp_pct@{pct_addr:#06x}={pct}/100 (word bar)",
        ),
    ]


def sample_page6_gpu(cfg: dict) -> dict[str, int]:
    usage, temp_c = gpu_usage_and_temp()
    temp_word = 0 if temp_c is None else max(0, min(999, int(round(temp_c))))
    full_c = max(float(cfg.get("gpu_temp_percent_full_c", 100)), 1.0)
    temp_pct = 0 if temp_c is None else max(0, min(100, int(round(temp_c * 100.0 / full_c))))
    data = {"usage": int(usage), "temp": temp_word, "pct": temp_pct}
    _RUNTIME["page6_gpu"] = data
    return data


def page6_gpu_packets(cfg: dict, data: Optional[dict[str, int]] = None) -> list[tuple[bytes, str]]:
    """Page 6: 0x133E/0x1352 字符型; 0x1166 GPU 温度进度条."""
    data = data or _RUNTIME.get("page6_gpu") or sample_page6_gpu(cfg)
    usage_addr = parse_addr(cfg.get("gpu_usage_addr"), 0x133E)
    temp_addr = parse_addr(cfg.get("gpu_temp_addr"), 0x1352)
    pct_addr = parse_addr(cfg.get("gpu_temp_percent_addr"), 0x1166)
    usage_text = str(int(data["usage"]))
    temp_text = str(int(data["temp"]))
    pct = int(data["pct"]) & 0xFFFF
    return [
        (
            build_write_packed_ascii(usage_addr, usage_text),
            f"gpu_usage@{usage_addr:#06x}={usage_text} (ascii8)",
        ),
        (
            build_write_packed_ascii(temp_addr, temp_text),
            f"gpu_temp_c@{temp_addr:#06x}={temp_text} (ascii8)",
        ),
        (
            build_write_word(pct_addr, pct),
            f"gpu_temp_pct@{pct_addr:#06x}={pct}/100 (word bar)",
        ),
    ]


def sample_page7_mem(_cfg: dict) -> dict[str, int | str]:
    detail = memory_usage_detail()
    data: dict[str, int | str] = {
        "percent": int(detail["percent"]),
        "text": str(detail["text"]),
    }
    _RUNTIME["page7_mem"] = data
    return data


def page7_mem_packets(cfg: dict, data: Optional[dict[str, int | str]] = None) -> list[tuple[bytes, str]]:
    """Page 7: 0x123E 使用率字符; 0x11CC 进度条; 0x128F 16GB/32GB."""
    data = data or _RUNTIME.get("page7_mem") or sample_page7_mem(cfg)
    usage_addr = parse_addr(cfg.get("mem_usage_addr"), 0x123E)
    bar_addr = parse_addr(cfg.get("mem_usage_bar_addr"), 0x11CC)
    cap_addr = parse_addr(cfg.get("mem_capacity_addr"), 0x128F)
    percent = int(data["percent"])
    text = str(data["text"])
    batt_minutes = str(cfg.get("battery_minutes_text", "0m"))
    batt_percent = int(cfg.get("battery_percent", 0))
    minutes_addr = parse_addr(cfg.get("battery_minutes_addr"), 0x12E1)
    batt_pct_addr = parse_addr(cfg.get("battery_percent_addr"), 0x125D)
    batt_bar_addr = parse_addr(cfg.get("battery_bar_addr"), 0x1266)
    return [
        (
            build_write_packed_ascii(usage_addr, str(percent)),
            f"mem_usage@{usage_addr:#06x}={percent} (ascii8)",
        ),
        (
            build_write_word(bar_addr, percent & 0xFFFF),
            f"mem_usage_bar@{bar_addr:#06x}={percent}/100 (word bar)",
        ),
        (
            build_write_packed_ascii(cap_addr, text),
            f"mem_capacity@{cap_addr:#06x}={text} (ascii8)",
        ),
        (
            build_write_packed_ascii(minutes_addr, batt_minutes),
            f"battery_min@{minutes_addr:#06x}={batt_minutes} (ascii8)",
        ),
        (
            build_write_packed_ascii(batt_pct_addr, str(batt_percent)),
            f"battery_pct@{batt_pct_addr:#06x}={batt_percent} (ascii8)",
        ),
        (
            build_write_word(batt_bar_addr, batt_percent & 0xFFFF),
            f"battery_bar@{batt_bar_addr:#06x}={batt_percent}/100 (word bar)",
        ),
    ]


def send_packets(
    ser: Optional[serial.Serial],
    packets: list[tuple[bytes, str]],
    *,
    dry_run: bool = False,
) -> None:
    for packet, label in packets:
        LOG.info("%s -> %s", label, format_hex(packet))
        if not dry_run and ser is not None:
            send_packet(ser, packet, label)


def send_page5_cpu(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    resample: bool = False,
) -> None:
    data = sample_page5_cpu(cfg) if resample or not _RUNTIME.get("page5_cpu") else _RUNTIME["page5_cpu"]
    send_packets(ser, page5_cpu_packets(cfg, data), dry_run=dry_run)


def send_page6_gpu(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    resample: bool = False,
) -> None:
    data = sample_page6_gpu(cfg) if resample or not _RUNTIME.get("page6_gpu") else _RUNTIME["page6_gpu"]
    send_packets(ser, page6_gpu_packets(cfg, data), dry_run=dry_run)


def send_page7_mem(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    resample: bool = False,
) -> None:
    data = sample_page7_mem(cfg) if resample or not _RUNTIME.get("page7_mem") else _RUNTIME["page7_mem"]
    send_packets(ser, page7_mem_packets(cfg, data), dry_run=dry_run)


def sample_page8_store(cfg: dict) -> dict[str, int | str]:
    storage = storage_usage_detail()
    net = network_rate_texts(float(cfg.get("page8_net_sample_seconds", 0.5)))
    data: dict[str, int | str] = {
        "percent": int(storage["percent"]),
        "text": str(storage["text"]),
        "speed": net["speed"],
        "upload": net["upload"],
        "download": net["download"],
    }
    _RUNTIME["page8_store"] = data
    return data


def page8_store_packets(cfg: dict, data: Optional[dict[str, int | str]] = None) -> list[tuple[bytes, str]]:
    """Page 8: storage 0x113E/0x118F/0x12CC; net 0x11CD/0x11E1/0x11F8."""
    data = data or _RUNTIME.get("page8_store") or sample_page8_store(cfg)
    usage_addr = parse_addr(cfg.get("storage_usage_addr"), 0x113E)
    cap_addr = parse_addr(cfg.get("storage_capacity_addr"), 0x118F)
    bar_addr = parse_addr(cfg.get("storage_bar_addr"), 0x12CC)
    speed_addr = parse_addr(cfg.get("net_speed_text_addr"), 0x11CD)
    up_addr = parse_addr(cfg.get("net_upload_addr"), 0x11E1)
    down_addr = parse_addr(cfg.get("net_download_addr"), 0x11F8)
    percent = int(data["percent"])
    return [
        (
            build_write_packed_ascii(usage_addr, str(percent)),
            f"storage_usage@{usage_addr:#06x}={percent} (ascii8)",
        ),
        (
            build_write_packed_ascii(cap_addr, str(data["text"])),
            f"storage_cap@{cap_addr:#06x}={data['text']} (ascii8)",
        ),
        (
            build_write_word(bar_addr, percent & 0xFFFF),
            f"storage_bar@{bar_addr:#06x}={percent}/100 (word bar)",
        ),
        (
            build_write_packed_ascii(speed_addr, str(data["speed"])),
            f"net_speed@{speed_addr:#06x}={data['speed']} (ascii8)",
        ),
        (
            build_write_packed_ascii(up_addr, str(data["upload"])),
            f"net_upload@{up_addr:#06x}={data['upload']} (ascii8)",
        ),
        (
            build_write_packed_ascii(down_addr, str(data["download"])),
            f"net_download@{down_addr:#06x}={data['download']} (ascii8)",
        ),
    ]


def send_page8_store(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    resample: bool = False,
) -> None:
    data = sample_page8_store(cfg) if resample or not _RUNTIME.get("page8_store") else _RUNTIME["page8_store"]
    send_packets(ser, page8_store_packets(cfg, data), dry_run=dry_run)


def _parse_icon_id(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        text = text.removeprefix("icon/")
        if text.endswith(".png") or text.endswith(".bmp") or text.endswith(".jpg"):
            text = text.rsplit(".", 1)[0]
        text = text.split("_", 1)[0]
        if text.startswith("0x"):
            return int(text, 16)
        if text.isdigit():
            return int(text, 10)
        return default
    return int(value)


def disk_icon_word(cfg: dict, status: str) -> int:
    """Icon VP: 0=0801 green, 1=0802 red, 2=0803 gray. Out-of-range hides the icon."""
    first = _parse_icon_id(cfg.get("disk_icon_first"), 0)
    pictures = {
        "ok": _parse_icon_id(cfg.get("disk_icon_ok"), 0),
        "error": _parse_icon_id(cfg.get("disk_icon_error"), 1),
        "empty": _parse_icon_id(cfg.get("disk_icon_empty"), 2),
    }
    picture = pictures.get(status, pictures["empty"])
    if first > 0:
        return max(0, picture - first) & 0xFFFF
    return picture & 0xFFFF


def disk_icon_addrs(cfg: dict) -> list[int]:
    ssd = cfg.get("ssd_icon_addrs")
    hdd = cfg.get("hdd_icon_addrs")
    addrs: list[int] = []
    if isinstance(ssd, list):
        addrs.extend(parse_addr(item) for item in ssd)
    if isinstance(hdd, list):
        addrs.extend(parse_addr(item) for item in hdd)
    if len(addrs) >= 10:
        return addrs[:10]
    base = parse_addr(cfg.get("disk_icon_base_addr"), 0x14CE)
    return [base + index for index in range(10)]


def _normalize_disk_status(value: Any, fallback: str = "empty") -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "ok": "ok",
        "green": "ok",
        "normal": "ok",
        "error": "error",
        "red": "error",
        "fail": "error",
        "failed": "error",
        "empty": "empty",
        "gray": "empty",
        "grey": "empty",
        "missing": "empty",
    }
    return aliases.get(text, fallback)


def _apply_disk_overrides(cfg: dict, data: dict[str, list[str]]) -> dict[str, list[str]]:
    ssd = list(data.get("ssd") or ["empty"] * 4)
    hdd = list(data.get("hdd") or ["empty"] * 6)
    ssd_override = cfg.get("ssd_status_override")
    hdd_override = cfg.get("hdd_status_override")
    if isinstance(ssd_override, list):
        for index, value in enumerate(ssd_override[:4]):
            if index < len(ssd):
                ssd[index] = _normalize_disk_status(value, ssd[index])
    if isinstance(hdd_override, list):
        for index, value in enumerate(hdd_override[:6]):
            if index < len(hdd):
                hdd[index] = _normalize_disk_status(value, hdd[index])
            elif index >= len(hdd):
                hdd.append(_normalize_disk_status(value))
    return {"ssd": ssd[:4], "hdd": (hdd + ["empty"] * 6)[:6]}


def _disk_statuses(data: dict[str, list[str]]) -> list[str]:
    return list(data.get("ssd") or []) + list(data.get("hdd") or [])


def _led_ctrl_search_dirs(cfg: dict) -> list[Path]:
    dirs: list[Path] = []
    extra = cfg.get("led_ctrl_dir")
    if extra:
        dirs.append(Path(str(extra)))
    here = Path(__file__).resolve().parent
    dirs.extend(
        [
            here / "led",
            Path("/opt/displayshow/led"),
            Path("/home/displayshow/led"),
            Path("/home/display/led"),
        ]
    )
    unique: list[Path] = []
    for path in dirs:
        resolved = path.expanduser()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _led_ctrl_module(cfg: dict):
    for led_dir in _led_ctrl_search_dirs(cfg):
        if not (led_dir / "led_ctrl.py").is_file():
            continue
        led_path = str(led_dir)
        if led_path not in sys.path:
            sys.path.insert(0, led_path)
        import led_ctrl  # noqa: PLC0415

        return led_ctrl
    return None


def _led_set_mode_at(mode: int, port: str, baudrate: int) -> str:
    with serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.8,
        write_timeout=2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:
        # CH340 DTR/RTS 拉高会复位灯控 MCU，命令还没执行就被重启。
        try:
            ser.dtr = False
            ser.rts = False
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.2)
        ser.reset_input_buffer()
        ser.write(f"AT+L_MODE={int(mode)}\r\n".encode("ascii"))
        ser.flush()
        time.sleep(0.3)
        raw = ser.read(128)
    return raw.decode("ascii", errors="replace").strip()


def sync_led_strip_for_disks(cfg: dict, data: dict[str, list[str]]) -> None:
    """Any red disk -> AT+L_MODE=1; otherwise AT+L_MODE=5."""
    if not cfg.get("led_follow_disks", True):
        return
    has_error = any(status == "error" for status in _disk_statuses(data))
    mode = int(cfg.get("led_error_mode", 1) if has_error else cfg.get("led_ok_mode", 5))
    with _RUNTIME["led_lock"]:
        if _RUNTIME.get("led_mode") == mode:
            return
        port = str(cfg.get("led_serial_port") or LED_DEFAULT_PORT)
        screen_port = str(cfg.get("serial_port") or "")
        if port and port == screen_port:
            LOG.error("LED port %s is the screen serial port; skip LED update", port)
            return
        baud = int(cfg.get("led_baudrate") or 9600)
        try:
            led_ctrl = _led_ctrl_module(cfg)
            if led_ctrl is not None:
                reply = led_ctrl.set_mode(mode, port=port, baudrate=baud)
            else:
                reply = _led_set_mode_at(mode, port, baud)
        except Exception as exc:  # noqa: BLE001
            LOG.error("LED mode %s failed: %s", mode, exc)
            return
        if "OK" in (reply or "").upper():
            _RUNTIME["led_mode"] = mode
            LOG.info(
                "LED AT+L_MODE=%s (%s) -> %s",
                mode,
                "disk error" if has_error else "disks ok",
                reply,
            )
        else:
            LOG.warning("LED AT+L_MODE=%s unexpected reply: %s", mode, reply or "(no response)")


def sample_page9_disks(cfg: dict, *, check_smart: bool = True) -> dict[str, list[str]]:
    roots = cfg.get("ssd_slot_pci_roots")
    if not isinstance(roots, list):
        roots = None
    hdd_pci = cfg.get("hdd_slot_pci")
    if hdd_pci is not None:
        hdd_pci = str(hdd_pci).strip() or None
    raw_ports = cfg.get("hdd_ata_ports")
    hdd_ports = None
    if isinstance(raw_ports, list):
        parsed: list[int] = []
        for item in raw_ports:
            try:
                parsed.append(int(item))
            except (TypeError, ValueError):
                continue
        hdd_ports = parsed or None
    data = disk_bay_status(
        ssd_slot_pci_roots=roots,
        hdd_slot_pci=hdd_pci,
        hdd_ata_ports=hdd_ports,
        check_smart=check_smart,
    )
    data = _apply_disk_overrides(cfg, data)
    prev = _RUNTIME.get("page9_disks")
    if isinstance(prev, dict) and not check_smart:
        data = _keep_previous_disk_errors(prev, data)
    _RUNTIME["page9_disks"] = data
    return data


def _keep_previous_disk_errors(
    prev: dict[str, list[str]],
    data: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Presence-only refresh must not clear a SMART/md error until the next full check."""
    merged: dict[str, list[str]] = {
        "ssd": list(data.get("ssd") or []),
        "hdd": list(data.get("hdd") or []),
    }
    for kind in ("ssd", "hdd"):
        old = list(prev.get(kind) or [])
        for index, status in enumerate(merged[kind]):
            if status == "ok" and index < len(old) and old[index] == "error":
                merged[kind][index] = "error"
    return merged


def page9_disk_packets(cfg: dict, data: Optional[dict[str, list[str]]] = None) -> list[tuple[bytes, str]]:
    """Page 9: Icon VP 0x14CE..0x14D1 SSD, 0x14D2..0x14D7 HDD. 0=绿 1=红 2=灰.

    Consecutive icon addresses are written in one frame so the screen does not
    flash the page-default gray while ten single-word writes wait for ACK.
    """
    data = data or _RUNTIME.get("page9_disks") or sample_page9_disks(cfg)
    statuses = list(data.get("ssd") or []) + list(data.get("hdd") or [])
    addrs = disk_icon_addrs(cfg)
    labels = [f"ssd{index + 1}" for index in range(4)] + [f"hdd{index + 1}" for index in range(6)]
    skip_empty = bool(cfg.get("disk_icon_skip_empty", False))
    items: list[tuple[int, int, str, str]] = []
    for index, addr in enumerate(addrs):
        status = statuses[index] if index < len(statuses) else "empty"
        if skip_empty and status == "empty":
            continue
        name = labels[index] if index < len(labels) else f"disk{index}"
        items.append((addr, disk_icon_word(cfg, status), name, status))
    packets: list[tuple[bytes, str]] = []
    run_start = 0
    while run_start < len(items):
        run_end = run_start + 1
        while (
            run_end < len(items)
            and items[run_end][0] == items[run_end - 1][0] + 1
        ):
            run_end += 1
        run = items[run_start:run_end]
        values = [item[1] for item in run]
        detail = " ".join(f"{name}={value}({status})" for _addr, value, name, status in run)
        packets.append(
            (
                build_write_words(run[0][0], values),
                f"disks@{run[0][0]:#06x} {detail}",
            )
        )
        run_start = run_end
    return packets


def send_page9_disks(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    resample: bool = False,
    check_smart: bool = True,
) -> None:
    if resample or not _RUNTIME.get("page9_disks"):
        data = sample_page9_disks(cfg, check_smart=check_smart)
    else:
        data = _RUNTIME["page9_disks"]
    send_packets(ser, page9_disk_packets(cfg, data), dry_run=dry_run)
    if not dry_run:
        sync_led_strip_for_disks(cfg, data)


def send_page_metrics(
    ser: Optional[serial.Serial],
    cfg: dict,
    page: int,
    *,
    dry_run: bool = False,
    resample: bool = False,
) -> None:
    page4 = int(cfg.get("default_page", 4))
    page5 = int(cfg.get("cpu_metrics_page", 5))
    page6 = int(cfg.get("gpu_metrics_page", 6))
    page7 = int(cfg.get("mem_metrics_page", 7))
    page8 = int(cfg.get("storage_metrics_page", 8))
    page9 = int(cfg.get("disk_metrics_page", 9))
    if page == page4:
        send_nic_ips(ser, cfg, dry_run=dry_run)
    elif page == page5:
        send_page5_cpu(ser, cfg, dry_run=dry_run, resample=resample)
    elif page == page6:
        send_page6_gpu(ser, cfg, dry_run=dry_run, resample=resample)
    elif page == page7:
        send_page7_mem(ser, cfg, dry_run=dry_run, resample=resample)
    elif page == page8:
        send_page8_store(ser, cfg, dry_run=dry_run, resample=resample)
    elif page == page9:
        # Page cycle hits this every ~20s; skip SMART so the page can switch
        # immediately. The 60s refresh still runs a full SMART pass.
        send_page9_disks(
            ser,
            cfg,
            dry_run=dry_run,
            resample=resample,
            check_smart=False,
        )


def send_all_page_metrics(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
) -> None:
    send_page5_cpu(ser, cfg, dry_run=dry_run, resample=True)
    send_page6_gpu(ser, cfg, dry_run=dry_run, resample=True)
    send_page7_mem(ser, cfg, dry_run=dry_run, resample=True)
    send_page8_store(ser, cfg, dry_run=dry_run, resample=True)
    send_page9_disks(ser, cfg, dry_run=dry_run, resample=True)


def _net_iface_auto(cfg: dict) -> bool:
    return bool(cfg.get("net_iface_auto", True))


def nic_ip_packets(cfg: dict, *, wait_dhcp: bool = False) -> list[tuple[bytes, str]]:
    encoding = cfg.get("string_encoding", "gb2312")
    auto = _net_iface_auto(cfg)
    net1_cfg = str(cfg.get("net1_iface") or "").strip()
    net2_cfg = str(cfg.get("net2_iface") or "").strip()
    net1_iface = resolve_display_iface(net1_cfg, 0, auto=auto)
    net2_iface = resolve_display_iface(net2_cfg, 1, auto=auto)
    discovered = display_ethernet_ifaces(slots=2)
    if not _RUNTIME.get("nic_map_logged"):
        _RUNTIME["nic_map_logged"] = True
        LOG.info(
            "NIC auto=%s discovered=%s -> net1=%s net2=%s",
            auto,
            ",".join(discovered) or "(none)",
            net1_iface or "none",
            net2_iface or "none",
        )
    wait_s = float(cfg.get("net_ip_wait_seconds", 8.0)) if wait_dhcp else 0.0
    if wait_s > 0:
        ip1 = iface_ipv4_wait(net1_iface, wait_s)
        ip2 = iface_ipv4_wait(net2_iface, wait_s)
    else:
        ip1 = iface_ipv4(net1_iface)
        ip2 = iface_ipv4(net2_iface)
    net1_addr = parse_addr(cfg.get("net1_ip_addr"), 0x1116)
    net2_addr = parse_addr(cfg.get("net2_ip_addr"), 0x112A)
    if not net1_iface:
        LOG.warning("No Ethernet NIC for net1; writing %s", ip1)
    if not net2_iface:
        LOG.warning("No Ethernet NIC for net2; writing %s", ip2)
    return [
        (
            build_write_string(net1_addr, ip1, encoding=encoding),
            f"net1_ip@{net1_addr:#06x}={ip1} ({net1_iface or 'none'})",
        ),
        (
            build_write_string(net2_addr, ip2, encoding=encoding),
            f"net2_ip@{net2_addr:#06x}={ip2} ({net2_iface or 'none'})",
        ),
    ]


def send_nic_ips(
    ser: Optional[serial.Serial],
    cfg: dict,
    *,
    dry_run: bool = False,
    wait_dhcp: bool = False,
) -> None:
    send_packets(ser, nic_ip_packets(cfg, wait_dhcp=wait_dhcp), dry_run=dry_run)


def refresh_once(ser: serial.Serial, cfg: dict, *, dry_run: bool = False) -> None:
    send_nic_ips(ser, cfg, dry_run=dry_run)
    send_all_page_metrics(ser, cfg, dry_run=dry_run)
    now = datetime.now()
    progress0 = cpu_load_percent()
    progress1 = memory_usage_percent()
    cpu_temp = cpu_temperature_display_value(
        mode=cfg.get("cpu_temp_mode", "celsius"),
        temp_min_c=float(cfg.get("cpu_temp_min_c", 30)),
        temp_max_c=float(cfg.get("cpu_temp_max_c", 100)),
    )
    cpu_temp_c = cpu_temperature_c()
    cpu_queue = cpu_processor_queue_length()
    net_speed = network_speed_display_value(
        mode=cfg.get("network_speed_mode", "percent_100m"),
        sample_seconds=float(cfg.get("network_speed_sample_seconds", 1.0)),
        max_kbps=int(cfg.get("network_speed_max_kbps", 102400)),
        max_mbps=int(cfg.get("network_speed_max_mbps", 100)),
    )

    packets: list[tuple[bytes, str]] = list(clock_packets(now, cfg))

    if cfg.get("backlight_every_refresh", True):
        packets.append(backlight_packet(cfg, backlight_on_value(cfg)))

    p0_addr = parse_addr(cfg.get("progress_0_addr"), 0x1003)
    p1_addr = parse_addr(cfg.get("progress_1_addr"), 0x0004)
    cpu_addr = parse_addr(cfg.get("cpu_temp_addr"), 0x0002)
    queue_addr = parse_addr(cfg.get("cpu_queue_addr"), 0x001E)
    net_addr = parse_addr(cfg.get("network_speed_addr"), 0x1005)
    status_addr = parse_addr(cfg.get("network_status_addr"), 0x1008)
    health_addr = parse_addr(cfg.get("health_score_addr"), 0x1007)
    health_status_addr = parse_addr(cfg.get("health_status_addr"), 0x10EE)
    net_status = network_connection_status()
    health = system_health_breakdown()
    health_score = int(round(health["total"]))
    encoding = cfg.get("string_encoding", "gb2312")
    temp_note = f", {cpu_temp_c:.1f}C" if cpu_temp_c is not None else ", N/A"
    packets.append((build_write_word(p0_addr, progress0), f"progress_0={progress0}%"))
    packets.append((build_write_word(p1_addr, progress1), f"progress_1={progress1}% (memory)"))
    packets.append(
        (
            build_write_word(cpu_addr, cpu_temp),
            f"cpu_temp@{cpu_addr:#06x}={cpu_temp}{temp_note}",
        )
    )
    packets.append(
        (
            build_write_word(queue_addr, cpu_queue),
            f"cpu_queue@{queue_addr:#06x}={cpu_queue}",
        )
    )
    packets.append(
        (
            build_write_word(net_addr, net_speed),
            f"network_speed@{net_addr:#06x}={net_speed}/100 (0-100M)",
        )
    )
    if cfg.get("network_status_mode", "string") == "word":
        status_word = network_status_display_word(net_status)
        packets.append(
            (
                build_write_word(status_addr, status_word),
                f"network_status@{status_addr:#06x}={status_word} ({net_status})",
            )
        )
    else:
        packets.append(
            (
                build_write_string(status_addr, net_status, encoding=encoding),
                f"network_status@{status_addr:#06x}={net_status}",
            )
        )
    health_label = health_status_label(health_score)
    packets.append(
        (
            build_write_word(health_addr, health_score),
            (
                f"health_score@{health_addr:#06x}={health_score}/100 "
                f"(cpu={health['cpu']:.0f} mem={health['memory']:.0f} "
                f"disk={health['disk']:.0f} net={health['network']:.0f})"
            ),
        )
    )
    if cfg.get("health_status_mode", "string") == "word":
        health_word = health_status_display_word(health_score)
        packets.append(
            (
                build_write_word(health_status_addr, health_word),
                f"health_status@{health_status_addr:#06x}={health_word} ({health_label})",
            )
        )
    else:
        packets.append(
            (
                build_write_string(health_status_addr, health_label, encoding=encoding),
                f"health_status@{health_status_addr:#06x}={health_label}",
            )
        )
    packets.extend(nic_ip_packets(cfg))

    for packet, label in packets:
        LOG.info("%s -> %s", label, format_hex(packet))
        if not dry_run:
            send_packet(ser, packet, label)


def run_loop(cfg: dict, *, once: bool = False, dry_run: bool = False) -> None:
    interval = max(1, int(cfg.get("refresh_interval_seconds", 60)))
    baud = int(cfg.get("baudrate", 115200))

    _RUNTIME["cfg"] = cfg
    _RUNTIME["dry_run"] = dry_run
    _RUNTIME["dimmed"] = False
    _RUNTIME["stop"].clear()
    if not once and cfg.get("backlight_dim_on_shutdown", True):
        atexit.register(dim_screen_now, "atexit")
    _install_signal_hooks()

    if dry_run:
        LOG.warning(
            "Dry run: packets are NOT sent to %s. Remove --dry-run to update the screen.",
            cfg.get("serial_port", DEFAULT_SERIAL_PORT),
        )
        page_thread = None
        if not once:
            page_thread = threading.Thread(
                target=page_cycle_worker,
                name="page-cycle",
                daemon=True,
            )
            page_thread.start()
        else:
            switch_page(None, cfg, int(cfg.get("default_page", 4)), dry_run=True)
        while not _RUNTIME["stop"].is_set():
            refresh_once(ser=None, cfg=cfg, dry_run=True)  # type: ignore[arg-type]
            if once:
                break
            if _RUNTIME["stop"].wait(interval):
                break
        return

    port = resolve_serial_port(cfg)
    with open_serial(port, baud) as ser:
        _RUNTIME["ser"] = ser
        LOG.info("Opened %s @ %d baud, refresh every %ds", port, baud, interval)
        LOG.info(
            "Backlight 0x%04X on=%s off=%s (dim on shutdown=%s)",
            backlight_addr(cfg),
            backlight_on_value(cfg),
            backlight_off_value(cfg),
            cfg.get("backlight_dim_on_shutdown", True),
        )
        try:
            write_backlight(ser, cfg, backlight_on_value(cfg), dry_run=False)
            send_nic_ips(ser, cfg, dry_run=False, wait_dhcp=True)
            send_all_page_metrics(ser, cfg, dry_run=False)
            switch_page(ser, cfg, int(cfg.get("default_page", 4)), dry_run=False)
            page_thread = None
            if not once:
                page_thread = threading.Thread(
                    target=page_cycle_worker,
                    name="page-cycle",
                    daemon=True,
                )
                page_thread.start()
            while not _RUNTIME["stop"].is_set():
                try:
                    refresh_once(ser, cfg, dry_run=False)
                except serial.SerialException as exc:
                    LOG.error("Serial error: %s", exc)
                if once:
                    break
                if _RUNTIME["stop"].wait(interval):
                    break
        finally:
            _RUNTIME["ser"] = None
            if not _RUNTIME["dimmed"] and not once:
                dim_screen_now("loop exit")


def set_backlight_cli(cfg: dict, value: int, *, dry_run: bool = False) -> None:
    if dry_run:
        write_backlight(None, cfg, value, dry_run=True)
        return
    port = resolve_serial_port(cfg)
    baud = int(cfg.get("baudrate", 115200))
    with open_serial(port, baud) as ser:
        write_backlight(ser, cfg, value, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEVETOP serial screen updater (Linux)")
    parser.add_argument("-c", "--config", type=Path, default=CONFIG_PATH, help="config.json path")
    parser.add_argument("--once", action="store_true", help="refresh once and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log packets only; does NOT write to serial port (screen will not change)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--probe-rtc",
        action="store_true",
        help="write once then read back registers 0x7002..0x7007 for diagnosis",
    )
    parser.add_argument(
        "--blank",
        action="store_true",
        help="write backlight_off to 0x7001 and exit (test dim)",
    )
    parser.add_argument(
        "--unblank",
        action="store_true",
        help="write backlight_on to 0x7001 and exit (test wake)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.config.is_file():
        LOG.error("Config not found: %s", args.config)
        return 1

    cfg = load_config(args.config)
    _RUNTIME["cfg"] = cfg
    _RUNTIME["dry_run"] = args.dry_run

    if args.blank and args.unblank:
        LOG.error("Use only one of --blank / --unblank")
        return 1

    if args.blank:
        set_backlight_cli(cfg, backlight_off_value(cfg), dry_run=args.dry_run)
        return 0

    if args.unblank:
        set_backlight_cli(cfg, backlight_on_value(cfg), dry_run=args.dry_run)
        return 0

    if args.probe_rtc:
        port = resolve_serial_port(cfg)
        baud = int(cfg.get("baudrate", 115200))
        with open_serial(port, baud) as ser:
            refresh_once(ser, cfg, dry_run=False)
            probe_screen_rtc(ser)
        return 0

    try:
        run_loop(cfg, once=args.once, dry_run=args.dry_run)
    except KeyboardInterrupt:
        dim_screen_now("KeyboardInterrupt")
        LOG.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
