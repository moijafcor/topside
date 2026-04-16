from core.collector import BaseCollector, Threshold


class DemoReadiness(BaseCollector):
    name = "demo_readiness"
    interval = 2

    def __init__(self, plugin_cache: dict, config: dict) -> None:
        self._cache = plugin_cache
        self._config = config

    async def collect(self) -> dict:
        ram  = self._cache.get("ram_monitor", {})
        cpu  = self._cache.get("cpu_monitor", {})
        gpu  = self._cache.get("gpu_monitor", {})
        ups  = self._cache.get("ups_monitor", {})

        cfg_thresh  = self._config.get("thresholds", {})
        cfg_drill   = self._config.get("drill_cost", {})
        cfg_ups     = self._config.get("ups", {})
        cfg_earlyoom = self._config.get("earlyoom", {})

        # Current metrics (with safe fallbacks)
        ram_pct  = ram.get("ram_pct", 0.0) or 0.0
        cpu_pct  = cpu.get("aggregate_pct", 0.0) or 0.0
        vram_pct = gpu.get("vram_pct", 0.0) or 0.0

        ram_total_gb = ram.get("ram_total_gb", 0.0) or 0.0
        ram_used_gb  = ram.get("ram_used_gb",  0.0) or 0.0

        swap        = ram.get("swap", {})
        disk_swap   = swap.get("disk", {})
        zram_swap   = swap.get("zram", {})

        disk_swap_pct      = disk_swap.get("pct", 0.0) or 0.0
        disk_swap_vel      = disk_swap.get("velocity_mbps", 0.0) or 0.0
        zram_swap_pct      = zram_swap.get("pct", 0.0) or 0.0
        zram_swap_vel      = zram_swap.get("velocity_mbps", 0.0) or 0.0

        earlyoom_warn = ram.get("earlyoom_browser_warn", False)

        ups_available     = ups.get("ups_available", False)
        on_battery        = ups.get("on_battery", False)
        low_battery       = ups.get("low_battery", False)
        ups_load_pct      = ups.get("ups_load_pct") or 0.0
        battery_charge    = ups.get("battery_charge_pct")

        # Threshold values
        crit_ram  = float(cfg_thresh.get("ram",      {}).get("critical", 85))
        crit_cpu  = float(cfg_thresh.get("cpu",      {}).get("critical", 95))
        crit_vram = float(cfg_thresh.get("gpu_vram", {}).get("critical", 90))

        swap_buf_pct  = float(cfg_thresh.get("swap_proximity_buffer_pct", 8))
        ups_load_warn = float(cfg_ups.get("load_warn",    70))
        ups_load_crit = float(cfg_ups.get("load_critical", 85))

        # ------------------------------------------------------------------
        # Step 1: Hard HOLD overrides
        # ------------------------------------------------------------------
        hold_reasons: list[str] = []

        if disk_swap_vel > 0 and disk_swap_pct > 0:
            hold_reasons.append("Swapping to disk — disk swap active")

        if ups_available:
            if on_battery:
                hold_reasons.append("UPS on battery — do not start drill")
            if low_battery:
                hold_reasons.append("Battery low")
            if ups_load_pct > ups_load_crit:
                hold_reasons.append("UPS load critical")

        # ------------------------------------------------------------------
        # Step 2: EASE_IN floors
        # ------------------------------------------------------------------
        ease_reasons: list[str] = []

        if zram_swap_pct > 0 and zram_swap_vel > 0:
            ease_reasons.append("zram buffer active — RAM pressure building")

        if earlyoom_warn:
            ease_reasons.append("earlyoom may cull browser tabs")

        if ups_available:
            if ups_load_pct > ups_load_warn:
                ease_reasons.append("UPS load high")
            if battery_charge is not None and battery_charge < 30 and not on_battery:
                ease_reasons.append("Battery not fully charged")

        if ram_total_gb > 0:
            swap_proximity = (ram_total_gb - ram_used_gb) / ram_total_gb * 100
            if swap_proximity < swap_buf_pct:
                ease_reasons.append("Approaching swap boundary")

        # ------------------------------------------------------------------
        # Step 3: Headroom model (only when no overrides)
        # ------------------------------------------------------------------
        ram_delta  = float(cfg_drill.get("ram_delta_pct",      8))
        cpu_spike  = float(cfg_drill.get("cpu_spike_pct",     15))
        vram_delta = float(cfg_drill.get("gpu_vram_delta_pct", 6))

        proj_ram  = ram_pct  + ram_delta
        proj_cpu  = cpu_pct  + cpu_spike
        proj_vram = vram_pct + vram_delta

        headroom_ram  = crit_ram  - proj_ram
        headroom_cpu  = crit_cpu  - proj_cpu
        headroom_vram = crit_vram - proj_vram

        headroom = {
            "ram":      round(headroom_ram,  1),
            "cpu":      round(headroom_cpu,  1),
            "gpu_vram": round(headroom_vram, 1),
        }

        # ------------------------------------------------------------------
        # Step 4: Resolve final state
        # ------------------------------------------------------------------
        all_overrides = hold_reasons + ease_reasons

        if hold_reasons:
            state = "HOLD"
            reason = hold_reasons[0]
        elif ease_reasons:
            state = "EASE_IN"
            reason = ease_reasons[0]
        else:
            # Apply headroom model
            headroom_values = [headroom_ram, headroom_cpu, headroom_vram]
            if any(h < 0 for h in headroom_values):
                state = "HOLD"
                # Find which resource is responsible
                labels = ["RAM", "CPU", "GPU VRAM"]
                first = next(l for l, h in zip(labels, headroom_values) if h < 0)
                reason = f"{first} headroom exhausted"
            elif any(h < 10 for h in headroom_values):
                state = "EASE_IN"
                labels = ["RAM", "CPU", "GPU VRAM"]
                first = next(l for l, h in zip(labels, headroom_values) if h < 10)
                reason = f"{first} headroom low"
            else:
                state = "GO"
                reason = "All systems nominal"

        return {
            "state":     state,
            "reason":    reason,
            "overrides": all_overrides,
            "headroom":  headroom,
        }

    def thresholds(self) -> list[Threshold]:
        return []
