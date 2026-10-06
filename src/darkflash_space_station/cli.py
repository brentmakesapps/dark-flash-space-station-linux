"""Command line interface for the darkFlash Space Station controller."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .api import ApiServer, DEFAULT_PORT, LOOPBACK
from .animation import AdaptiveAnimation, AnimationCache
from .controller import SpaceStation
from .hid import HidError
from .layout import Layout, LayoutMedia, LayoutStore
from .media import render_background_image, render_blank_background
from .openrgb import gif_mask_color
from .settings import AppSettings, SettingsStore
from .service import (
    api_service_status,
    install_api_service,
    install_service,
    service_status,
    start_api_service,
    start_service,
    stop_api_service,
    stop_service,
)

def restart_display_service() -> None:
    """Restart the always-on overlay so it picks up the newly active layout."""
    stop_service()
    start_service()

def restore_saved_animation(
    controller: SpaceStation, store: SettingsStore, settings: AppSettings
) -> bool:
    """Reapply the retained GIF and its saved mask, if one exists."""
    source = store.saved_gif()
    if source is None:
        return False
    controller.show_animation(source, gif_mask_color(settings))
    return True


def resolved_layout_media(
    layout: Layout, store: SettingsStore, settings: AppSettings
) -> tuple[LayoutMedia, bool]:
    """Resolve legacy inherited media into the active layout's runtime settings."""
    if layout.media.kind != "inherit":
        return layout.media, False
    source = store.saved_gif()
    return (
        LayoutMedia(
            kind="gif" if source else "none",
            file=str(source) if source else "",
            gif_mask_mode=settings.gif_mask_mode,
            gif_mask_color=settings.gif_mask_color,
            gif_speed_source=settings.gif_speed_source,
            gif_speed_gpu_bdf=settings.gpu_bdf,
            gif_hue_shift=settings.gif_hue_shift,
            gif_brightness_percent=settings.gif_brightness_percent,
        ),
        True,
    )


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file does not exist: {path}")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Native controller for the darkFlash Space Station display."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="show device firmware and display state")
    wake = commands.add_parser("wake", help="wake the display and configure timeout")
    wake.add_argument("--timeout", type=int, default=60, help="sleep timeout in seconds")

    display = commands.add_parser("display", help="configure captured display settings")
    display_commands = display.add_subparsers(dest="display_command", required=True)
    timeout = display_commands.add_parser("timeout", help="set display sleep timeout")
    timeout.add_argument("seconds", type=int)

    media = commands.add_parser("media", help="manage background media and OSD overlays")
    media_commands = media.add_subparsers(dest="media_command", required=True)
    image = media_commands.add_parser("image", help="show a 320x320 still image")
    image.add_argument("file", type=_path)
    gif = media_commands.add_parser("gif", help="convert a GIF to looping H.264 video")
    gif.add_argument("file", type=_path)
    media_commands.add_parser("clear-overlay", help="clear foreground OSD content")

    telemetry = commands.add_parser("telemetry", help="send native stock-theme telemetry")
    telemetry.add_argument(
        "--gpu-bdf",
        default=SettingsStore().load().gpu_bdf,
        help="fallback GPU for legacy layouts without per-widget selection",
    )
    telemetry.add_argument("--interval", type=float, default=1.0)
    telemetry.add_argument(
        "--temperature-unit",
        choices=("C", "F"),
        default=SettingsStore().load().temperature_unit,
        help="temperature display unit (default: saved GUI preference or C)",
    )
    telemetry.add_argument("--once", action="store_true")
    telemetry.add_argument(
        "--overlay",
        action="store_true",
        help="render CPU/GPU readings as a foreground OSD above video backgrounds",
    )
    telemetry.add_argument(
        "--layout",
        help="saved layout name; defaults to the active saved layout",
    )
    telemetry.add_argument(
        "--keep-awake",
        action="store_true",
        help="disable display sleep while an overlay stream is active",
    )

    service = commands.add_parser(
        "service", help="manage the always-on display user service"
    )
    service_commands = service.add_subparsers(dest="service_command", required=True)
    service_commands.add_parser(
        "install", help="install/update the user unit and reload systemd"
    )
    for name, help_text in (
        ("start", "enable and start the background overlay"),
        ("stop", "stop the background overlay"),
        ("status", "show background overlay status"),
    ):
        service_commands.add_parser(name, help=help_text)

    api_parser = commands.add_parser(
        "api",
        help="Home Assistant integration HTTP API for layout control",
        description="Home Assistant integration HTTP API for layout control.",
    )
    api_common = argparse.ArgumentParser(add_help=False)
    api_common.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    api_common.add_argument(
        "--bind",
        default=LOOPBACK,
        help="listen address; a token is required for non-loopback addresses",
    )
    api_common.add_argument(
        "--token", help="bearer token required by Home Assistant requests"
    )
    api_common.add_argument(
        "--gpu-bdf", help="GPU BDF for telemetry (default: saved GUI preference)"
    )

    api_commands = api_parser.add_subparsers(dest="api_command", required=True)
    api_commands.add_parser(
        "serve", parents=[api_common], help="run the API server in the foreground"
    )
    api_commands.add_parser(
        "install", parents=[api_common], help="install/update the API user unit"
    )
    for name, help_text in (
        ("start", "enable and start the API service"),
        ("stop", "stop the API service"),
        ("status", "show API service status"),
    ):
        api_commands.add_parser(name, parents=[api_common], help=help_text)

    commands.add_parser("gui", help="open the GTK 4 / Libadwaita controller")
    return parser


def main() -> None:
    args = _parser().parse_args()
    controller = SpaceStation()
    settings_store = SettingsStore()
    settings = settings_store.load()
    try:
        if args.command == "status":
            print(json.dumps(controller.status(), indent=2, sort_keys=True))
        elif args.command == "wake":
            controller.wake(args.timeout)
        elif args.command == "display":
            controller.set_timeout(args.seconds)
        elif args.command == "media":
            if args.media_command == "image":
                controller.show_image(args.file)
            elif args.media_command == "gif":
                controller.show_animation(args.file)
            else:
                controller.clear_overlay()
        elif args.command == "telemetry":
            layout = LayoutStore().active_layout(args.layout) if args.overlay else None
            adaptive_animation = None
            animation_load_source = settings.gif_speed_source
            if args.overlay:
                media, reload_media_settings = resolved_layout_media(
                    layout, settings_store, settings
                )
                animation_gpu_bdf = media.gif_speed_gpu_bdf or args.gpu_bdf
                media_settings = settings if reload_media_settings else media
                animation_load_source = media.gif_speed_source
                if media.kind == "gif":
                    saved_gif = Path(media.file) if media.file else None
                    if saved_gif is None or not saved_gif.is_file():
                        raise ValueError(
                            f"layout media does not exist: {media.file or '(missing path)'}"
                        )
                    adaptive_animation = AdaptiveAnimation(
                        saved_gif,
                        media_settings,
                        AnimationCache(
                            settings_store,
                            (
                                saved_gif.parent
                                / f"{saved_gif.stem}-speeds"
                                if not reload_media_settings
                                else None
                            ),
                        ),
                        reload_settings=reload_media_settings,
                    )
                elif media.kind == "image" and media.file:
                    image = Path(media.file)
                    if not image.is_file():
                        raise ValueError(f"layout media does not exist: {image}")
                    controller.show_rendered_animation(
                        render_background_image(image), image.stem
                    )
                elif media.kind == "none" and not reload_media_settings:
                    controller.show_rendered_animation(
                        render_blank_background(), "blank"
                    )
            updates = controller.telemetry_updates(
                animation_gpu_bdf if args.overlay else args.gpu_bdf,
                args.interval,
                lambda: False,
                overlay=args.overlay,
                layout=layout,
                keep_awake=args.keep_awake,
                temperature_unit=args.temperature_unit,
                adaptive_animation=adaptive_animation,
                animation_load_source=animation_load_source,
            )
            for payload in updates:
                print(json.dumps(payload, sort_keys=True))
                if args.once:
                    break
        elif args.command == "service":
            if args.service_command == "install":
                print(f"Installed {install_service()}")
            elif args.service_command == "start":
                start_service()
            elif args.service_command == "stop":
                stop_service()
            else:
                print(service_status())
        elif args.command == "api":
            if args.api_command == "serve":
                server = ApiServer(
                    host=args.bind,
                    port=args.port,
                    token=args.token,
                    gpu_bdf=args.gpu_bdf,
                    apply_callback=restart_display_service,
                )
                print(
                    f"API server listening on {server.host}:{server.port}",
                    flush=True,
                )
                server.serve_forever()
            elif args.api_command == "install":
                print(
                    f"Installed "
                    f"{install_api_service(args.port, args.bind, args.token, args.gpu_bdf)}"
                )
            elif args.api_command == "start":
                start_api_service()
            elif args.api_command == "stop":
                stop_api_service()
            else:
                print(api_service_status())
        else:
            from .gui import main as gui_main

            gui_main()
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise SystemExit(detail) from exc
    except (HidError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
