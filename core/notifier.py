import logging
import os
import subprocess
from pathlib import Path

from core.collector import Threshold

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: dict) -> None:
        self._config = config
        # Edge-trigger state: threshold.name -> bool (True = alert already fired)
        self._state: dict[str, bool] = {}
        self._prev_demo_state: str | None = None
        self._disk_swap_active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, plugin_name: str, payload: dict, thresholds: list[Threshold]) -> None:
        notif = self._config.get("notifications", {})
        for t in thresholds:
            metric = payload.get(t.metric_key)
            if metric is None:
                continue
            fired = self._state.get(t.name, False)
            over = metric >= t.value
            if over and not fired:
                self._state[t.name] = True
                urgency = "critical" if t.level == "critical" else "normal"
                title = f"TOPSIDE — {plugin_name} {t.level.upper()}"
                body = f"{t.metric_key} = {metric:.1f} (threshold {t.value})"
                if notif.get("desktop", False):
                    self._dispatch_desktop(title, body, urgency)
                if notif.get("opswire", False):
                    severity = "CRITICAL" if t.level == "critical" else notif.get("opswire_severity", "WARN")
                    self._dispatch_opswire(severity, f"{title}: {body}")
            elif not over and fired:
                # Hysteresis: reset only when below warn level
                warn_threshold = self._warn_for(plugin_name, t.metric_key, thresholds)
                if warn_threshold is None or metric < warn_threshold:
                    self._state[t.name] = False

    def notify_demo_state(self, new_state: str) -> None:
        old = self._prev_demo_state
        self._prev_demo_state = new_state
        if old is None or old == new_state:
            return
        notif = self._config.get("notifications", {})
        if new_state == "HOLD":
            severity, urgency = "CRITICAL", "critical"
            title = "TOPSIDE — HOLD"
            body = "Demo readiness: HOLD — do not start drill"
        elif new_state == "EASE_IN" and old == "GO":
            severity, urgency = "WARN", "normal"
            title = "TOPSIDE — EASE_IN"
            body = "Demo readiness degraded to EASE_IN"
        elif new_state == "GO" and old == "HOLD":
            severity, urgency = "INFO", "low"
            title = "TOPSIDE — All clear"
            body = "Demo readiness restored to GO"
        else:
            return
        if notif.get("desktop", False):
            self._dispatch_desktop(title, body, urgency)
        if notif.get("opswire", False):
            self._dispatch_opswire(severity, f"{title}: {body}")

    def notify_disk_swap_activated(self) -> None:
        if self._disk_swap_active:
            return
        self._disk_swap_active = True
        notif = self._config.get("notifications", {})
        title = "TOPSIDE — CRITICAL: Disk swap active"
        body = "System is swapping to disk"
        if notif.get("desktop", False):
            self._dispatch_desktop(title, body, "critical")
        if notif.get("opswire", False):
            self._dispatch_opswire("CRITICAL", f"{title}: {body}")

    def notify_disk_swap_cleared(self) -> None:
        self._disk_swap_active = False

    def notify_earlyoom_warning(self) -> None:
        notif = self._config.get("notifications", {})
        title = "TOPSIDE — WARN: earlyoom threshold"
        body = "Browser RSS exceeds earlyoom warning threshold"
        if notif.get("desktop", False):
            self._dispatch_desktop(title, body, "normal")
        if notif.get("opswire", False):
            self._dispatch_opswire("WARN", f"{title}: {body}")

    def reload(self, config: dict) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_desktop(self, title: str, body: str, urgency: str) -> None:
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, title, body],
                check=False,
                timeout=3,
            )
        except Exception as exc:
            log.warning("notify-send failed: %s", exc)

    def _dispatch_opswire(self, severity: str, message: str) -> None:
        notif = self._config.get("notifications", {})
        script = notif.get("opswire_script", "")
        if not script:
            return
        script_path = Path(os.path.expanduser(script))
        if not script_path.exists():
            log.warning("opswire_script not found: %s", script_path)
            return
        try:
            subprocess.run(
                [str(script_path), severity, message],
                check=False,
                timeout=5,
            )
        except Exception as exc:
            log.warning("opswire dispatch failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _warn_for(plugin_name: str, metric_key: str, thresholds: list[Threshold]) -> float | None:
        for t in thresholds:
            if t.plugin == plugin_name and t.metric_key == metric_key and t.level == "warn":
                return t.value
        return None
