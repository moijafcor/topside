from core.collector import BaseCollector, Threshold


class Headroom(BaseCollector):
    """Meta-plugin: reads the plugin cache and emits a composite GO / EASE_IN / HOLD signal."""

    name = "headroom"
    interval = 2

    def __init__(self, plugin_cache: dict, config: dict) -> None:
        self._cache = plugin_cache
        self._config = config

    async def collect(self) -> dict:  # noqa: PLR0912,PLR0914,PLR0915
        ram = self._cache.get("ram_monitor", {})
        cpu = self._cache.get("cpu_monitor", {})
        gpu = self._cache.get("gpu_monitor", {})
        ups = self._cache.get("ups_monitor", {})

        cfg_thresh   = self._config.get("thresholds", {})
        cfg_drill    = self._config.get("drill_cost", {})
        cfg_ups      = self._config.get("ups", {})
        cfg_earlyoom = self._config.get("earlyoom", {})

        # ── Current metrics ───────────────────────────────────────────────
        ram_pct  = ram.get("ram_pct", 0.0) or 0.0
        cpu_pct  = cpu.get("aggregate_pct", 0.0) or 0.0
        vram_pct = gpu.get("vram_pct", 0.0) or 0.0

        ram_total_gb = ram.get("ram_total_gb", 0.0) or 0.0
        ram_used_gb  = ram.get("ram_used_gb",  0.0) or 0.0

        swap      = ram.get("swap", {})
        disk_swap = swap.get("disk", {})
        zram_swap = swap.get("zram", {})

        disk_swap_pct = disk_swap.get("pct", 0.0) or 0.0
        disk_swap_vel = disk_swap.get("velocity_mbps", 0.0) or 0.0
        zram_swap_pct = zram_swap.get("pct", 0.0) or 0.0
        zram_swap_vel = zram_swap.get("velocity_mbps", 0.0) or 0.0

        earlyoom_warn = ram.get("earlyoom_browser_warn", False)

        ups_available   = ups.get("ups_available", False)
        on_battery      = ups.get("on_battery", False)
        low_battery     = ups.get("low_battery", False)
        ups_load_pct    = ups.get("ups_load_pct") or 0.0
        battery_charge  = ups.get("battery_charge_pct")
        battery_runtime = ups.get("battery_runtime_m")

        # ── Thresholds ────────────────────────────────────────────────────
        crit_ram  = float(cfg_thresh.get("ram",      {}).get("critical", 85))
        crit_cpu  = float(cfg_thresh.get("cpu",      {}).get("critical", 95))
        crit_vram = float(cfg_thresh.get("gpu_vram", {}).get("critical", 90))

        swap_buf_pct       = float(cfg_thresh.get("swap_proximity_buffer_pct", 8))
        ups_load_warn      = float(cfg_ups.get("load_warn",      70))
        ups_load_crit      = float(cfg_ups.get("load_critical",  85))
        ups_battery_warn   = float(cfg_ups.get("battery_warn",   50))
        ups_runtime_warn_m = float(cfg_ups.get("runtime_warn_m", 10))

        # ── Drill-cost projections ────────────────────────────────────────
        ram_delta     = float(cfg_drill.get("ram_delta_pct",       8))
        cpu_spike     = float(cfg_drill.get("cpu_spike_pct",      15))
        vram_delta    = float(cfg_drill.get("gpu_vram_delta_pct",  6))
        ease_in_floor = float(cfg_drill.get("headroom_ease_in_pct", 5))

        proj_ram  = ram_pct  + ram_delta
        proj_cpu  = cpu_pct  + cpu_spike
        proj_vram = vram_pct + vram_delta

        headroom_ram  = crit_ram  - proj_ram
        headroom_cpu  = crit_cpu  - proj_cpu
        headroom_vram = crit_vram - proj_vram

        # ── Step 1: Hard HOLD ─────────────────────────────────────────────
        # Conditions that are happening right now and block all drills.
        hold_reasons: list[str] = []

        if disk_swap_vel > 0 and disk_swap_pct > 0:
            hold_reasons.append("Swapping to disk")

        if ups_available:
            if on_battery:
                hold_reasons.append("UPS on battery")
            if low_battery:
                hold_reasons.append("Battery low")
            if ups_load_pct > ups_load_crit:
                hold_reasons.append(f"UPS load critical ({ups_load_pct:.0f}%)")

        # Resources already above their critical threshold right now.
        if ram_pct >= crit_ram:
            hold_reasons.append(f"RAM at critical ({ram_pct:.0f}% ≥ {crit_ram:.0f}%)")
        if cpu_pct >= crit_cpu:
            hold_reasons.append(f"CPU at critical ({cpu_pct:.0f}% ≥ {crit_cpu:.0f}%)")
        if vram_pct >= crit_vram:
            hold_reasons.append(f"VRAM at critical ({vram_pct:.0f}% ≥ {crit_vram:.0f}%)")

        # ── Step 2: EASE_IN floors ────────────────────────────────────────
        # Conditions that warrant caution but don't block outright.
        ease_reasons: list[str] = []

        # zram: require > 1 MB/s to filter marginal background writes.
        if zram_swap_pct > 0 and zram_swap_vel >= 1.0:
            ease_reasons.append("zram buffer active — RAM pressure building")

        earlyoom_pressure_floor = float(cfg_earlyoom.get("ram_pressure_floor", 70))
        if earlyoom_warn and ram_pct >= earlyoom_pressure_floor:
            ease_reasons.append("earlyoom may cull browser tabs")

        if ups_available:
            if ups_load_pct > ups_load_warn:
                ease_reasons.append(f"UPS load high ({ups_load_pct:.0f}%)")
            if (battery_charge is not None
                    and battery_charge < ups_battery_warn
                    and not on_battery):
                ease_reasons.append(f"Battery charge low ({battery_charge:.0f}%)")
            if battery_runtime is not None and battery_runtime < ups_runtime_warn_m:
                ease_reasons.append(f"UPS runtime low ({battery_runtime:.0f} min)")

        if ram_total_gb > 0:
            swap_proximity = (ram_total_gb - ram_used_gb) / ram_total_gb * 100
            if swap_proximity < swap_buf_pct:
                ease_reasons.append("Approaching swap boundary")

        # ── Step 3: Headroom projections → EASE_IN ───────────────────────
        # Negative projected headroom means a drill would breach the critical
        # threshold, but the system is currently stable — EASE_IN, not HOLD.
        # Resources already above threshold are caught in Step 1 above.
        for label, headroom, proj, crit in (
            ("RAM",  headroom_ram,  proj_ram,  crit_ram),
            ("CPU",  headroom_cpu,  proj_cpu,  crit_cpu),
            ("VRAM", headroom_vram, proj_vram, crit_vram),
        ):
            if headroom < 0:
                ease_reasons.append(
                    f"{label} headroom exhausted"
                    f" — drill would reach {proj:.0f}% vs {crit:.0f}% crit"
                )
            elif headroom < ease_in_floor:
                ease_reasons.append(f"{label} headroom low ({headroom:.1f}%)")

        # ── Step 4: Resolve final state ───────────────────────────────────
        all_overrides = hold_reasons + ease_reasons

        if hold_reasons:
            state = "HOLD"
            reason = hold_reasons[0]
        elif ease_reasons:
            state = "EASE_IN"
            reason = ease_reasons[0]
        else:
            state = "GO"
            reason = "All systems nominal"

        return {
            "state":     state,
            "reason":    reason,
            "overrides": [o for o in all_overrides if o != reason],
            "headroom":  {
                "ram":      round(headroom_ram,  1),
                "cpu":      round(headroom_cpu,  1),
                "gpu_vram": round(headroom_vram, 1),
            },
            "breakdown": {
                "ram": {
                    "current": round(ram_pct, 1), "delta": ram_delta,
                    "projected": round(proj_ram, 1), "threshold": crit_ram,
                },
                "cpu": {
                    "current": round(cpu_pct, 1), "delta": cpu_spike,
                    "projected": round(proj_cpu, 1), "threshold": crit_cpu,
                },
                "gpu_vram": {
                    "current": round(vram_pct, 1), "delta": vram_delta,
                    "projected": round(proj_vram, 1), "threshold": crit_vram,
                },
            },
        }

    def thresholds(self) -> list[Threshold]:
        return []
