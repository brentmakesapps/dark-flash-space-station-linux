"""Read-only OpenRGB color lookups for media masking."""

from __future__ import annotations


class OpenRgbError(RuntimeError):
    """The requested current OpenRGB color could not be read."""


def argb_v2_3_color() -> str:
    """Return the first current color from the motherboard's ARGB_V2_3 zone."""
    try:
        from openrgb import OpenRGBClient

        client = OpenRGBClient(name="darkFlash Space Station")
        for device in client.devices:
            for zone in device.zones:
                if zone.name == "ARGB_V2_3" and zone.colors:
                    color = zone.colors[0]
                    return f"#{color.red:02x}{color.green:02x}{color.blue:02x}"
    except Exception as exc:
        raise OpenRgbError(
            "could not read OpenRGB ARGB_V2_3; start OpenRGB with --server"
        ) from exc
    raise OpenRgbError("OpenRGB ARGB_V2_3 has no readable LED color")
