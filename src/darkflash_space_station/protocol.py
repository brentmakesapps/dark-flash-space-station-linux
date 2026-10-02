"""Framing for the darkFlash Space Station 1024-byte HID protocol."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

REPORT_SIZE = 1024
MEDIA_BLOCK_SIZE = 1000


def frame(payload: bytes) -> bytes:
    """Wrap a protocol payload in the device's length/checksum envelope."""
    total_length = len(payload) + 5
    if total_length > REPORT_SIZE:
        raise ValueError(f"payload is too large for a {REPORT_SIZE}-byte HID report")
    checksum = (sum(payload) + total_length) & 0xFF
    return b"Z" + total_length.to_bytes(2, "big") + payload + bytes((checksum,)) + b"Z"


def state_message(state: Mapping[str, object], sequence: int) -> bytes:
    """Build a `STATE all` telemetry update accepted by the stock device theme."""
    content = json.dumps(state, separators=(",", ":")).encode()
    headers = (
        f"SeqNumber={sequence}\r\n"
        f"Date={int(time.time() * 1000)}\r\n"
        "ContentType=json\r\n"
        f"ContentLength={len(content)}\r\n"
        "\r\n"
    ).encode()
    return frame(b"STATE all 1\r\n" + headers + content)


def request_message(command: str, content: Mapping[str, object], sequence: int) -> bytes:
    """Build a JSON `POST` request for a media-transfer control command."""
    payload = json.dumps(content, separators=(",", ":")).encode()
    headers = (
        f"SeqNumber={sequence}\r\n"
        f"Date={int(time.time() * 1000)}\r\n"
        "ContentType=json\r\n"
        f"ContentLength={len(payload)}\r\n"
        "\r\n"
    ).encode()
    return frame(f"POST {command} 1\r\n".encode() + headers + payload)


def connect_message(sequence: int) -> bytes:
    """Build the vendor-captured connection handshake request."""
    headers = f"SeqNumber={sequence}\r\nDate={int(time.time() * 1000)}\r\n\r\n"
    return frame(b"POST conn 1\r\n" + headers.encode())


def media_block(payload: bytes, block_count: int, block_index: int) -> bytes:
    """Build one captured-format PNG media block."""
    if not payload or len(payload) > MEDIA_BLOCK_SIZE:
        raise ValueError(f"media blocks must contain 1 to {MEDIA_BLOCK_SIZE} bytes")
    if not 1 <= block_count <= 0xFFFF:
        raise ValueError("invalid media block count")
    if not 0 <= block_index < block_count:
        raise ValueError("invalid media block index")
    length = len(payload) + 21
    header = (
        b"\x5c"
        + length.to_bytes(2, "big")
        + b"\x34"
        + block_count.to_bytes(2, "big")
        + block_index.to_bytes(2, "big")
        + b"\x01\x00\x00\x00"
        + b"\0" * 12
    )
    return (header + payload).ljust(REPORT_SIZE, b"\0")
