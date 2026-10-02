"""Minimal hidraw access for the darkFlash Space Station display."""

from __future__ import annotations

import errno
import os
import select
from pathlib import Path

from .protocol import REPORT_SIZE

VENDOR_ID = 0x1D6B
PRODUCT_ID = 0x0102
MANUFACTURER = "darkFlash Inc."
PRODUCT = "darkFlash USB Device"


class HidError(RuntimeError):
    """The cooler display cannot be found or accessed."""


def find_device(sysfs: Path = Path("/sys/bus/hid/devices")) -> Path:
    """Find the cooler's hidraw node independently of its dynamic number."""
    for device in sorted(sysfs.iterdir(), key=lambda entry: entry.name):
        try:
            uevent = (device / "uevent").read_text()
            properties = dict(
                line.split("=", 1) for line in uevent.splitlines() if "=" in line
            )
            _, vendor, product = properties["HID_ID"].split(":")
            if (int(vendor, 16), int(product, 16)) != (VENDOR_ID, PRODUCT_ID):
                continue
            if properties.get("HID_NAME") != f"{MANUFACTURER} {PRODUCT}":
                continue
            hidraw = next((device / "hidraw").iterdir())
            return Path("/dev") / hidraw.name
        except (FileNotFoundError, KeyError, StopIteration, ValueError):
            continue
    raise HidError("darkFlash Space Station display was not found on hidraw")


class Device:
    """A Space Station display endpoint."""

    def __init__(self, path: Path) -> None:
        try:
            self._fd = os.open(path, os.O_RDWR)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise HidError(
                    f"permission denied opening {path}; install the darkFlash udev rule"
                ) from exc
            raise HidError(f"cannot open {path}: {exc.strerror}") from exc

    def send(self, message: bytes) -> None:
        """Send one padded output report."""
        if len(message) > REPORT_SIZE:
            raise ValueError(f"message exceeds {REPORT_SIZE}-byte report size")
        report = message.ljust(REPORT_SIZE, b"\0")
        written = os.write(self._fd, report)
        if written != REPORT_SIZE:
            raise HidError(f"short write to display: {written} bytes")

    def receive(self, timeout: float = 2.0) -> bytes:
        """Read one complete device response, rejecting protocol timeouts."""
        readable, _, _ = select.select((self._fd,), (), (), timeout)
        if not readable:
            raise HidError("timed out waiting for a response from the display")
        response = os.read(self._fd, REPORT_SIZE)
        if not response.startswith(b"Z") or len(response) < 5:
            raise HidError("display returned an invalid response frame")
        return response

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
