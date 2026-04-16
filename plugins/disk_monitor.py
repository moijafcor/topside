"""Disk plugin: volume usage and NVMe I/O throughput."""
import re
import time

import psutil

from core.collector import BaseCollector, Threshold

# Filesystem types worth monitoring
_LOCAL_FS   = {"ext4", "ext3", "ext2", "xfs", "btrfs", "vfat", "ntfs", "f2fs"}
_NETWORK_FS = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "fuse.rclone"}

# Physical block devices only (no partitions, no loops) for I/O rates
_PHYS_DEV_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+)$")

_IOSnap = tuple  # (read_bytes, write_bytes, read_count, write_count, ts)


class DiskMonitor(BaseCollector):
    """Collects filesystem usage and block-device I/O rates."""

    name = "disk_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        self._prev_io: dict[str, _IOSnap] = {}

    async def collect(self) -> dict:
        return {
            "volumes": self._collect_volumes(),
            "io":      self._collect_io(),
        }

    def thresholds(self) -> list[Threshold]:
        cfg      = self._config.get("thresholds", {}).get("disk", {})
        warn     = float(cfg.get("warn",     80))
        critical = float(cfg.get("critical", 90))
        return [
            Threshold("disk_root_warn",     "disk_monitor", "root_pct", "warn",     warn),
            Threshold("disk_root_critical", "disk_monitor", "root_pct", "critical", critical),
        ]

    # ------------------------------------------------------------------
    # Volumes
    # ------------------------------------------------------------------

    def _collect_volumes(self) -> dict:
        local:    list[dict] = []
        network:  list[dict] = []
        root_pct: float | None = None

        try:
            # all=True is required to include CIFS/NFS network mounts
            partitions = psutil.disk_partitions(all=True)
        except OSError:
            return {"local": [], "network": [], "root_pct": None}

        for p in partitions:
            fstype = p.fstype.lower() if p.fstype else ""
            if fstype not in _LOCAL_FS and fstype not in _NETWORK_FS:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except (PermissionError, OSError):
                continue

            entry = {
                "mountpoint": p.mountpoint,
                "device":     p.device,
                "fstype":     fstype,
                "total_gb":   round(u.total / 1024 ** 3, 1),
                "used_gb":    round(u.used  / 1024 ** 3, 1),
                "free_gb":    round(u.free  / 1024 ** 3, 1),
                "pct":        round(u.percent, 1),
            }

            if fstype in _NETWORK_FS:
                network.append(entry)
            else:
                local.append(entry)
                if p.mountpoint == "/":
                    root_pct = entry["pct"]

        local.sort(key=lambda x: (x["mountpoint"] != "/", x["mountpoint"]))
        network.sort(key=lambda x: x["mountpoint"])

        return {"local": local, "network": network, "root_pct": root_pct}

    # ------------------------------------------------------------------
    # I/O throughput
    # ------------------------------------------------------------------

    def _collect_io(self) -> dict:
        now = time.monotonic()
        try:
            counters = psutil.disk_io_counters(perdisk=True)
        except OSError:
            return {}

        result: dict[str, dict] = {}
        for dev, c in counters.items():
            if not _PHYS_DEV_RE.match(dev):
                continue
            result[dev] = self._io_rates(dev, c, now)

        return result

    def _io_rates(self, dev: str, c, now: float) -> dict:
        rb, wb, rc, wc = c.read_bytes, c.write_bytes, c.read_count, c.write_count
        snap = self._prev_io.get(dev)
        self._prev_io[dev] = (rb, wb, rc, wc, now)

        if snap is None:
            return {"read_mbps": 0.0, "write_mbps": 0.0,
                    "read_iops": 0.0, "write_iops": 0.0}

        prb, pwb, prc, pwc, pts = snap
        elapsed = max(now - pts, 0.001)
        mb = 1024 ** 2
        return {
            "read_mbps":  max(round((rb - prb) / elapsed / mb, 2), 0.0),
            "write_mbps": max(round((wb - pwb) / elapsed / mb, 2), 0.0),
            "read_iops":  max(round((rc - prc) / elapsed, 1),       0.0),
            "write_iops": max(round((wc - pwc) / elapsed, 1),       0.0),
        }
