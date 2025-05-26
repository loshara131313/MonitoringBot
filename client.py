from __future__ import annotations

"""pc_agent_tls.py – Lightweight PC agent."""


import logging
import os
import platform
try:
    import wmi
except ImportError:
    wmi = None
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import List, Optional
import shutil, subprocess, re
import psutil
import requests
from requests import Session
from requests.exceptions import SSLError, ConnectionError
try:
    import pynvml
    pynvml.nvmlInit()
except Exception:
    pynvml = None

log = logging.getLogger("pc-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ENV_FILE = Path(".env")

# ────────────────────────── load .env → os.environ ─────────────────────────
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ────────────────────────── prompt helpers ─────────────────────────────────
IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

def prompt_ip() -> str:
    while True:
        ip = input("Enter SERVER IPv4 [127.0.0.1]: ").strip() or "127.0.0.1"
        if IP_RE.match(ip):
            return ip
        print("❌ Invalid IPv4, try again (e.g. 192.168.1.42)")

# ────────────────────────── CONFIG values ──────────────────────────────────
SECRET = os.getenv("AGENT_SECRET") or input("Enter AGENT_SECRET: ").strip()
if not SECRET:
    print("AGENT_SECRET required"); sys.exit(1)

if "AGENT_SECRET" not in os.environ:
    ENV_FILE.write_text((ENV_FILE.read_text() if ENV_FILE.exists() else "") + f"AGENT_SECRET={SECRET}\n")

SERVER_IP = os.getenv("AGENT_SERVER_IP")
if not SERVER_IP:
    SERVER_IP = prompt_ip()
    ENV_FILE.write_text((ENV_FILE.read_text() if ENV_FILE.exists() else "") + f"AGENT_SERVER_IP={SERVER_IP}\n")

PORT = int(os.getenv("AGENT_PORT", "8000"))

VERIFY_ENV = os.getenv("AGENT_VERIFY_SSL", "1").lower()
if VERIFY_ENV == "0":
    VERIFY_SSL: Optional[str | bool] = False
elif VERIFY_ENV == "force":
    VERIFY_SSL = True
else:
    VERIFY_SSL = True

CA_FILE = os.getenv("AGENT_CA_FILE")
if CA_FILE:
    VERIFY_SSL = CA_FILE  # requests accepts str path

SCHEME = "https" if VERIFY_SSL is not False else "http"
SERVER = f"{SCHEME}://{SERVER_IP}:{PORT}"
INTERVAL = int(os.getenv("AGENT_INTERVAL", "5"))

log.info("Config → server %s verify=%s interval %ss", SERVER, VERIFY_SSL, INTERVAL)

# ────────────────────────── metric helpers ────────────────────────────────

UNIT_NAMES = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]

def human_bytes(num: float) -> str:
    for unit in UNIT_NAMES:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} EiB"


def disk_bar(p: float, length=10) -> str:
    filled = int(round(p * length / 100))
    return "█" * filled + "░" * (length - filled)


def gather_disks() -> List[str]:
    lines, seen = [], set()
    lines.append(
        "━━━━━━━━━━━DISKS━━━━━━━━━━")
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen or part.fstype.lower() in {"tmpfs", "devtmpfs"}:
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        if u.total == 0:
            continue
        disk_string = f"💾 {part.mountpoint}: {disk_bar(u.percent)} {u.percent:.0f}% ({human_bytes(u.used)} / {human_bytes(u.total)})"
        if u.percent >= 90:
            disk_string += "❗"
        lines.append(
            disk_string
        )
    return lines

def gather_gpu() -> tuple[str, str, str, str] | None:
    # ── 1) pynvml ─────────────────────────────
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(h).gpu          # %
        mem  = pynvml.nvmlDeviceGetMemoryInfo(h)                    # bytes
        temp = pynvml.nvmlDeviceGetTemperature(
            h, pynvml.NVML_TEMPERATURE_GPU)
        return (
            "━━━━━━━━━━━GPU━━━━━━━━━━━",
            f"🎮 GPU: {util:.1f}%",
            f"🗄️ VRAM: {mem.used/2**20:.0f} / {mem.total/2**20:.0f} MiB "
            f"({mem.used/mem.total*100:.1f}%)",
            f"🌡️ GPU Temp: {temp} °C"
        )
    except Exception:
        pass  # переходим к следующему способу

    # ── 2)
    if shutil.which("nvidia-smi"):
        try:
            util, used, total, temp = map(float, re.split(r",\s*",
                                                          subprocess.check_output(
                                                              ["nvidia-smi",
                                                               "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                                                               "--format=csv,noheader,nounits"],
                                                              text=True, timeout=2
                                                          ).strip()
                                                          ))
            return (
                "━━━━━━━━━━━GPU━━━━━━━━━━━",
                f"🎮 GPU: {util:.1f}%",
                f"🗄️ VRAM: {used:.0f} / {total:.0f} MiB "
                f"({used/total*100:.1f}%)",
                f"🌡️ GPU Temp: {temp} °C"
            )
        except Exception:
            pass

    # ── 3) GPUtil
    try:
        import GPUtil
        gpu = GPUtil.getGPUs()[0]
        util = gpu.load * 100                           # 0-1 → %
        used = gpu.memoryUsed
        total = gpu.memoryTotal
        temp = gpu.temperature
        return (
            "━━━━━━━━━━━GPU━━━━━━━━━━━",
            f"🎮 GPU: {util:.1f}%",
            f"🗄️ VRAM: {used:.0f} / {total:.0f} MiB "
            f"({used/total*100:.1f}%)",
            f"🌡️ GPU Temp: {temp} °C"
        )
    except Exception:
        return None     # не удалось
def get_cpu_temp() -> str | None:
    # ── 1) стандартный psutil ─────────────────────────────
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ("coretemp", "k10temp", "cpu_thermal"):
                if name in temps and temps[name]:
                    return f"{temps[name][0].current:.1f} °C"
    except Exception:
        pass

    # ── 2) Windows: Open/Libre Hardware Monitor через WMI ─
    if platform.system() == "Windows" and wmi:
        for namespace in ("root\\OpenHardwareMonitor",
                          "root\\LibreHardwareMonitor"):
            try:
                c = wmi.WMI(namespace=namespace)
                sensors = c.Sensor()  # все датчики
                for s in sensors:
                    if s.SensorType == u"Temperature" and "CPU" in s.Name:
                        return f"{s.Value:.1f} °C"
            except Exception:
                continue

    return None
def gather_status() -> str:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    temp_val = get_cpu_temp()
    temp = temp_val if temp_val is not None else "N/A"

    uptime = time.time() - psutil.boot_time()
    lines = [
        "💻 *PC stats*",
        f"⏳ Uptime: {timedelta(seconds=int(uptime))}",
        "━━━━━━━━━━━CPU━━━━━━━━━━━",
        f"🖥️ CPU: {cpu:.1f}%",
        f"🌡️ CPU Temp: {temp}",
        "━━━━━━━━━━━RAM━━━━━━━━━━━",
        f"🧠 RAM: {human_bytes(mem.used)} / {human_bytes(mem.total)} ({mem.percent:.1f}%)",
        f"🧠 SWAP: {human_bytes(swap.used)} / {human_bytes(swap.total)} ({swap.percent:.1f}%)",

    ]
    gpu_lines = gather_gpu()
    disk_lines = gather_disks()
    if gpu_lines:
        lines.extend(gpu_lines)
    lines.extend(disk_lines)
    return "\n".join(lines)
# ────────────────────────── network helpers ───────────────────────────────

session = Session()


def _request(method: str, url: str, **kwargs):
    """Wrapper that tries once with verification, optionally downgrades."""
    global VERIFY_SSL
    try:
        return session.request(method, url, verify=VERIFY_SSL, timeout=10, **kwargs)
    except SSLError as e:
        if VERIFY_SSL is True and VERIFY_ENV != "force" and CA_FILE is None:
            log.warning("SSL verify failed (%s). Falling back to verify=False once.", e)
            VERIFY_SSL = False  # downgrade for all future calls
            return session.request(method, url.replace("https://", "http://"), verify=False, timeout=10, **kwargs)
        raise


def push_status(txt: str):
    try:
        r = _request("POST", f"{SERVER}/api/push/{SECRET}", json={"text": txt})
        r.raise_for_status()
    except Exception as e:
        log.error("push error: %s", e)


def pull_cmds() -> List[str]:
    try:
        r = _request("GET", f"{SERVER}/api/pull/{SECRET}")
        r.raise_for_status(); return r.json().get("commands", [])
    except Exception as e:
        log.error("pull error: %s", e); return []

# ────────────────────────── actions ───────────────────────────────────────

def do_reboot():
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/r", "/t", "0"], shell=False)
        else:
            subprocess.Popen(["sudo", "reboot"], shell=False)
    except Exception as e:
        log.error("reboot failed: %s", e)


def do_shutdown():
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/s", "/t", "0"], shell=False)
        else:
            subprocess.Popen(["sudo", "shutdown", "-h", "now"], shell=False)
    except Exception as e:
        log.error("shutdown failed: %s", e)

# ────────────────────────── main loop ─────────────────────────────────────

log.info("Agent started → %s", SERVER)
while True:
    push_status(gather_status())
    for c in pull_cmds():
        if c == "reboot":
            log.info("cmd reboot"); push_status("⚡️ Rebooting…"); do_reboot()
        elif c == "shutdown":
            log.info("cmd shutdown"); push_status("💤 Shutting down…"); do_shutdown()
    time.sleep(INTERVAL)