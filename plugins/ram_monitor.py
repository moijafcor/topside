import os
import re
import time
from pathlib import Path

import psutil

from core.collector import BaseCollector, Threshold

BROWSER_NAMES: dict[str, str] = {
    "chrome": "Chrome",
    "chromium": "Chrome",
    "chromium-browser": "Chrome",
    "firefox": "Firefox",
    "firefox-esr": "Firefox",
    "brave": "Brave",
    "brave-browser": "Brave",
    "msedge": "Edge",
    "microsoft-edge": "Edge",
    "microsoft-edge-stable": "Edge",
}

# Match disk swap device paths: /dev/sdX or /dev/nvmeXnX or /dev/vdX
_DISK_SWAP_RE = re.compile(r"^/dev/(sd[a-z]+|nvme\d+n\d+|vd[a-z]+)")


class RamMonitor(BaseCollector):
    name = "ram_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        self._prev_swap_used: dict[str, float] = {}  # device_path -> used_bytes
        self._prev_time: float = time.monotonic()

    async def collect(self) -> dict:
        vm = psutil.virtual_memory()
        ram_total_gb = vm.total / 1024 ** 3
        ram_used_gb = vm.used / 1024 ** 3
        ram_free_gb = vm.available / 1024 ** 3
        ram_pct = vm.percent

        swap = self._collect_swap()
        top_procs, earlyoom_warn = self._collect_processes(ram_total_gb)

        return {
            "ram_total_gb": round(ram_total_gb, 2),
            "ram_used_gb": round(ram_used_gb, 2),
            "ram_free_gb": round(ram_free_gb, 2),
            "ram_pct": round(ram_pct, 1),
            "swap": swap,
            "top_processes": top_procs,
            "earlyoom_browser_warn": earlyoom_warn,
        }

    def thresholds(self) -> list[Threshold]:
        cfg = self._config.get("thresholds", {}).get("ram", {})
        return [
            Threshold("ram_warn",     "ram_monitor", "ram_pct", "warn",     float(cfg.get("warn",     70))),
            Threshold("ram_critical", "ram_monitor", "ram_pct", "critical", float(cfg.get("critical", 85))),
        ]

    # ------------------------------------------------------------------
    # Swap collection
    # ------------------------------------------------------------------

    def _collect_swap(self) -> dict:
        now = time.monotonic()
        elapsed = max(now - self._prev_time, 0.001)
        self._prev_time = now

        zram_used = zram_total = 0.0
        disk_used = disk_total = 0.0
        zram_velocity = disk_velocity = 0.0

        try:
            with open("/proc/swaps") as f:
                lines = f.readlines()[1:]  # skip header
        except OSError:
            lines = []

        new_snap: dict[str, float] = {}

        for line in lines:
            parts = line.split()
            if len(parts) < 5:
                continue
            device, _type, size_kb, used_kb, _prio = parts[0], parts[1], parts[2], parts[3], parts[4]
            size_bytes = int(size_kb) * 1024
            used_bytes = int(used_kb) * 1024
            new_snap[device] = used_bytes

            prev = self._prev_swap_used.get(device, used_bytes)
            delta_bytes = used_bytes - prev
            vel_mbps = delta_bytes / elapsed / (1024 * 1024)

            if "zram" in device:
                zram_used += used_bytes
                zram_total += size_bytes
                zram_velocity += vel_mbps
            elif _DISK_SWAP_RE.match(device):
                disk_used += used_bytes
                disk_total += size_bytes
                disk_velocity += vel_mbps

        self._prev_swap_used = new_snap

        def to_gb(b: float) -> float:
            return round(b / 1024 ** 3, 3)

        def pct(used: float, total: float) -> float:
            return round(used / total * 100, 1) if total > 0 else 0.0

        zram_result: dict = {
            "used_gb": to_gb(zram_used),
            "total_gb": to_gb(zram_total),
            "pct": pct(zram_used, zram_total),
            "velocity_mbps": round(max(zram_velocity, 0), 3),
            "compression_ratio": self._zram_compression_ratio(),
        }

        disk_result: dict = {
            "used_gb": to_gb(disk_used),
            "total_gb": to_gb(disk_total),
            "pct": pct(disk_used, disk_total),
            "velocity_mbps": round(max(disk_velocity, 0), 3),
        }

        return {"zram": zram_result, "disk": disk_result}

    @staticmethod
    def _zram_compression_ratio() -> float | None:
        for i in range(8):
            mm_stat = Path(f"/sys/block/zram{i}/mm_stat")
            if not mm_stat.exists():
                continue
            try:
                parts = mm_stat.read_text().split()
                # Fields: orig_data_size compr_data_size mem_used_total ...
                orig = int(parts[0])
                compr = int(parts[1])
                if compr > 0 and orig > 0:
                    return round(orig / compr, 2)
            except (OSError, ValueError, IndexError):
                continue
        return None

    # ------------------------------------------------------------------
    # Process collection
    # ------------------------------------------------------------------

    def _collect_processes(self, ram_total_gb: float) -> tuple[list[dict], bool]:
        earlyoom_pct = self._config.get("earlyoom", {}).get("browser_warn_pct", 20)
        earlyoom_bytes = earlyoom_pct / 100 * ram_total_gb * 1024 ** 3

        # Aggregate by exe basename
        groups: dict[str, dict] = {}
        for proc in psutil.process_iter(["name", "exe", "memory_info", "cmdline", "pid"]):
            try:
                info = proc.info
                exe = info.get("exe") or ""
                name = os.path.basename(exe) if exe else (info.get("name") or "")
                rss = info["memory_info"].rss if info.get("memory_info") else 0
                cmdline = info.get("cmdline") or []

                if name not in groups:
                    groups[name] = {"rss": 0, "count": 0, "renderer_count": 0, "cmdline_sample": cmdline}
                groups[name]["rss"] += rss
                groups[name]["count"] += 1
                if "--type=renderer" in cmdline:
                    groups[name]["renderer_count"] += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by total RSS descending, take top 10
        sorted_groups = sorted(groups.items(), key=lambda x: x[1]["rss"], reverse=True)[:10]

        earlyoom_warn = False
        top_procs = []
        for exe_name, data in sorted_groups:
            browser_label = BROWSER_NAMES.get(exe_name.lower())
            renderer_count = data["renderer_count"] if browser_label else None
            rss_bytes = data["rss"]

            if browser_label and rss_bytes >= earlyoom_bytes:
                earlyoom_warn = True

            top_procs.append({
                "name": exe_name,
                "rss_mb": round(rss_bytes / 1024 ** 2, 1),
                "label": f"Browser ({browser_label})" if browser_label else None,
                "renderer_count": renderer_count,
                "earlyoom_risk": bool(browser_label and rss_bytes >= earlyoom_bytes),
            })

        return top_procs, earlyoom_warn
