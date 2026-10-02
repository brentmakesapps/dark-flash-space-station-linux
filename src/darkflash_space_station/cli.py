"""Command line interface for the darkFlash Space Station controller."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .controller import SpaceStation
from .hid import HidError
from .layout import LayoutStore
from .settings import SettingsStore
from .service import (
    install_service,
    service_status,
    start_service,
    stop_service,
)

DEFAULT_GPU_BDF = "0000:04:00.0"


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
    telemetry.add_argument("--gpu-bdf", default=DEFAULT_GPU_BDF)
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
        "service", help="manage the background telemetry-overlay user service"
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

    commands.add_parser("gui", help="open the GTK 4 / Libadwaita controller")
    return parser


def main() -> None:
    args = _parser().parse_args()
    controller = SpaceStation()
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
            updates = controller.telemetry_updates(
                args.gpu_bdf,
                args.interval,
                lambda: False,
                overlay=args.overlay,
                layout=layout,
                keep_awake=args.keep_awake,
                temperature_unit=args.temperature_unit,
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
