import asyncio
import importlib.util
import json
import logging
import platform
import signal
import socket
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core.collector import BaseCollector
from core.notifier import Notifier

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
PLUGINS_DIR = Path(__file__).parent.parent / "plugins"
STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(title="TOPSIDE")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_config: dict = {}
_plugins: dict[str, BaseCollector] = {}
_notifier: Notifier | None = None
_cache: dict[str, dict] = {}           # plugin_name -> latest payload
_tasks: dict[str, asyncio.Task] = {}   # plugin_name -> running asyncio task


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws) if hasattr(self._active, "discard") else None
        try:
            self._active.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, data: dict) -> None:
        dead: list[WebSocket] = []
        payload = json.dumps(data)
        for ws in list(self._active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_cache(self, ws: WebSocket) -> None:
        for name, payload in _cache.items():
            try:
                await ws.send_text(json.dumps({"plugin": name, "data": payload}))
            except Exception:
                break


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Dynamic plugin loader
# ---------------------------------------------------------------------------

def load_plugins(config: dict) -> dict[str, BaseCollector]:
    plugins: dict[str, BaseCollector] = {}
    for path in sorted(PLUGINS_DIR.glob("*.py")):
        module_name = path.stem
        if module_name.startswith("_"):
            continue
        if not config.get("plugins", {}).get(module_name, False):
            continue
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for obj in vars(module).values():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseCollector)
                    and obj is not BaseCollector
                ):
                    # headroom needs the cache and config injected
                    if module_name == "headroom":
                        instance = obj(_cache, config)
                    else:
                        instance = obj(config)
                    plugins[module_name] = instance
                    log.info("Loaded plugin: %s (interval=%ss)", module_name, instance.interval)
                    break
        except Exception as exc:
            log.error("Failed to load plugin %s: %s", module_name, exc)
    return plugins


# ---------------------------------------------------------------------------
# Collection loop
# ---------------------------------------------------------------------------

async def _collection_loop(name: str, plugin: BaseCollector) -> None:
    global _notifier
    while True:
        try:
            payload = await plugin.collect()
            _cache[name] = payload
            if _notifier is not None:
                try:
                    _notifier.evaluate(name, payload, plugin.thresholds())
                    # Special-case disk swap and demo_readiness notifications
                    if name == "ram_monitor":
                        disk = payload.get("swap", {}).get("disk", {})
                        if disk.get("velocity_mbps", 0) > 0 and disk.get("pct", 0) > 0:
                            _notifier.notify_disk_swap_activated()
                        else:
                            _notifier.notify_disk_swap_cleared()
                    if name == "headroom":
                        _notifier.notify_demo_state(payload.get("state", "GO"))
                except Exception as exc:
                    log.warning("Notifier error for %s: %s", name, exc)
            await manager.broadcast({"plugin": name, "data": payload})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Collection error in %s: %s", name, exc)
        await asyncio.sleep(plugin.interval)


# ---------------------------------------------------------------------------
# Reload logic
# ---------------------------------------------------------------------------

async def _do_reload() -> dict:
    global _config, _plugins, _notifier

    new_config = load_config()
    _config = new_config

    if _notifier is not None:
        _notifier.reload(new_config)
    else:
        _notifier = Notifier(new_config)

    new_plugins = load_plugins(new_config)

    # Cancel all running tasks — reload replaces every instance so config and
    # code changes take effect immediately in all plugins.
    for name, task in list(_tasks.items()):
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        del _tasks[name]
        _plugins.pop(name, None)

    # Start tasks for all enabled plugins
    for name, plugin in new_plugins.items():
        _plugins[name] = plugin
        task = asyncio.create_task(_collection_loop(name, plugin), name=name)
        _tasks[name] = task
        log.info("Reloaded plugin: %s", name)

    return {"status": "ok", "plugins": list(_tasks.keys())}


# ---------------------------------------------------------------------------
# FastAPI lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    global _config, _notifier

    _config = load_config()
    _notifier = Notifier(_config)

    plugins = load_plugins(_config)
    for name, plugin in plugins.items():
        _plugins[name] = plugin
        task = asyncio.create_task(_collection_loop(name, plugin), name=name)
        _tasks[name] = task

    # SIGHUP -> reload
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(_do_reload()))
    except (NotImplementedError, OSError):
        pass  # Windows / environments that don't support SIGHUP

    log.info("TOPSIDE started. Active plugins: %s", list(_tasks.keys()))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/info")
async def system_info() -> JSONResponse:
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except Exception:
        os_name = platform.system()
    return JSONResponse({
        "hostname": socket.gethostname(),
        "os":       os_name,
        "kernel":   platform.release(),
        "arch":     platform.machine(),
    })


@app.get("/reload")
async def reload_config() -> JSONResponse:
    result = await _do_reload()
    return JSONResponse(result)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    await manager.send_cache(ws)
    try:
        while True:
            # Keep connection alive; client messages are not expected but handled gracefully
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Static files — mount last so /ws and /reload take precedence
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
