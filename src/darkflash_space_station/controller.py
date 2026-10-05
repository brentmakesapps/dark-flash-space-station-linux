"""High-level, single-writer controls for the darkFlash Space Station."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from threading import Lock

from .animation import AdaptiveAnimation, StableSpeedSelector
from .hid import Device, HidError, find_device
from .media import (
    MediaSession,
    initialize_device,
    render_animation,
    render_png,
    render_telemetry_overlay,
    render_transparent_overlay,
    upload_media,
)
from .layout import Layout
from .openrgb import argb_v2_3_color
from .protocol import connect_message, state_message
from .telemetry import (
    CpuUtilization,
    NetworkThroughput,
    TelemetryHistory,
    TemperatureAverage,
    detected_gpus,
    state,
)


def _response_json(response: bytes) -> dict[str, object]:
    """Extract a JSON response body from a framed device reply."""
    total_length = int.from_bytes(response[1:3], "big")
    payload = response[3 : total_length - 2]
    _, separator, body = payload.partition(b"\r\n\r\n")
    if not separator:
        raise HidError("display response did not contain a message body")
    body_text = body.decode(errors="replace")
    if body_text.count("{") == body_text.count("}") + 1:
        body_text += "}"
    try:
        parsed = json.loads(body_text, strict=False)
    except json.JSONDecodeError as exc:
        raise HidError(f"display returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HidError("display response JSON was not an object")
    return parsed


def animation_load(payload: Mapping[str, object], source: str) -> float:
    """Return the load driving adaptive playback for the selected source."""
    cpu = payload.get("cpu")
    gpu = payload.get("gpu")
    cpu_load = float(cpu.get("load", 0)) if isinstance(cpu, Mapping) else 0.0
    gpu_load = float(gpu.get("load", 0)) if isinstance(gpu, Mapping) else 0.0
    if source == "none":
        return 0.0
    if source == "cpu":
        return cpu_load
    if source == "gpu":
        return gpu_load
    if source == "max":
        gpus = payload.get("gpus")
        all_gpu_loads = (
            [
                float(value.get("load", 0))
                for value in gpus.values()
                if isinstance(value, Mapping)
            ]
            if isinstance(gpus, Mapping)
            else []
        )
        return max(cpu_load, gpu_load, *all_gpu_loads)
    raise ValueError(f"unsupported animation load source: {source}")


class SpaceStation:
    """Execute foreground display actions without concurrent HID writers."""

    def __init__(self) -> None:
        self._lock = Lock()

    def status(self) -> dict[str, object]:
        with self._lock, Device(find_device()) as device:
            session = MediaSession()
            device.send(connect_message(session.next_sequence()))
            return _response_json(device.receive())

    def wake(self, timeout: int = 60) -> None:
        self._validate_timeout(timeout)
        with self._lock, Device(find_device()) as device:
            initialize_device(device, MediaSession(), timeout)

    def set_timeout(self, timeout: int) -> None:
        """Set the observed firmware sleep timeout in seconds."""
        self.wake(timeout)

    def show_image(self, source: Path) -> None:
        png = render_png(source)
        with self._lock, Device(find_device()) as device:
            session = MediaSession()
            initialize_device(device, session)
            upload_media(device, session, png, source.stem, "osd")

    def show_animation(self, source: Path, mask_color: str | None = None) -> None:
        self.show_rendered_animation(render_animation(source, mask_color), source.stem)

    def show_rendered_animation(self, animation: bytes, name: str) -> None:
        with self._lock, Device(find_device()) as device:
            session = MediaSession()
            initialize_device(device, session)
            upload_media(device, session, animation, name, "mp4")
            self._clear_overlay(device, session, name)

    def clear_overlay(self) -> None:
        with self._lock, Device(find_device()) as device:
            session = MediaSession()
            initialize_device(device, session)
            self._clear_overlay(device, session, "clear")

    def telemetry_updates(
        self,
        gpu_bdf: str,
        interval: float,
        stop: Callable[[], bool],
        *,
        overlay: bool = False,
        layout: Layout | None = None,
        keep_awake: bool = False,
        temperature_unit: str = "C",
        adaptive_animation: AdaptiveAnimation | None = None,
        animation_load_source: str = "none",
        animation_stability_seconds: float = 5,
    ) -> Iterator[dict[str, object]]:
        """Yield native telemetry payloads while streaming them to the display."""
        if interval <= 0:
            raise ValueError("telemetry interval must be greater than zero")
        if animation_load_source not in {"none", "cpu", "gpu", "max"}:
            raise ValueError("unsupported animation load source")
        if animation_stability_seconds <= 0:
            raise ValueError("animation stability duration must be greater than zero")
        cpu_usage = CpuUtilization()
        network = NetworkThroughput()
        temperature_average = TemperatureAverage()
        history = TelemetryHistory(
            max(
                (
                    max(1, int(widget.history_seconds))
                    for widget in (layout.widgets if layout is not None else [])
                    if widget.is_graph
                ),
                default=60,
            )
        )
        widget_gpu_bdfs = {
            widget.gpu_bdf or gpu_bdf
            for widget in (layout.widgets if layout is not None else [])
            if widget.metric.startswith("gpu-")
        }
        if animation_load_source == "max":
            widget_gpu_bdfs.update(gpu.bdf for gpu in detected_gpus())
        needs_gpu_load = any(
            widget.metric == "gpu-load"
            for widget in (layout.widgets if layout is not None else [])
        )
        uses_openrgb_text = any(
            not widget.is_graph and widget.color_source == "openrgb"
            for widget in (layout.widgets if layout is not None else [])
        )
        with self._lock, Device(find_device()) as device:
            sequence = 0
            session = MediaSession()
            if overlay:
                initialize_device(device, session, timeout=0 if keep_awake else 60)
            speed_selector = StableSpeedSelector(animation_stability_seconds)
            next_color_check = time.monotonic() + 3
            openrgb_text_color: str | None = None
            next_text_color_check = 0.0
            while not stop():
                payload = state(
                    cpu_usage,
                    gpu_bdf,
                    network,
                    temperature_average,
                    include_gpu_load=(
                        needs_gpu_load
                        or animation_load_source in {"gpu", "max"}
                    ),
                    gpu_bdfs=widget_gpu_bdfs,
                )
                history.add(payload)
                if overlay:
                    now = time.monotonic()
                    if uses_openrgb_text and now >= next_text_color_check:
                        openrgb_text_color = argb_v2_3_color()
                        next_text_color_check = now + 3
                    if adaptive_animation is not None:
                        selected_load = animation_load(
                            payload, animation_load_source
                        )
                        animation_bucket = speed_selector.update(selected_load, now)
                        if animation_bucket is not None:
                            upload_media(
                                device,
                                session,
                                adaptive_animation.variant(animation_bucket),
                                "background",
                                "mp4",
                            )
                            print(
                                f"Adaptive GIF speed bucket {animation_bucket} "
                                f"selected at {selected_load:.1f}% load",
                                flush=True,
                            )
                        if now >= next_color_check:
                            next_color_check = now + 3
                            if adaptive_animation.refresh_color():
                                upload_media(
                                    device,
                                    session,
                                    adaptive_animation.variant(
                                        speed_selector.current or 0
                                    ),
                                    "background",
                                    "mp4",
                                )
                                print(
                                    "Adaptive GIF variants rebuilt for OpenRGB color "
                                    f"{adaptive_animation.color}",
                                    flush=True,
                                )
                    upload_media(
                        device,
                        session,
                        render_telemetry_overlay(
                            payload,
                            layout,
                            history,
                            temperature_unit,
                            openrgb_text_color,
                        ),
                        "telemetry",
                        "osd",
                    )
                else:
                    device.send(state_message(payload, sequence))
                yield payload
                sequence = (sequence + 1) & 0xFFFF
                deadline = time.monotonic() + interval
                while not stop() and time.monotonic() < deadline:
                    time.sleep(min(0.1, deadline - time.monotonic()))

    @staticmethod
    def _clear_overlay(device: Device, session: MediaSession, name: str) -> None:
        upload_media(
            device,
            session,
            render_transparent_overlay(),
            f"{name}-overlay",
            "osd",
        )

    @staticmethod
    def _validate_timeout(timeout: int) -> None:
        if not 0 <= timeout <= 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")
