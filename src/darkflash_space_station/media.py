"""Direct PNG media uploads for the darkFlash Space Station display."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import tempfile
import time
from datetime import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .hid import Device, HidError
from .layout import Layout, default_layout
from .openrgb import adjust_mask_color
from .telemetry import TelemetryHistory
from .protocol import (
    MEDIA_BLOCK_SIZE,
    connect_message,
    media_block,
    request_message,
)

DISPLAY_CONTENT_INSET = 24
GIF_PREVIEW_CACHE_LIMIT = 32
IMAGE_MAGICK_FONTS = {
    "Liberation Sans": "Liberation-Sans",
    "DejaVu Sans": "DejaVu-Sans",
    "Monospace": "Adwaita-Mono",
    "Serif": "DejaVu-Serif",
    "CubicCoreMono": "CubicCoreMono-Regular",
}


@lru_cache
def image_magick_font(family: str) -> str:
    """Return an ImageMagick font identifier for a Fontconfig family."""
    if identifier := IMAGE_MAGICK_FONTS.get(family):
        return identifier
    try:
        result = subprocess.run(
            ["fc-list", "--format=%{family}\t%{file}\n"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return family.replace(" ", "-")
    for entry in result.stdout.splitlines():
        candidates, separator, path = entry.partition("\t")
        if not separator:
            continue
        if any(
            candidate.strip().replace(r"\-", "-") == family
            for candidate in candidates.split(",")
        ):
            return path
    return family.replace(" ", "-")


@dataclass
class MediaSession:
    """Allocate consecutive control-message sequence numbers for one session."""

    sequence: int = 0

    def next_sequence(self) -> int:
        current = self.sequence
        self.sequence = (self.sequence + 1) & 0xFFFF
        return current


def render_png(source: Path) -> bytes:
    """Crop and scale an image to the cooler's 320x320 PNG canvas."""
    with tempfile.NamedTemporaryFile(suffix=".png") as output:
        try:
            subprocess.run(
                [
                    "convert",
                    str(source),
                    "-resize",
                    "320x320^",
                    "-gravity",
                    "center",
                    "-extent",
                    "320x320",
                    "PNG24:" + output.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HidError(f"unable to render {source} as a 320x320 PNG: {exc}") from exc
        return Path(output.name).read_bytes()


def render_transparent_overlay() -> bytes:
    """Create the transparent OSD layer used over video backgrounds."""
    with tempfile.NamedTemporaryFile(suffix=".png") as output:
        try:
            subprocess.run(
                [
                    "convert",
                    "-size",
                    "320x320",
                    "xc:none",
                    "PNG32:" + output.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HidError(f"unable to render transparent OSD layer: {exc}") from exc
        return Path(output.name).read_bytes()


def format_temperature(temperature: object, temperature_unit: str = "C") -> str:
    """Format a Celsius sensor reading for the selected display unit."""
    if temperature_unit not in {"C", "F"}:
        raise ValueError("temperature unit must be C or F")
    try:
        celsius = float(temperature)
    except (TypeError, ValueError) as exc:
        raise ValueError("temperature must be numeric") from exc
    value = celsius if temperature_unit == "C" else celsius * 9 / 5 + 32
    return f"{round(value)}°{temperature_unit}"


def _graph_values(
    widget: object, telemetry: Mapping[str, object], history: TelemetryHistory | None
) -> list[float]:
    metric = widget.metric
    seconds = max(1, int(widget.history_seconds))
    history_metric = (
        f"{metric}@{widget.gpu_bdf}"
        if metric.startswith("gpu-") and widget.gpu_bdf
        else metric
    )
    values = history.values(history_metric, seconds) if history is not None else []
    if values:
        return _temperature_graph_values(widget, values)
    section, field = {
        "cpu-temperature": ("cpu", "temperature"),
        "cpu-load": ("cpu", "load"),
        "gpu-temperature": ("gpu", "temperature"),
        "gpu-load": ("gpu", "load"),
        "network-upload": ("network", "upload"),
        "network-download": ("network", "download"),
    }[metric]
    source = (
        _widget_gpu_telemetry(widget, telemetry)
        if section == "gpu"
        else telemetry.get(section)
    )
    value = source.get(field, 0) if isinstance(source, Mapping) else 0
    values = [float(value)] if isinstance(value, (int, float)) else [0]
    return _temperature_graph_values(widget, values)


def _widget_gpu_telemetry(
    widget: object, telemetry: Mapping[str, object]
) -> object:
    gpus = telemetry.get("gpus")
    gpu_bdf = getattr(widget, "gpu_bdf", "")
    if gpu_bdf and isinstance(gpus, Mapping):
        selected = gpus.get(gpu_bdf)
        if isinstance(selected, Mapping):
            return selected
    return telemetry.get("gpu", {})


def _temperature_graph_values(widget: object, values: list[float]) -> list[float]:
    if (
        widget.metric.endswith("temperature")
        and getattr(widget, "temperature_unit", "") == "F"
    ):
        return [value * 9 / 5 + 32 for value in values]
    return values


def _graph_scale(widget: object, values: list[float]) -> float:
    if widget.metric.endswith("temperature"):
        return 212 if getattr(widget, "temperature_unit", "") == "F" else 100
    if widget.metric.endswith("load"):
        return 100
    return max(max(values, default=0), 1)


def _graph_draw(widget: object, values: list[float]) -> list[str]:
    """Return ImageMagick drawing arguments for one graph widget."""
    x, y = (
        int(widget.x) + DISPLAY_CONTENT_INSET,
        int(widget.y) + DISPLAY_CONTENT_INSET,
    )
    width, height = max(20, int(widget.width)), max(20, int(widget.height))
    color, style = widget.color, widget.graph_style
    scale = _graph_scale(widget, values)
    normalized = [max(0, min(1, value / scale)) for value in values]
    if style == "bar":
        bar_width = max(1, width / max(len(normalized), 1))
        commands = []
        for index, value in enumerate(normalized):
            left = round(x + index * bar_width)
            right = max(left + 1, round(x + (index + 1) * bar_width) - 1)
            top = round(y + height * (1 - value))
            commands.append(f"rectangle {left},{top} {right},{y + height}")
        return ["-fill", color, "-stroke", "none", "-draw", " ".join(commands)]
    if style == "line":
        if len(normalized) == 1:
            normalized *= 2
        points = [
            f"{round(x + width * index / (len(normalized) - 1))},{round(y + height * (1 - value))}"
            for index, value in enumerate(normalized)
        ]
        return ["-fill", "none", "-stroke", color, "-strokewidth", "2", "-draw", f"polyline {' '.join(points)}"]
    if style == "circular-line":
        import math

        radius_x = max(8, width / 2 - 2)
        radius_y = max(8, height / 2 - 2)
        center_x, center_y = x + width // 2, y + height // 2
        if len(normalized) == 1:
            normalized *= 2
        points = []
        for index, value in enumerate(normalized):
            angle = -math.pi / 2 + 2 * math.pi * index / (len(normalized) - 1)
            radial = 0.25 + 0.75 * value
            points.append(
                f"{round(center_x + math.cos(angle) * radius_x * radial)},"
                f"{round(center_y + math.sin(angle) * radius_y * radial)}"
            )
        return ["-fill", "none", "-stroke", color, "-strokewidth", "2", "-draw", f"polyline {' '.join(points)}"]
    if style == "ring-gauge":
        inset = 3
        left, top = x + inset, y + inset
        right, bottom = x + width - inset, y + height - inset
        ring_color = f"{color}80" if color.startswith("#") and len(color) == 7 else color
        return [
            "-fill",
            "none",
            "-stroke",
            ring_color,
            "-strokewidth",
            "4",
            "-draw",
            f"arc {left},{top} {right},{bottom} -90,270",
        ]
    if style == "semicircle-gauge":
        ratio = normalized[-1] if normalized else 0
        inset = 3
        left, top = x + inset, y + inset
        right, bottom = x + width - inset, y + height - inset
        start, end = 180, 360
        progress_end = start + (end - start) * ratio
        return [
            "-fill",
            "none",
            "-stroke",
            "#ffffff44",
            "-strokewidth",
            "4",
            "-draw",
            f"arc {left},{top} {right},{bottom} {start},{end}",
            "-stroke",
            color,
            "-draw",
            f"arc {left},{top} {right},{bottom} {start},{progress_end}",
        ]
    if style == "pie":
        import math

        center_x, center_y = x + width // 2, y + height // 2
        radius = max(8, min(width, height) // 2)
        fraction = sum(normalized) / len(normalized)
        steps = max(2, round(32 * fraction))
        points = [f"{center_x},{center_y}"]
        for index in range(steps + 1):
            angle = -math.pi / 2 + 2 * math.pi * fraction * index / steps
            points.append(
                f"{round(center_x + math.cos(angle) * radius)},{round(center_y + math.sin(angle) * radius)}"
            )
        return [
            "-fill", "#ffffff33", "-stroke", "none", "-draw", f"circle {center_x},{center_y} {center_x},{center_y - radius}",
            "-fill", color, "-draw", f"polygon {' '.join(points)}",
        ]
    return []


def render_telemetry_overlay(
    telemetry: Mapping[str, object],
    layout: Layout | None = None,
    history: TelemetryHistory | None = None,
    temperature_unit: str = "C",
    openrgb_color: str | None = None,
) -> bytes:
    """Render text and rolling-history graph widgets into a transparent foreground OSD."""
    cpu = telemetry.get("cpu", {})
    gpu = telemetry.get("gpu", {})
    network = telemetry.get("network", {})
    if not isinstance(cpu, Mapping) or not isinstance(gpu, Mapping) or not isinstance(network, Mapping):
        raise ValueError("telemetry payload does not contain CPU and GPU data")
    now = datetime.now()
    layout = layout or default_layout()
    arguments = ["convert", "-size", "320x320", "xc:none"]
    for widget in layout.widgets:
        if widget.is_graph:
            arguments.extend(_graph_draw(widget, _graph_values(widget, telemetry, history)))
        else:
            color = widget.color
            if getattr(widget, "color_source", "manual") == "openrgb":
                if openrgb_color is None:
                    raise ValueError(
                        "OpenRGB text color was requested but no color was provided"
                    )
                color = adjust_mask_color(
                    openrgb_color,
                    widget.color_hue_shift,
                    widget.color_brightness_percent,
                )
            widget_gpu = _widget_gpu_telemetry(widget, telemetry)
            widget_unit = widget.temperature_unit or temperature_unit
            values = {
                "cpu-temperature": format_temperature(
                    cpu.get("temperature", 0), widget_unit
                ),
                "cpu-load": f"{cpu.get('load', 0)}%",
                "gpu-temperature": format_temperature(
                    (
                        widget_gpu.get("temperature", 0)
                        if isinstance(widget_gpu, Mapping)
                        else 0
                    ),
                    widget_unit,
                ),
                "gpu-load": (
                    f"{widget_gpu.get('load', 0)}%"
                    if isinstance(widget_gpu, Mapping)
                    else "0%"
                ),
                "network-upload": f"{network.get('upload', 0)} KB/s",
                "network-download": f"{network.get('download', 0)} KB/s",
                "time": now.strftime("%H:%M"),
                "date": now.strftime("%Y-%m-%d"),
            }
            arguments.extend((
                "-font", image_magick_font(widget.font),
                "-fill", color,
                "-pointsize", str(widget.size),
                "-gravity", "northwest",
                "-annotate",
                f"+{int(widget.x) + DISPLAY_CONTENT_INSET}"
                f"+{int(widget.y) + DISPLAY_CONTENT_INSET}",
                values.get(widget.metric, widget.text),
            ))
    try:
        result = subprocess.run(
            arguments + ["PNG32:-"], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HidError(f"unable to render telemetry overlay: {exc}") from exc
    return result.stdout


def render_animation(
    source: Path, mask_color: str | None = None, *, speed: float = 1.0
) -> bytes:
    """Render a GIF as the display's native 320x320 H.264 MP4 media format."""
    if mask_color is not None and re.fullmatch(r"#[0-9a-fA-F]{6}", mask_color) is None:
        raise ValueError("GIF mask color must be a six-digit hex color")
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("GIF playback speed must be a positive finite number")
    timing_filter = f",setpts=PTS/{speed:.6f}"
    with tempfile.NamedTemporaryFile(suffix=".mp4") as output:
        try:
            filter_arguments = (
                [
                    "-filter_complex",
                    (
                        "[0:v]scale=320:320:force_original_aspect_ratio=increase,"
                        "crop=320:320,format=gray,format=rgb24,"
                        f"lutrgb=r='val*{int(mask_color[1:3], 16)}/255':"
                        f"g='val*{int(mask_color[3:5], 16)}/255':"
                        f"b='val*{int(mask_color[5:7], 16)}/255'"
                        f"{timing_filter}[video]"
                    ),
                    "-map",
                    "[video]",
                ]
                if mask_color is not None
                else [
                    "-vf",
                    (
                        "scale=320:320:force_original_aspect_ratio=increase,"
                        f"crop=320:320{timing_filter}"
                    ),
                ]
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-stream_loop",
                    "9",
                    "-i",
                    str(source),
                    *filter_arguments,
                    "-r",
                    "20",
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    "675k",
                    "-minrate",
                    "675k",
                    "-maxrate",
                    "675k",
                    "-bufsize",
                    "675k",
                    "-movflags",
                    "+faststart",
                    output.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HidError(f"unable to render animation {source}: {exc}") from exc
        return Path(output.name).read_bytes()


def render_background_image(source: Path) -> bytes:
    """Render a still image as a looping-compatible H.264 background."""
    return _render_static_background(["-loop", "1", "-i", str(source)], source)


def render_blank_background() -> bytes:
    """Render a black video background that effectively removes selected media."""
    return _render_static_background(
        ["-f", "lavfi", "-i", "color=c=black:s=320x320:r=20"],
        "blank background",
    )


def render_masked_gif_preview(
    source: Path, mask_color: str, directory: Path
) -> Path:
    """Cache an animated GIF tinted with the effective display mask color."""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", mask_color) is None:
        raise ValueError("GIF preview mask color must be a six-digit hex color")
    try:
        modified = source.stat().st_mtime_ns
    except OSError as exc:
        raise HidError(f"unable to read GIF preview source {source}: {exc}") from exc
    key = hashlib.sha256(
        f"{source.resolve()}:{modified}:{mask_color.lower()}".encode()
    ).hexdigest()
    target = directory / f"{key}.gif"
    if target.is_file():
        _prune_gif_preview_cache(directory, target)
        return target
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{key}.tmp.gif"
    try:
        subprocess.run(
            [
                "magick",
                str(source),
                "-coalesce",
                "-colorspace",
                "gray",
                "+level-colors",
                f"#000000,{mask_color}",
                "-layers",
                "Optimize",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        temporary.replace(target)
        _prune_gif_preview_cache(directory, target)
    except (OSError, subprocess.CalledProcessError) as exc:
        temporary.unlink(missing_ok=True)
        raise HidError(f"unable to render masked GIF preview {source}: {exc}") from exc
    return target


def _prune_gif_preview_cache(directory: Path, current: Path) -> None:
    """Bound generated previews while retaining the preview currently in use."""
    previews = sorted(
        (
            preview
            for preview in directory.glob("*.gif")
            if preview != current
        ),
        key=lambda preview: preview.stat().st_mtime_ns,
        reverse=True,
    )
    for obsolete in previews[GIF_PREVIEW_CACHE_LIMIT - 1 :]:
        obsolete.unlink(missing_ok=True)


def _render_static_background(input_args: list[str], description: object) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as output:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    *input_args,
                    "-t",
                    "30",
                    "-vf",
                    (
                        "scale=320:320:force_original_aspect_ratio=increase,"
                        "crop=320:320"
                    ),
                    "-r",
                    "20",
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-b:v",
                    "675k",
                    "-minrate",
                    "675k",
                    "-maxrate",
                    "675k",
                    "-bufsize",
                    "675k",
                    "-movflags",
                    "+faststart",
                    output.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HidError(f"unable to render {description}: {exc}") from exc
        return Path(output.name).read_bytes()


def _expect_success(response: bytes, *, body_required: bool = True) -> None:
    if b" 200\r\n" not in response or (
        body_required and b'"state":"success"' not in response
    ):
        payload_end = int.from_bytes(response[1:3], "big")
        payload = response[3 : max(3, payload_end - 2)].decode(
            errors="replace"
        )
        raise HidError(f"display rejected the media transfer: {payload}")


def _receive_success(
    device: Device, *, body_required: bool = True, discard_block_acks: bool = False
) -> None:
    """Await a control reply, discarding delayed media-block acknowledgements."""
    for _ in range(3):
        response = device.receive()
        if discard_block_acks and b"AckNumber=0\r\n" in response:
            continue
        _expect_success(response, body_required=body_required)
        return
    raise HidError("display did not return a control-message acknowledgement")


def initialize_device(device: Device, session: MediaSession, timeout: int = 60) -> None:
    """Establish the same active display session used by the vendor app."""
    device.send(connect_message(session.next_sequence()))
    _receive_success(device, body_required=False)
    device.send(
        request_message("power", {"event": "resume"}, session.next_sequence())
    )
    _receive_success(device, body_required=False)
    device.send(request_message("timeout", {"value": timeout}, session.next_sequence()))
    _receive_success(device, body_required=False)


def upload_media(
    device: Device, session: MediaSession, data: bytes, name: str, extension: str
) -> None:
    """Transfer media through the vendor-captured block protocol."""
    chunks = [
        data[offset : offset + MEDIA_BLOCK_SIZE]
        for offset in range(0, len(data), MEDIA_BLOCK_SIZE)
    ]
    if not chunks:
        raise ValueError("cannot upload empty media")
    filename = f"{name}.{extension}"
    device.send(
        request_message(
            "transport",
            {"type": "media", "fileSize": len(data), "fileName": filename},
            session.next_sequence(),
        )
    )
    _receive_success(device, discard_block_acks=True)
    for index, chunk in enumerate(chunks):
        device.send(media_block(chunk, len(chunks), index))
    _receive_success(device, body_required=False)
    device.send(
        request_message(
            "transported",
            {"md5": "todo", "fileName": filename},
            session.next_sequence(),
        )
    )
    _receive_success(device, discard_block_acks=True)
