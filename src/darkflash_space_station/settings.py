"""Persistent desktop-app preferences independent of editable layouts."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_GPU_BDF = "0000:04:00.0"


@dataclass
class AppSettings:
    """Values owned by the GUI rather than by a saved telemetry layout."""

    gpu_bdf: str = DEFAULT_GPU_BDF
    refresh_interval: float = 1.0
    sleep_timeout: int = 60
    selected_layout: str | None = None
    temperature_unit: str = "C"
    gif_mask_mode: str = "none"
    gif_mask_color: str = "#ffffff"
    snap_to_grid: bool = False


class SettingsStore:
    """Read and write GUI preferences, tolerating absent or malformed files."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config/darkflash-space-station/settings.json"

    def load(self) -> AppSettings:
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("settings must be a JSON object")
        except (OSError, UnicodeError, ValueError, TypeError):
            return AppSettings()

        defaults = AppSettings()
        gpu_bdf = data.get("gpu_bdf", defaults.gpu_bdf)
        interval = data.get("refresh_interval", defaults.refresh_interval)
        timeout = data.get("sleep_timeout", defaults.sleep_timeout)
        selected_layout = data.get("selected_layout", defaults.selected_layout)
        temperature_unit = data.get("temperature_unit", defaults.temperature_unit)
        gif_mask_mode = data.get("gif_mask_mode", defaults.gif_mask_mode)
        gif_mask_color = data.get("gif_mask_color", defaults.gif_mask_color)
        snap_to_grid = data.get("snap_to_grid", defaults.snap_to_grid)
        try:
            valid_interval = (
                isinstance(interval, (int, float))
                and not isinstance(interval, bool)
                and math.isfinite(float(interval))
                and 0.2 <= float(interval) <= 60
            )
        except OverflowError:
            valid_interval = False
        return AppSettings(
            gpu_bdf=gpu_bdf if isinstance(gpu_bdf, str) and gpu_bdf else defaults.gpu_bdf,
            refresh_interval=float(interval) if valid_interval else defaults.refresh_interval,
            sleep_timeout=(
                timeout
                if isinstance(timeout, int)
                and not isinstance(timeout, bool)
                and 0 <= timeout <= 3600
                else defaults.sleep_timeout
            ),
            selected_layout=(
                selected_layout
                if isinstance(selected_layout, str) and selected_layout
                else None
            ),
            temperature_unit=(
                temperature_unit
                if isinstance(temperature_unit, str)
                and temperature_unit in {"C", "F"}
                else defaults.temperature_unit
            ),
            gif_mask_mode=(
                gif_mask_mode
                if isinstance(gif_mask_mode, str)
                and gif_mask_mode in {"none", "manual", "openrgb"}
                else defaults.gif_mask_mode
            ),
            gif_mask_color=(
                gif_mask_color
                if isinstance(gif_mask_color, str)
                and len(gif_mask_color) == 7
                and gif_mask_color.startswith("#")
                else defaults.gif_mask_color
            ),
            snap_to_grid=snap_to_grid if isinstance(snap_to_grid, bool) else defaults.snap_to_grid,
        )

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
