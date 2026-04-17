import asyncio
import logging
import re
import time
import urllib.request

from core.collector import BaseCollector, Threshold

log = logging.getLogger(__name__)

_LABEL_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+([\d.eE+\-]+)')
_PLAIN_RE  = re.compile(r'^(\w+)\s+([\d.eE+\-]+)')
_MODEL_RE  = re.compile(r'model="([^"]+)"')


def _fetch_metrics(url: str, timeout: float = 3.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        log.debug("ollama_tokens fetch %s: %s", url, exc)
        return None


def _parse_metrics(raw: bytes) -> dict[str, dict[str, float]]:
    """Parse Prometheus text format. Returns {metric_name: {model_label: value}}."""
    result: dict[str, dict[str, float]] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LABEL_RE.match(line)
        if m:
            name, labels_str, value_str = m.group(1), m.group(2), m.group(3)
            model_m = _MODEL_RE.search(labels_str)
            model = model_m.group(1) if model_m else "__all__"
            result.setdefault(name, {})[model] = float(value_str)
            continue
        m = _PLAIN_RE.match(line)
        if m:
            result.setdefault(m.group(1), {})["__all__"] = float(m.group(2))
    return result


class OllamaTokens(BaseCollector):
    name     = "ollama_tokens"
    interval = 5

    def __init__(self, config: dict) -> None:
        self._config  = config
        base          = config.get("ollama", {}).get("base_url", "http://localhost:11434")
        self._url     = base.rstrip("/") + "/metrics"
        self._prev:    dict[str, dict[str, float]] = {}
        self._prev_ts: float = 0.0

    async def collect(self) -> dict:
        loop = asyncio.get_event_loop()
        raw  = await loop.run_in_executor(None, _fetch_metrics, self._url)

        if raw is None:
            return {
                "metrics_available":       False,
                "prompt_tokens_per_s":     0.0,
                "completion_tokens_per_s": 0.0,
                "total_tokens_per_s":      0.0,
                "prompt_tokens_total":     0,
                "completion_tokens_total": 0,
                "requests_total":          0,
                "models":                  [],
            }

        now     = time.monotonic()
        current = _parse_metrics(raw)
        elapsed = now - self._prev_ts if self._prev_ts else 0.0

        def _delta(metric: str, model: str) -> float:
            cur = current.get(metric, {}).get(model, 0.0)
            prv = self._prev.get(metric, {}).get(model, 0.0)
            return max(cur - prv, 0.0)  # clamp negative on counter reset

        def _rate(metric: str, model: str) -> float:
            if elapsed <= 0:
                return 0.0
            return round(_delta(metric, model) / elapsed, 2)

        def _total(metric: str) -> int:
            return int(sum(current.get(metric, {}).values()))

        comp_metric   = "ollama_completion_tokens_total"
        prompt_metric = "ollama_prompt_tokens_total"
        req_metric    = "ollama_request_duration_seconds_count"

        all_models = (
            set(current.get(comp_metric,   {}).keys()) |
            set(current.get(prompt_metric, {}).keys())
        ) - {"__all__"}

        agg_comp   = sum(_rate(comp_metric,   m) for m in all_models)
        agg_prompt = sum(_rate(prompt_metric, m) for m in all_models)

        models = [
            {
                "name":                    m,
                "completion_tokens_per_s": _rate(comp_metric,   m),
                "prompt_tokens_per_s":     _rate(prompt_metric, m),
                "requests_total":          int(current.get(req_metric, {}).get(m, 0)),
            }
            for m in sorted(all_models)
        ]

        self._prev    = current
        self._prev_ts = now

        return {
            "metrics_available":       True,
            "prompt_tokens_per_s":     round(agg_prompt, 2),
            "completion_tokens_per_s": round(agg_comp, 2),
            "total_tokens_per_s":      round(agg_comp + agg_prompt, 2),
            "prompt_tokens_total":     _total(prompt_metric),
            "completion_tokens_total": _total(comp_metric),
            "requests_total":          _total(req_metric),
            "models":                  models,
        }

    def thresholds(self) -> list[Threshold]:
        warn = (
            self._config.get("ollama", {})
                        .get("tokens", {})
                        .get("warn_completion_per_s", 80)
        )
        return [
            Threshold(
                name="ollama_completion_throughput",
                plugin=self.name,
                metric_key="completion_tokens_per_s",
                level="warn",
                value=warn,
                direction="above",
            )
        ]
