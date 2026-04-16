import os

import psutil

from core.collector import BaseCollector, Threshold


class CpuMonitor(BaseCollector):
    name = "cpu_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config

    async def collect(self) -> dict:
        per_core = psutil.cpu_percent(percpu=True)
        aggregate = psutil.cpu_percent(percpu=False)
        load_1, load_5, load_15 = os.getloadavg()

        freqs = psutil.cpu_freq(percpu=True)
        if freqs:
            freq_mhz = [round(f.current, 1) for f in freqs]
        else:
            # Fallback: single freq reported for all cores
            single = psutil.cpu_freq(percpu=False)
            freq_mhz = [round(single.current, 1)] * len(per_core) if single else []

        return {
            "per_core_pct": [round(v, 1) for v in per_core],
            "aggregate_pct": round(aggregate, 1),
            "load_avg": {
                "1m":  round(load_1,  2),
                "5m":  round(load_5,  2),
                "15m": round(load_15, 2),
            },
            "freq_mhz": freq_mhz,
        }

    def thresholds(self) -> list[Threshold]:
        cfg = self._config.get("thresholds", {}).get("cpu", {})
        return [
            Threshold("cpu_warn",     "cpu_monitor", "aggregate_pct", "warn",     float(cfg.get("warn",     80))),
            Threshold("cpu_critical", "cpu_monitor", "aggregate_pct", "critical", float(cfg.get("critical", 95))),
        ]
