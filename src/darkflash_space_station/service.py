"""Management helpers for the always-on display user service."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "darkflash-space-station.service"


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


def _systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=check,
        text=True,
        capture_output=True,
    )


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


def start_service() -> None:
    _systemctl("enable", "--now", SERVICE_NAME)


def stop_service() -> None:
    _systemctl("stop", SERVICE_NAME)


def service_active() -> bool:
    return _systemctl("is-active", "--quiet", SERVICE_NAME, check=False).returncode == 0


def service_status() -> str:
    result = _systemctl("--no-pager", "status", SERVICE_NAME, check=False)
    return (result.stdout or result.stderr).strip()
