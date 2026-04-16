#!/usr/bin/env python3
"""TOPSIDE installer — stdlib only, no venv required to run.

Usage:
    python3 install.py [--prefix PATH] [--port PORT] [--start] [--update] [--dry-run]
    python3 install.py --uninstall [--prefix PATH] [--dry-run]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.resolve()
DEFAULT_PREFIX = Path.home() / ".local" / "share" / "topside"
SERVICE_DIR  = Path.home() / ".config" / "systemd" / "user"
SERVICE_NAME = "topside"

# Directories and files synced from the repo into the install prefix.
# Everything else (mandates/, install.py, config.yaml.example, .git/) stays out.
_SYNC_DIRS  = ["core", "plugins", "static"]
_SYNC_FILES = ["requirements.txt"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"  {msg}")


def _run(cmd: list, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess:
    display = " ".join(str(c) for c in cmd)
    if dry_run:
        _log(f"[dry-run] {display}")
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.run(cmd, check=check, capture_output=False, text=True)


def _service_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() == "active"


def _generate_service(prefix: Path, port: int) -> str:
    uvicorn = prefix / "venv" / "bin" / "uvicorn"
    return (
        "[Unit]\n"
        "Description=TOPSIDE system monitor\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={prefix}\n"
        f"ExecStart={uvicorn} core.server:app --host 0.0.0.0 --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def do_install(args: argparse.Namespace) -> None:
    prefix  = Path(args.prefix).expanduser().resolve()
    port    = args.port
    dry_run = args.dry_run

    print(f"\nTOPSIDE installer")
    print(f"  prefix : {prefix}")
    print(f"  port   : {port}")
    print(f"  mode   : {'dry-run' if dry_run else 'install'}\n")

    # 1. Validate Python version
    if sys.version_info < (3, 12):
        print(f"ERROR: Python 3.12+ required (found "
              f"{sys.version_info.major}.{sys.version_info.minor})")
        sys.exit(1)
    _log("Python version OK")

    # 2-3. Create prefix directory
    if not dry_run:
        prefix.mkdir(parents=True, exist_ok=True)
    _log(f"Prefix ready: {prefix}")

    # 4. Sync source files — always overwrite so updates take effect
    for name in _SYNC_DIRS:
        src = REPO_ROOT / name
        dst = prefix / name
        if dry_run:
            _log(f"[dry-run] sync {src} → {dst}")
        else:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            _log(f"Synced {name}/")

    for name in _SYNC_FILES:
        src = REPO_ROOT / name
        dst = prefix / name
        if dry_run:
            _log(f"[dry-run] copy {name} → {dst}")
        else:
            shutil.copy2(src, dst)
            _log(f"Copied {name}")

    # 5. Create / update virtual environment
    venv = prefix / "venv"
    _run([sys.executable, "-m", "venv", str(venv)], dry_run)
    _log("Virtual environment ready")

    # 6. Install dependencies into the venv
    pip = venv / "bin" / "pip"
    req = prefix / "requirements.txt"
    _run([str(pip), "install", "--quiet", "--upgrade", "-r", str(req)], dry_run)
    _log("Dependencies installed")

    # 7. Seed config — first install only; never overwrite on update
    config_dst = prefix / "config.yaml"
    config_src = REPO_ROOT / "config.yaml.example"
    if config_dst.exists():
        _log("config.yaml preserved (existing file untouched)")
        config_is_new = False
    elif dry_run:
        _log(f"[dry-run] copy config.yaml.example → {config_dst}")
        config_is_new = True
    else:
        shutil.copy2(config_src, config_dst)
        _log("config.yaml seeded from config.yaml.example")
        config_is_new = True

    # 8. Generate and write systemd service file
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    service_path = SERVICE_DIR / f"{SERVICE_NAME}.service"
    service_content = _generate_service(prefix, port)
    if dry_run:
        _log(f"[dry-run] write {service_path}")
    else:
        service_path.write_text(service_content)
        _log(f"Service file: {service_path}")

    # 9. Reload systemd so the new/updated unit is visible
    _run(["systemctl", "--user", "daemon-reload"], dry_run, check=False)
    _log("systemd reloaded")

    # 10. Start / restart the service
    was_active = not dry_run and _service_is_active()
    if args.start:
        if was_active:
            _run(["systemctl", "--user", "restart", SERVICE_NAME], dry_run, check=False)
            _log("Service restarted")
        else:
            _run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], dry_run, check=False)
            _log("Service enabled and started")
    elif was_active:
        # Update path: running service gets restarted automatically
        _run(["systemctl", "--user", "restart", SERVICE_NAME], dry_run, check=False)
        _log("Running service restarted with updated code")

    # 11. Summary
    print(f"\nDone.")
    print(f"  Dashboard : http://localhost:{port}")
    print(f"  Config    : {prefix / 'config.yaml'}")
    print(f"  Logs      : journalctl --user -u {SERVICE_NAME} -f\n")

    if config_is_new:
        print("  Before starting, review config.yaml for this machine:")
        print(f"    ups.nis_host    — host running apcupsd (default: localhost)")
        print(f"    ollama.base_url — Ollama endpoint if not on localhost")
        print(f"    plugins.*       — disable collectors you don't have hardware for")
        print()

    if not args.start and not was_active:
        print(f"  To start: systemctl --user enable --now {SERVICE_NAME}\n")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def do_uninstall(args: argparse.Namespace) -> None:
    prefix  = Path(args.prefix).expanduser().resolve()
    dry_run = args.dry_run
    service_path = SERVICE_DIR / f"{SERVICE_NAME}.service"

    print(f"\nTOPSIDE uninstaller")
    print(f"  prefix : {prefix}\n")

    # Stop and disable the service (ignore errors if it was never enabled)
    _run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], dry_run, check=False)
    _log("Service stopped and disabled")

    # Remove the service unit file
    if dry_run:
        _log(f"[dry-run] remove {service_path}")
    elif service_path.exists():
        service_path.unlink()
        _log(f"Removed {service_path}")
    else:
        _log("Service file not found (already removed?)")

    # Reload systemd
    _run(["systemctl", "--user", "daemon-reload"], dry_run, check=False)
    _log("systemd reloaded")

    # Remove the install prefix — ask first
    if dry_run:
        _log(f"[dry-run] remove {prefix}")
    elif prefix.exists():
        answer = input(f"\n  Remove {prefix}? [y/N] ").strip().lower()
        if answer == "y":
            shutil.rmtree(prefix)
            _log(f"Removed {prefix}")
        else:
            _log("Prefix kept")
    else:
        _log(f"Prefix not found: {prefix}")

    print("\nDone.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TOPSIDE installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 install.py                          install to default prefix\n"
            "  python3 install.py --start                  install and start service\n"
            "  python3 install.py --prefix /opt/topside    custom prefix\n"
            "  python3 install.py --port 8080              custom port\n"
            "  python3 install.py --update                 update existing install\n"
            "  python3 install.py --dry-run                preview without changes\n"
            "  python3 install.py --uninstall              remove everything\n"
        ),
    )
    p.add_argument(
        "--prefix", default=str(DEFAULT_PREFIX), metavar="PATH",
        help=f"install prefix (default: {DEFAULT_PREFIX})",
    )
    p.add_argument(
        "--port", type=int, default=7700,
        help="HTTP/WebSocket port (default: 7700)",
    )
    p.add_argument(
        "--start", action="store_true",
        help="enable and start the systemd service after install",
    )
    p.add_argument(
        "--update", action="store_true",
        help="update an existing install — config.yaml is always preserved",
    )
    p.add_argument(
        "--uninstall", action="store_true",
        help="stop service, remove unit file, and remove install prefix",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print every action without executing any of them",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.uninstall:
        do_uninstall(args)
    else:
        do_install(args)
