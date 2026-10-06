"""CLI module execution tests."""

from __future__ import annotations

import subprocess
import sys


def test_cli_module_runs_main() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "darkflash_space_station.cli", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Native controller for the darkFlash Space Station display." in result.stdout


def test_api_commands_are_documented() -> None:
    group = subprocess.run(
        [sys.executable, "-m", "darkflash_space_station.cli", "api", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Home Assistant integration HTTP API" in group.stdout
    for command in ("serve", "install", "start", "stop", "status"):
        assert command in group.stdout

    serve = subprocess.run(
        [sys.executable, "-m", "darkflash_space_station.cli", "api", "serve", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--token" in serve.stdout
    assert "--port" in serve.stdout
    assert "--bind" in serve.stdout
