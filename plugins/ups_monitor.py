import logging
from collections import deque

from core.collector import BaseCollector, Threshold

log = logging.getLogger(__name__)

try:
    from nut2 import PyNUTClient
    _NUT_AVAILABLE = True
except ImportError:
    _NUT_AVAILABLE = False
    log.warning("nut2 not installed — UPS monitoring unavailable")

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


class UpsMonitor(BaseCollector):
    name = "ups_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        self._device = config.get("ups", {}).get("device_name", "ups")
        self._power_history: deque[float] = deque(maxlen=3)

    async def collect(self) -> dict:
        if not _NUT_AVAILABLE:
            return dict(_DEGRADED)

        try:
            client = PyNUTClient()
            vars_ = client.list_vars(self._device)
        except Exception as exc:
            log.warning("NUT daemon unavailable: %s", exc)
            return dict(_DEGRADED)

        try:
            ups_load_pct = self._float(vars_.get("ups.load"))
            input_voltage = self._float(vars_.get("input.voltage"))
            battery_charge_pct = self._float(vars_.get("battery.charge"))
            battery_runtime_s = self._float(vars_.get("battery.runtime"))
            battery_runtime_m = round(battery_runtime_s / 60, 1) if battery_runtime_s is not None else None
            ups_status = vars_.get("ups.status", "")

            # ups.realpower may be absent — fall back gracefully
            ups_realpower_w = self._float(vars_.get("ups.realpower"))

            on_battery = "OB" in ups_status if ups_status else False
            low_battery = (
                ("LB" in ups_status if ups_status else False)
                or (battery_charge_pct is not None and battery_charge_pct < 30)
            )

            # power_climbing: positive deltas across last 3 readings
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
            Threshold("ups_load_warn",     "ups_monitor", "ups_load_pct",       "warn",     float(cfg.get("load_warn",       70))),
            Threshold("ups_load_critical", "ups_monitor", "ups_load_pct",       "critical", float(cfg.get("load_critical",   85))),
            Threshold("ups_batt_warn",     "ups_monitor", "battery_charge_pct", "warn",     float(cfg.get("battery_warn",    50))),
            Threshold("ups_batt_critical", "ups_monitor", "battery_charge_pct", "critical", float(cfg.get("battery_critical", 30))),
        ]

    @staticmethod
    def _float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
