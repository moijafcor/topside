import asyncio
import json
import logging
import urllib.request
from datetime import datetime, timezone

from core.collector import BaseCollector, Threshold

log = logging.getLogger(__name__)

# Sentinel year Ollama uses when a model is loaded with keepalive=0 (never expires)
_KEEPALIVE_YEAR = 2200


def _fetch(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.debug("Ollama fetch %s: %s", url, exc)
        return None


class OllamaMonitor(BaseCollector):
    name = "ollama_monitor"
    interval = 5

    def __init__(self, config: dict) -> None:
        self._config = config
        base = config.get("ollama", {}).get("base_url", "http://localhost:11434")
        self._base = base.rstrip("/")

    async def collect(self) -> dict:
        loop = asyncio.get_event_loop()

        ps_data, tags_data = await asyncio.gather(
            loop.run_in_executor(None, _fetch, f"{self._base}/api/ps"),
            loop.run_in_executor(None, _fetch, f"{self._base}/api/tags"),
        )

        if ps_data is None and tags_data is None:
            return {
                "available": False,
                "version": None,
                "loaded_models": [],
                "total_models": 0,
                "total_vram_gb": 0.0,
            }

        loaded_models = []
        total_vram_bytes = 0

        for m in (ps_data or {}).get("models", []):
            size_vram = m.get("size_vram", 0) or 0
            total_vram_bytes += size_vram

            expires_in_s = self._parse_expires(m.get("expires_at"))
            details = m.get("details", {})

            loaded_models.append({
                "name":         m.get("name", ""),
                "family":       details.get("family", ""),
                "params":       details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "size_vram_gb": round(size_vram / 1024 ** 3, 2),
                "context_length": m.get("context_length"),
                "expires_in_s": expires_in_s,   # None = keepalive/never
            })

        total_models = len((tags_data or {}).get("models", []))

        return {
            "available":     True,
            "version":       None,          # polled separately only on startup if needed
            "loaded_models": loaded_models,
            "total_models":  total_models,
            "total_vram_gb": round(total_vram_bytes / 1024 ** 3, 2),
        }

    def thresholds(self) -> list[Threshold]:
        return []

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_expires(expires_at: str | None) -> int | None:
        if not expires_at:
            return None
        try:
            # Strip nanosecond precision Python can't parse (keep 6 decimal places)
            ts = expires_at
            # Handle timezone offset with colon (e.g. -04:00)
            # datetime.fromisoformat handles this in Python 3.11+; use manual strip for 3.12
            # Truncate sub-microsecond digits
            import re
            ts = re.sub(r'(\.\d{6})\d+', r'\1', ts)
            dt = datetime.fromisoformat(ts)
            if dt.year >= _KEEPALIVE_YEAR:
                return None  # loaded indefinitely
            now = datetime.now(tz=timezone.utc)
            delta = int((dt.astimezone(timezone.utc) - now).total_seconds())
            return max(delta, 0)
        except Exception:
            return None
