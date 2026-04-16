# TOPSIDE

TOPSIDE is a local-first, plugin-driven system monitor built for workstations running inference workloads, live demos, or anything where a RAM ambush or power event is not an option.

A FastAPI backend pushes live metrics over WebSocket to a zero-build-step HTML dashboard. A plugin contract means new collectors are a single file drop. A Headroom strip — pinned, always visible, peripheral-friendly — gives you a one-glance **GO / EASE_IN / HOLD** signal before you fire the next drill.

No cloud. No agents. No npm. Runs anywhere Python 3.12 runs.

---

## What it monitors

| Plugin | Metrics | Interval |
| --- | --- | --- |
| `ram_monitor` | Used / free / total RAM, swap (zram + disk) with velocity and compression ratio, top-10 processes grouped by executable with browser tab estimates and earlyoom risk flags | 2 s |
| `cpu_monitor` | Per-core utilization, aggregate %, load averages (1 m / 5 m / 15 m), per-core frequency | 2 s |
| `gpu_monitor` | GPU utilization %, VRAM used / total / %, temperature °C, power draw W — via pynvml, no nvidia-smi subprocess | 2 s |
| `ups_monitor` | Load %, real power W, input voltage, battery charge %, runtime estimate, on-battery / low-battery flags, power-climbing trend — via apcupsd NIS (TCP 3551, stdlib sockets), graceful degraded mode when unavailable | 2 s |
| `ollama_monitor` | Loaded models with VRAM footprint, parameters, quantization, context length, and keepalive / unload timer — via Ollama REST API, no subprocess | 5 s |
| `headroom` | Composite **GO / EASE_IN / HOLD** state with primary reason, full override list, and per-resource headroom projections | 2 s |

---

## Headroom

The `headroom` meta-plugin reads the latest output from all other plugins and emits a single composite signal every 2 s.

### State resolution (in priority order)

1. **Hard HOLD** — any of:
   - Disk swap active (velocity > 0 and used > 0 %)
   - UPS on battery
   - Battery low (status `LB` or charge < 30 %)
   - UPS load above critical threshold

2. **EASE_IN floor** — any of:
   - zram swap active and growing (compressed RAM pressure)
   - Browser RSS exceeds `earlyoom.browser_warn_pct` **and** overall RAM exceeds `earlyoom.ram_pressure_floor` — both conditions required to avoid false positives on systems with large browser sessions at idle
   - UPS load above warn threshold
   - Battery charge < 30 % while on mains
   - Free RAM within `swap_proximity_buffer_pct` of swap boundary

3. **Headroom model** — projects whether the next drill fits given configured `drill_cost` deltas:

   ```text
   headroom_ram  = threshold.critical.ram      − (current_ram_pct  + drill_cost.ram_delta_pct)
   headroom_cpu  = threshold.critical.cpu      − (current_cpu_pct  + drill_cost.cpu_spike_pct)
   headroom_vram = threshold.critical.gpu_vram − (current_vram_pct + drill_cost.gpu_vram_delta_pct)

   any < 0                          → HOLD
   any < headroom_ease_in_pct (5 %) → EASE_IN
   else                             → GO
   ```

   The `headroom_ease_in_pct` default of 5 % is intentionally tighter than a naive 10 % cutoff. Systems running local LLMs maintain a high VRAM baseline (model weights stay resident); a 10 % floor produced false EASE_IN alerts at idle.

### Dashboard strip

A fixed 40 px bar pinned to the top of every page. Never scrolls away.

```text
[ ● GO ]   RAM 41%   CPU 12%   GPU 18%   VRAM 38%   PWR 14W   UPS 340W   LLM qwen2.5:14b
```

Conditional annotations appear inline: `SWAP ▲ 340 MB/s` in red, `zram ▲` in amber, `TABS ⚠` in amber, `⚡ ON BATTERY` replacing the UPS metric. The `LLM` token shows the active model name (or a count when multiple models are loaded) and disappears when nothing is loaded. The browser tab favicon updates to a matching colored circle on every state change — readable as a pinned tab.

---

## Architecture

```text
topside/
├── core/
│   ├── collector.py          # BaseCollector ABC + Threshold dataclass
│   ├── notifier.py           # Edge-triggered threshold engine + notify-send / opswire dispatch
│   └── server.py             # FastAPI app, dynamic plugin loader, WebSocket hub, /reload
├── plugins/
│   ├── ram_monitor.py
│   ├── cpu_monitor.py
│   ├── gpu_monitor.py
│   ├── ups_monitor.py
│   ├── ollama_monitor.py     # Ollama REST API — loaded models, VRAM, keepalive state
│   └── headroom.py           # Meta-plugin: reads plugin cache, emits composite state
├── static/
│   └── index.html            # Self-contained dashboard (Chart.js via CDN)
├── config.yaml
├── requirements.txt
└── topside.service           # systemd user unit
```

### Plugin contract

Every plugin subclasses `BaseCollector` and implements:

```python
class BaseCollector(ABC):
    name: str       # matches the config.yaml plugins key
    interval: int   # poll interval in seconds

    async def collect(self) -> dict: ...
    def thresholds(self) -> list[Threshold]: ...
```

The server dynamically loads all files in `plugins/` at startup with no hardcoded imports. Adding a collector = drop a `.py` file in `plugins/`, add its key to `config.yaml`, restart. Nothing else changes.

`/reload` (HTTP GET or `SIGHUP`) re-reads `config.yaml` and restarts all plugin tasks — plugin code changes take effect without a full server restart.

---

## Notifications

Alerts are edge-triggered: each threshold fires once on crossing and resets only after the metric drops below the warn level (hysteresis). Dispatch targets:

- **Desktop** — `notify-send`
- **Ops pipeline** — shell call to `notifications.opswire_script` (e.g. `~/ops/infra_notify.sh`)

Headroom state transitions also trigger notifications:

| Transition | Severity |
| --- | --- |
| GO → EASE_IN | WARN |
| any → HOLD | CRITICAL |
| HOLD → GO | INFO (all clear) |
| Disk swap activated | CRITICAL (independent of demo state) |
| earlyoom browser threshold crossed | WARN |

---

## Configuration

All thresholds, drill costs, UPS settings, and plugin toggles live in `config.yaml` and are reloadable at runtime without dropping WebSocket connections:

```bash
curl http://localhost:7700/reload   # or: kill -HUP <pid>
```

```yaml
thresholds:
  ram:        { warn: 70,  critical: 85 }
  gpu_vram:   { warn: 75,  critical: 90 }
  cpu:        { warn: 80,  critical: 95 }
  swap_proximity_buffer_pct: 8

earlyoom:
  browser_warn_pct: 20      # % of total RAM at which a browser group is considered large
  ram_pressure_floor: 70    # earlyoom EASE_IN only fires when RAM also exceeds this %

drill_cost:
  ram_delta_pct:        8   # expected RAM increase when a drill fires
  cpu_spike_pct:       15   # expected CPU spike at drill start
  gpu_vram_delta_pct:   6   # expected VRAM increase
  headroom_ease_in_pct: 5   # EASE_IN when projected headroom drops below this %; HOLD at < 0 %

ups:
  device_name: "ups"
  load_warn: 70
  load_critical: 85
  battery_warn: 50
  battery_critical: 30

notifications:
  desktop: true
  opswire: true
  opswire_severity: WARN
  opswire_script: ~/ops/infra_notify.sh

ollama:
  base_url: "http://localhost:11434"

plugins:
  ram_monitor:    true
  cpu_monitor:    true
  gpu_monitor:    true
  ups_monitor:    true
  ollama_monitor: true
  headroom: true
```

---

## Requirements

- Python 3.12
- Ubuntu 24.04 (tested on ARMOURY: Ryzen 7 7800X3D, RTX 5070 Ti, 32 GB DDR5)
- `apcupsd` on the host with the UPS physically attached; `ups_monitor` connects to its NIS (TCP 3551) — configure `ups.nis_host` in `config.yaml` if the UPS is on a remote machine
- GPU monitoring requires an NVIDIA card; `ups_monitor` degrades gracefully if apcupsd NIS is unreachable; `ollama_monitor` degrades gracefully if Ollama is not running

```bash
pip install -r requirements.txt
```

---

## Running

```bash
uvicorn core.server:app --host 0.0.0.0 --port 7700
```

Dashboard: `http://localhost:7700`

### Autostart with systemd

```bash
systemctl --user enable --now topside
```

The unit file (`topside.service`) assumes the repo lives at `~/code/topside`.

---

## Constraints

- No sudo required to run
- GPU: `nvidia-ml-py` (pynvml API) only — no `nvidia-smi` subprocess calls
- UPS: apcupsd NIS protocol over stdlib `socket` — no `nut2`, no `upsc` subprocess calls
- Ollama: stdlib `urllib` only — no extra HTTP client dependency
- Swap: `/proc/swaps` parsed directly — no shell commands
- Frontend: one `.html` file, Chart.js from CDN — no npm, no build step
