"""UPS plugin — polls APC UPS via apcupsd NIS (stdlib sockets, no nut2)."""
import asyncio
import logging
import socket
import struct
from collections import deque

from core.collector import BaseCollector, Threshold

log = logging.getLogger(__name__)

_DEGRADED: dict = {
    "ups_available":       False,
    "ups_load_pct":        None,
    "ups_realpower_w":     None,
    "input_voltage":       None,
    "battery_charge_pct":  None,
    "battery_runtime_m":   None,
    "ups_status":          None,
    "on_battery":          False,
    "low_battery":         False,
    "power_climbing":      False,
}


def _read_nis_status(host: str, port: int) -> dict[str, str]:
    """Query apcupsd NIS and return a key/value dict.

    Wire protocol (identical to apcaccess):
      - Client sends: 2-byte big-endian length + b"status"
      - Server replies: N length-prefixed records; zero-length record = end
      - Each record is a "KEY : VALUE" line

    Raises OSError on connection failure or timeout.
    """
    cmd = b"status"
    request = struct.pack(">H", len(cmd)) + cmd

    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(request)
        status: dict[str, str] = {}
        buf = b""

        while True:
            header = b""
            while len(header) < 2:
                chunk = sock.recv(2 - len(header))
                if not chunk:
                    return status
                header += chunk

            length = struct.unpack(">H", header)[0]
            if length == 0:
                break

            while len(buf) < length:
                chunk = sock.recv(length - len(buf))
                if not chunk:
                    return status
                buf += chunk

            line = buf[:length].decode("utf-8", errors="replace").strip()
            buf = buf[length:]

            if ":" in line:
                key, _, value = line.partition(":")
                status[key.strip()] = value.strip()

    return status


class UpsMonitor(BaseCollector):
    """Collects UPS metrics from apcupsd NIS; degrades gracefully when unavailable."""

    name = "ups_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        cfg = config.get("ups", {})
        self._host = str(cfg.get("nis_host", "localhost"))
        self._port = int(cfg.get("nis_port", 3551))
        self._power_history: deque[float] = deque(maxlen=3)

    async def collect(self) -> dict:
        loop = asyncio.get_event_loop()
        try:
            raw = await loop.run_in_executor(
                None, _read_nis_status, self._host, self._port
            )
        except OSError as exc:
            log.warning("apcupsd NIS unavailable at %s:%d — %s", self._host, self._port, exc)
            return dict(_DEGRADED)

        try:
            ups_status         = raw.get("STATUS", "")
            ups_load_pct       = self._parse_float(raw.get("LOADPCT"))
            input_voltage      = self._parse_float(raw.get("LINEV"))
            battery_charge_pct = self._parse_float(raw.get("BCHARGE"))
            # TIMELEFT is already in minutes in apcupsd (e.g. "19.4 Minutes")
            battery_runtime_m  = self._parse_float(raw.get("TIMELEFT"))

            # apcupsd does not expose real power directly; derive from load × nominal
            nom_power = self._parse_float(raw.get("NOMPOWER"))
            if ups_load_pct is not None and nom_power is not None:
                ups_realpower_w: float | None = round(ups_load_pct * nom_power / 100)
            else:
                ups_realpower_w = None

            # apcupsd status flags: ONBATT / LOWBATT (not OB/LB as in NUT)
            on_battery  = "ONBATT"  in ups_status
            low_battery = (
                "LOWBATT" in ups_status
                or (battery_charge_pct is not None and battery_charge_pct < 30)
            )

            if ups_realpower_w is not None:
                self._power_history.append(ups_realpower_w)
            power_climbing = False
            if len(self._power_history) == 3:
                h = list(self._power_history)
                power_climbing = h[1] > h[0] and h[2] > h[1]

            return {
                "ups_available":       True,
                "ups_load_pct":        ups_load_pct,
                "ups_realpower_w":     ups_realpower_w,
                "input_voltage":       input_voltage,
                "battery_charge_pct":  battery_charge_pct,
                "battery_runtime_m":   battery_runtime_m,
                "ups_status":          ups_status,
                "on_battery":          on_battery,
                "low_battery":         low_battery,
                "power_climbing":      power_climbing,
            }
        except Exception as exc:
            log.error("UPS data parse error: %s", exc)
            return dict(_DEGRADED)

    def thresholds(self) -> list[Threshold]:
        cfg = self._config.get("ups", {})
        return [
            Threshold("ups_load_warn",     "ups_monitor", "ups_load_pct",       "warn",     float(cfg.get("load_warn",        70))),
            Threshold("ups_load_critical", "ups_monitor", "ups_load_pct",       "critical", float(cfg.get("load_critical",    85))),
            Threshold("ups_batt_warn",     "ups_monitor", "battery_charge_pct", "warn",     float(cfg.get("battery_warn",     50))),
            Threshold("ups_batt_critical", "ups_monitor", "battery_charge_pct", "critical", float(cfg.get("battery_critical", 30))),
        ]

    @staticmethod
    def _parse_float(val: str | None) -> float | None:
        """Extract numeric portion from an apcupsd value string like '31.0 Percent'."""
        if val is None:
            return None
        try:
            return float(val.split()[0])
        except (ValueError, IndexError):
            return None
