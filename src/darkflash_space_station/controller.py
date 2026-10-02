"""High-level, single-writer controls for the darkFlash Space Station."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Lock

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
from .protocol import connect_message, state_message
from .telemetry import (
    CpuUtilization,
    NetworkThroughput,
    TelemetryHistory,
    TemperatureAverage,
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
        with self._lock, Device(find_device()) as device:
            session = MediaSession()
            initialize_device(device, session)
            upload_media(
                device, session, render_animation(source, mask_color), source.stem, "mp4"
            )
            self._clear_overlay(device, session, source.stem)

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
    ) -> Iterator[dict[str, object]]:
        """Yield native telemetry payloads while streaming them to the display."""
        if interval <= 0:
            raise ValueError("telemetry interval must be greater than zero")
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
        with self._lock, Device(find_device()) as device:
            sequence = 0
            session = MediaSession()
            if overlay:
                initialize_device(device, session, timeout=0 if keep_awake else 60)
            while not stop():
                payload = state(
                    cpu_usage,
                    gpu_bdf,
                    network,
                    temperature_average,
                )
                history.add(payload)
                if overlay:
                    upload_media(
                        device,
                        session,
                        render_telemetry_overlay(
                            payload, layout, history, temperature_unit
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
