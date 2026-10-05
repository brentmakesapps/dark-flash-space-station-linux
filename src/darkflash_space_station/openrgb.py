"""Read-only OpenRGB color lookups for media masking."""

from __future__ import annotations

import colorsys

from .settings import AppSettings


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


def gif_mask_color(settings: AppSettings) -> str | None:
    """Resolve the saved GIF mask mode to the color to apply now."""
    if settings.gif_mask_mode == "none":
        return None
    if settings.gif_mask_mode == "manual":
        color = settings.gif_mask_color
    elif settings.gif_mask_mode == "openrgb":
        color = argb_v2_3_color()
    else:
        raise ValueError(f"unsupported GIF mask mode: {settings.gif_mask_mode}")
    return adjust_mask_color(
        color,
        settings.gif_hue_shift,
        settings.gif_brightness_percent,
    )


def adjust_mask_color(
    color: str, hue_shift: float = 0, brightness_percent: float = 100
) -> str:
    """Rotate hue and scale HSL lightness for display color calibration."""
    if (
        len(color) != 7
        or not color.startswith("#")
        or any(character not in "0123456789abcdefABCDEF" for character in color[1:])
    ):
        raise ValueError("mask color must be a six-digit hex color")
    red, green, blue = (
        int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)
    )
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    adjusted = colorsys.hls_to_rgb(
        (hue + hue_shift / 360) % 1,
        min(1, max(0, lightness * brightness_percent / 100)),
        saturation,
    )
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in adjusted)
