"""Persisted telemetry-overlay layout definitions."""
from __future__ import annotations
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

WIDGETS = ("cpu-temperature", "cpu-load", "gpu-temperature", "gpu-load", "network-upload", "network-download", "time", "date", "custom-text")
GRAPH_METRICS = WIDGETS[:6]
GRAPH_STYLES = (
    "bar",
    "line",
    "circular-line",
    "semicircle-gauge",
    "ring-gauge",
    "pie",
)
CIRCULAR_GRAPH_STYLES = frozenset(
    {"circular-line", "semicircle-gauge", "ring-gauge", "pie"}
)
DISPLAY_SIZE = 320
MIN_FONT_SIZE = 8
MAX_FONT_SIZE = 64
MIN_GRAPH_SIZE = 20

@dataclass
class Widget:
    metric: str
    x: int = 16
    y: int = 16
    font: str = "Liberation Sans"
    size: int = 24
    color: str = "#ffffff"
    text: str = "Label"
    graph_style: str | None = None
    width: int = 120
    height: int = 60
    history_seconds: int = 60
    gpu_bdf: str = ""
    temperature_unit: str = ""
    color_source: str = "manual"
    color_hue_shift: float = 0
    color_brightness_percent: float = 100

    def __post_init__(self) -> None:
        if self.color_source not in {"manual", "openrgb"}:
            self.color_source = "manual"
        if (
            not isinstance(self.color_hue_shift, (int, float))
            or isinstance(self.color_hue_shift, bool)
            or not math.isfinite(self.color_hue_shift)
        ):
            self.color_hue_shift = 0
        self.color_hue_shift = min(180, max(-180, self.color_hue_shift))
        if (
            not isinstance(self.color_brightness_percent, (int, float))
            or isinstance(self.color_brightness_percent, bool)
            or not math.isfinite(self.color_brightness_percent)
        ):
            self.color_brightness_percent = 100
        self.color_brightness_percent = min(
            200, max(25, self.color_brightness_percent)
        )
        if self.temperature_unit not in {"", "C", "F"}:
            self.temperature_unit = ""
        if self.is_circular_graph:
            side = max(MIN_GRAPH_SIZE, self.width, self.height)
            self.width = side
            self.height = side

    @property
    def is_graph(self) -> bool:
        return self.graph_style in GRAPH_STYLES and self.metric in GRAPH_METRICS

    @property
    def is_circular_graph(self) -> bool:
        return self.is_graph and self.graph_style in CIRCULAR_GRAPH_STYLES

@dataclass
class Layout:
    name: str
    widgets: list[Widget] = field(default_factory=list)
    media: LayoutMedia = field(default_factory=lambda: LayoutMedia())


@dataclass
class LayoutMedia:
    kind: str = "inherit"
    file: str = ""
    gif_mask_mode: str = "none"
    gif_mask_color: str = "#ffffff"
    gif_speed_source: str = "none"
    gif_speed_gpu_bdf: str = ""
    gif_hue_shift: float = 0
    gif_brightness_percent: float = 100

    def __post_init__(self) -> None:
        if self.kind not in {"inherit", "none", "image", "gif"}:
            self.kind = "none"
        if self.gif_mask_mode not in {"none", "manual", "openrgb"}:
            self.gif_mask_mode = "none"
        if self.gif_speed_source not in {"none", "cpu", "gpu", "max"}:
            self.gif_speed_source = "none"


class LayoutDraft:
    """In-memory layout edits that are persisted only when explicitly saved."""

    def __init__(self, store: LayoutStore) -> None:
        self.store = store
        self.layouts = store.load()
        self.active_name = store.active_name(self.layouts)
        self.dirty = False

    @property
    def active_layout(self) -> Layout:
        return self.layouts[self.active_name]

    def select(self, name: str, *, mark_dirty: bool = True) -> None:
        if name not in self.layouts:
            raise ValueError(f"unknown layout {name!r}")
        self.active_name = name
        if mark_dirty:
            self.dirty = True

    def save(self) -> None:
        self.store.save(self.layouts)
        self.store.save_active(self.active_name, self.layouts)
        self.dirty = False

    def reset(self) -> None:
        """Discard edits and restore layouts from their last saved state."""
        selected_name = self.active_name
        self.layouts = self.store.load()
        self.active_name = (
            selected_name
            if selected_name in self.layouts
            else self.store.active_name(self.layouts)
        )
        self.dirty = False


def resize_values(
    widget: Widget,
    start_size: int,
    start_width: int,
    start_height: int,
    offset_x: float,
    offset_y: float,
) -> tuple[int, int, int]:
    """Return bounded dimensions after a lower-right resize drag."""
    if widget.is_graph:
        maximum_width = max(MIN_GRAPH_SIZE, DISPLAY_SIZE - widget.x)
        maximum_height = max(MIN_GRAPH_SIZE, DISPLAY_SIZE - widget.y)
        if widget.is_circular_graph:
            delta = max(offset_x, offset_y)
            if delta < 0:
                delta = min(offset_x, offset_y)
            maximum_side = min(maximum_width, maximum_height)
            side = max(
                MIN_GRAPH_SIZE,
                min(maximum_side, round(max(start_width, start_height) + delta)),
            )
            return widget.size, side, side
        return (
            widget.size,
            max(MIN_GRAPH_SIZE, min(maximum_width, round(start_width + offset_x))),
            max(MIN_GRAPH_SIZE, min(maximum_height, round(start_height + offset_y))),
        )
    size_delta = round((offset_x + offset_y) / 2)
    return (
        max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, start_size + size_delta)),
        widget.width,
        widget.height,
    )


def widget_selector_state(
    widgets: list[Widget], selected: int
) -> tuple[list[str], int]:
    """Build dropdown labels and retain a valid selection after structural edits."""
    labels = []
    for widget in widgets:
        label = widget.metric.replace("-", " ").title()
        if widget.is_graph:
            label += f" graph ({widget.graph_style}, {widget.history_seconds}s)"
        labels.append(label)
    if not labels:
        return labels, -1
    return labels, max(0, min(selected, len(labels) - 1))


def snap_to_grid(value: float, spacing: int = 40) -> int:
    """Round a canvas coordinate or dimension to its nearest grid line."""
    if spacing <= 0:
        raise ValueError("grid spacing must be greater than zero")
    return math.floor(value / spacing + 0.5) * spacing


def default_layout() -> Layout:
    return Layout("Default", [Widget("cpu-temperature", 16, 16), Widget("cpu-load", 16, 50), Widget("gpu-temperature", 16, 110), Widget("gpu-load", 16, 144)])

class LayoutStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config/darkflash-space-station/layouts.json"
        self.active_path = self.path.with_name("active-layout")

    def load(self) -> dict[str, Layout]:
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("layouts must be a JSON object")
            layouts = {}
            for name, saved_layout in data.items():
                if not isinstance(name, str):
                    continue
                widgets = (
                    saved_layout
                    if isinstance(saved_layout, list)
                    else saved_layout.get("widgets", [])
                    if isinstance(saved_layout, dict)
                    else []
                )
                media_data = (
                    saved_layout.get("media", {})
                    if isinstance(saved_layout, dict)
                    else {}
                )
                layouts[name] = Layout(
                    name,
                    [
                        Widget(
                            **{
                                key: value
                                for key, value in item.items()
                                if key in Widget.__dataclass_fields__
                            }
                        )
                        for item in widgets
                        if isinstance(item, dict)
                    ],
                    LayoutMedia(
                        **{
                            key: value
                            for key, value in media_data.items()
                            if key in LayoutMedia.__dataclass_fields__
                        }
                    )
                    if isinstance(media_data, dict)
                    else LayoutMedia(),
                )
            if layouts:
                return layouts
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        layout = default_layout()
        return {layout.name: layout}

    def save(self, layouts: dict[str, Layout]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    name: {
                        "widgets": [asdict(widget) for widget in layout.widgets],
                        "media": asdict(layout.media),
                    }
                    for name, layout in layouts.items()
                },
                indent=2,
            )
            + "\n"
        )

    def active_name(self, layouts: dict[str, Layout] | None = None) -> str:
        """Return the persisted active layout, falling back compatibly."""
        layouts = layouts or self.load()
        try:
            name = self.active_path.read_text().strip()
        except OSError:
            name = ""
        if name in layouts:
            return name
        return "Default" if "Default" in layouts else next(iter(layouts))

    def active_layout(self, name: str | None = None) -> Layout:
        layouts = self.load()
        selected = name or self.active_name(layouts)
        try:
            return layouts[selected]
        except KeyError as exc:
            choices = ", ".join(layouts)
            raise ValueError(f"unknown layout {selected!r}; available layouts: {choices}") from exc

    def save_active(self, name: str, layouts: dict[str, Layout] | None = None) -> None:
        layouts = layouts or self.load()
        if name not in layouts:
            raise ValueError(f"unknown layout {name!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.active_path.write_text(f"{name}\n")
