"""Network plugin: per-interface RX/TX throughput and error counts."""
import time
from pathlib import Path

from core.collector import BaseCollector, Threshold

_PROC_NET_DEV = Path("/proc/net/dev")

# Interfaces to skip regardless of activity
_SKIP_PREFIXES = ("lo", "docker", "veth", "virbr", "br-", "dummy")


def _skip(name: str) -> bool:
    return any(name.startswith(p) for p in _SKIP_PREFIXES)


def _parse_proc_net_dev() -> dict[str, dict]:
    """Parse /proc/net/dev into a dict keyed by interface name.

    Each value is a dict with the raw cumulative counters from the kernel.
    Fields: rx_bytes, rx_packets, rx_errors, rx_drop,
            tx_bytes, tx_packets, tx_errors, tx_drop
    """
    result: dict[str, dict] = {}
    lines = _PROC_NET_DEV.read_text().splitlines()
    for line in lines[2:]:          # skip the two header lines
        parts = line.split()
        if len(parts) < 17:
            continue
        name = parts[0].rstrip(":")
        if _skip(name):
            continue
        result[name] = {
            "rx_bytes":   int(parts[1]),
            "rx_packets": int(parts[2]),
            "rx_errors":  int(parts[3]),
            "rx_drop":    int(parts[4]),
            "tx_bytes":   int(parts[9]),
            "tx_packets": int(parts[10]),
            "tx_errors":  int(parts[11]),
            "tx_drop":    int(parts[12]),
        }
    return result


class NetworkMonitor(BaseCollector):
    """Collects per-interface network throughput from /proc/net/dev."""

    name = "network_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        self._prev: dict[str, dict] = {}
        self._prev_ts: float = 0.0

    async def collect(self) -> dict:
        now  = time.monotonic()
        snap = _parse_proc_net_dev()
        elapsed = max(now - self._prev_ts, 0.001) if self._prev_ts else None

        interfaces: dict[str, dict] = {}
        for name, cur in snap.items():
            prev = self._prev.get(name)
            if prev is None or elapsed is None:
                interfaces[name] = {
                    "rx_mbps":   0.0, "tx_mbps":   0.0,
                    "rx_pps":    0.0, "tx_pps":    0.0,
                    "rx_errors": cur["rx_errors"],
                    "tx_errors": cur["tx_errors"],
                }
            else:
                mb = 1024 ** 2
                interfaces[name] = {
                    "rx_mbps":   max(round((cur["rx_bytes"]   - prev["rx_bytes"])   / elapsed / mb, 3), 0.0),
                    "tx_mbps":   max(round((cur["tx_bytes"]   - prev["tx_bytes"])   / elapsed / mb, 3), 0.0),
                    "rx_pps":    max(round((cur["rx_packets"] - prev["rx_packets"]) / elapsed, 1), 0.0),
                    "tx_pps":    max(round((cur["tx_packets"] - prev["tx_packets"]) / elapsed, 1), 0.0),
                    "rx_errors": cur["rx_errors"],
                    "tx_errors": cur["tx_errors"],
                }

        self._prev    = snap
        self._prev_ts = now
        return {"interfaces": interfaces}

    def thresholds(self) -> list[Threshold]:
        return []
