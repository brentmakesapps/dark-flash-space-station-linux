"""Persistent pre-rendered animation variants for load-adaptive playback."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .media import render_animation
from .openrgb import gif_mask_color
from .settings import AppSettings, SettingsStore

SPEED_MULTIPLIERS = tuple(1 + 2 * index / 9 for index in range(10))
CACHE_VERSION = 3


def speed_bucket(load: float) -> int:
    """Map 0-100 percent load to ten buckets, with 0-10 percent in bucket zero."""
    if not math.isfinite(load):
        raise ValueError("animation load must be finite")
    return min(9, max(0, math.ceil(min(100, max(0, load)) / 10) - 1))


class StableSpeedSelector:
    """Emit a new speed bucket only after a requested change remains stable."""

    def __init__(self, stability_seconds: float = 5) -> None:
        if stability_seconds <= 0:
            raise ValueError("animation stability duration must be greater than zero")
        self.stability_seconds = stability_seconds
        self.current: int | None = None
        self.pending: int | None = None
        self.pending_since = 0.0

    def update(self, load: float, now: float) -> int | None:
        requested = speed_bucket(load)
        if self.current is None:
            self.current = requested
            return requested
        if requested == self.current:
            self.pending = None
            return None
        if requested != self.pending:
            self.pending = requested
            self.pending_since = now
            return None
        if now - self.pending_since < self.stability_seconds:
            return None
        self.current = requested
        self.pending = None
        return requested


class AnimationCache:
    """Build and retrieve the ten persisted playback-speed variants."""

    def __init__(self, store: SettingsStore, directory: Path | None = None) -> None:
        self.store = store
        self.directory = directory or store.saved_gif_path.parent / "speeds"
        self.manifest_path = self.directory / "manifest.json"

    def rebuild(self, source: Path, mask_color: str | None) -> None:
        variants = [
            render_animation(source, mask_color, speed=speed)
            for speed in SPEED_MULTIPLIERS
        ]
        self.directory.mkdir(parents=True, exist_ok=True)
        for index, data in enumerate(variants):
            temporary = self.variant_path(index).with_suffix(".mp4.tmp")
            temporary.write_bytes(data)
            temporary.replace(self.variant_path(index))
        temporary_manifest = self.manifest_path.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(
                {"version": CACHE_VERSION, "mask_color": mask_color}, indent=2
            )
            + "\n"
        )
        temporary_manifest.replace(self.manifest_path)

    def ensure(self, source: Path, mask_color: str | None) -> None:
        if not self.matches(mask_color) or any(
            not self.variant_path(index).is_file() for index in range(10)
        ):
            self.rebuild(source, mask_color)

    def variant_path(self, bucket: int) -> Path:
        if not 0 <= bucket < 10:
            raise ValueError("animation speed bucket must be between 0 and 9")
        return self.directory / f"speed-{bucket}.mp4"

    def variant(self, bucket: int) -> bytes:
        return self.variant_path(bucket).read_bytes()

    def mask_color(self) -> str | None:
        try:
            data = json.loads(self.manifest_path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("mask_color")
        return value if isinstance(value, str) else None

    def matches(self, mask_color: str | None) -> bool:
        try:
            data = json.loads(self.manifest_path.read_text())
        except (OSError, ValueError, TypeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("version") == CACHE_VERSION
            and "mask_color" in data
            and data["mask_color"] == mask_color
        )


class AdaptiveAnimation:
    """Keep cached variants synchronized with the saved OpenRGB mask."""

    def __init__(
        self,
        source: Path,
        settings: AppSettings,
        cache: AnimationCache,
        *,
        reload_settings: bool = True,
    ) -> None:
        self.source = source
        self.settings = settings
        self.cache = cache
        self.reload_settings = reload_settings
        self.color = gif_mask_color(settings)
        self.cache.ensure(source, self.color)

    def variant(self, bucket: int) -> bytes:
        return self.cache.variant(bucket)

    def refresh_color(self) -> bool:
        if self.reload_settings:
            self.settings = self.cache.store.load()
        color = gif_mask_color(self.settings)
        if color == self.color:
            return False
        self.cache.rebuild(self.source, color)
        self.color = color
        return True
