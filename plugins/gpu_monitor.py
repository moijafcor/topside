import logging

from core.collector import BaseCollector, Threshold

log = logging.getLogger(__name__)

try:
    import pynvml
    _NVML_AVAILABLE = True
except ImportError:
    _NVML_AVAILABLE = False
    log.warning("pynvml not installed — GPU monitoring unavailable")


class GpuMonitor(BaseCollector):
    name = "gpu_monitor"
    interval = 2

    def __init__(self, config: dict) -> None:
        self._config = config
        self._handle = None
        self._init_nvml()

    def _init_nvml(self) -> None:
        if not _NVML_AVAILABLE:
            return
        try:
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            log.info("pynvml initialized: %s", pynvml.nvmlDeviceGetName(self._handle))
        except Exception as exc:
            log.error("pynvml init failed: %s", exc)
            self._handle = None

    async def collect(self) -> dict:
        if not _NVML_AVAILABLE or self._handle is None:
            return {"error": "GPU monitoring unavailable — pynvml not initialized"}

        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self._handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            temp = pynvml.nvmlDeviceGetTemperature(self._handle, pynvml.NVML_TEMPERATURE_GPU)
            power_mw = pynvml.nvmlDeviceGetPowerUsage(self._handle)

            vram_used_gb = mem.used / 1024 ** 3
            vram_total_gb = mem.total / 1024 ** 3
            vram_pct = (mem.used / mem.total * 100) if mem.total > 0 else 0.0

            return {
                "util_pct":      round(util.gpu, 1),
                "vram_used_gb":  round(vram_used_gb, 2),
                "vram_total_gb": round(vram_total_gb, 2),
                "vram_pct":      round(vram_pct, 1),
                "temp_c":        float(temp),
                "power_w":       round(power_mw / 1000, 1),
            }
        except Exception as exc:
            log.error("pynvml collect error: %s", exc)
            return {"error": str(exc)}

    def thresholds(self) -> list[Threshold]:
        cfg = self._config.get("thresholds", {}).get("gpu_vram", {})
        return [
            Threshold("gpu_vram_warn",     "gpu_monitor", "vram_pct", "warn",     float(cfg.get("warn",     75))),
            Threshold("gpu_vram_critical", "gpu_monitor", "vram_pct", "critical", float(cfg.get("critical", 90))),
        ]
