"""Management helpers for the always-on display user service."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "darkflash-space-station.service"
API_SERVICE_NAME = "darkflash-space-station-api.service"


def unit_text(python: Path | None = None) -> str:
    """Build a user unit bound to the Python environment running this CLI."""
    # Do not resolve a virtualenv's Python symlink: resolving it loses the
    # environment's site-packages and its editable project installation.
    executable = python or Path(sys.executable)
    return f"""[Unit]
Description=darkFlash Space Station display service
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={executable} -m darkflash_space_station.cli telemetry --overlay --keep-awake
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""

def api_unit_text(python: Path | None = None) -> str:
    executable = python or Path(sys.executable)
    return f"""[Unit]
Description=darkFlash Space Station API service
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
EnvironmentFile=%h/.config/darkflash-space-station/api.env
ExecStart={executable} -m darkflash_space_station.cli api serve --port ${{PORT:-8790}} --bind ${{BIND:-127.0.0.1}} --token ${{TOKEN}} --gpu-bdf ${{GPU_BDF:-0000:04:00.0}}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *arguments],
            check=check,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("systemctl is not available on this system") from exc


def install_service() -> Path:
    """Install the managed unit without following an unexpected symlink."""
    directory = Path.home() / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination = directory / SERVICE_NAME
    if destination.is_symlink():
        raise ValueError(f"refusing to replace symlinked service unit: {destination}")
    destination.write_text(unit_text())
    destination.chmod(0o644)
    _systemctl("daemon-reload")
    return destination

def install_api_service(port: int = 8790, bind: str = "127.0.0.1", token: str | None = None, gpu_bdf: str | None = None) -> Path:
    config_dir = Path.home() / ".config" / "darkflash-space-station"
    config_dir.mkdir(parents=True, exist_ok=True)
    env_path = config_dir / "api.env"
    lines = [
        f"PORT={port}",
        f"BIND={bind}",
    ]
    if token:
        lines.append(f"TOKEN={token}")
    lines.append(f"GPU_BDF={gpu_bdf or '0000:04:00.0'}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    directory = Path.home() / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination = directory / API_SERVICE_NAME
    if destination.is_symlink():
        raise ValueError(f"refusing to replace symlinked service unit: {destination}")
    destination.write_text(api_unit_text())
    destination.chmod(0o644)
    _systemctl("daemon-reload")
    return destination


def start_service() -> None:
    _systemctl("enable", "--now", SERVICE_NAME)


def stop_service() -> None:
    _systemctl("stop", SERVICE_NAME)


def start_api_service() -> None:
    _systemctl("enable", "--now", API_SERVICE_NAME)


def stop_api_service() -> None:
    _systemctl("stop", API_SERVICE_NAME)


def service_active() -> bool:
    return _systemctl("is-active", "--quiet", SERVICE_NAME, check=False).returncode == 0


def api_service_active() -> bool:
    return _systemctl("is-active", "--quiet", API_SERVICE_NAME, check=False).returncode == 0


def service_status() -> str:
    result = _systemctl("--no-pager", "status", SERVICE_NAME, check=False)
    return (result.stdout or result.stderr).strip()


def api_service_status() -> str:
    result = _systemctl("--no-pager", "status", API_SERVICE_NAME, check=False)
    return (result.stdout or result.stderr).strip()
