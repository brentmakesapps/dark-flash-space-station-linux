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
