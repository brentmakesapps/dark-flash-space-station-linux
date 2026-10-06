"""HTTP API server for Home Assistant integration."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .layout import LayoutStore
from .settings import SettingsStore
from .service import api_service_active, service_active
from . import telemetry

DEFAULT_PORT = 8790
LOOPBACK = "127.0.0.1"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "darkflash-space-station/0.1"
    protocol_version = "HTTP/1.1"

    api: "ApiServer"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _respond(self, code: int, data: object) -> None:
        body = json.dumps(data, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.api.token
        if not token:
            return True
        return self.headers.get("Authorization", "").casefold() == f"bearer {token}".casefold()

    def _state(self) -> dict[str, object]:
        layouts = self.api.layout_store.load()
        return {
            "active_layout": self.api.layout_store.active_name(layouts),
            "layouts": sorted(layouts),
            "service_active": self.api.display_active(),
            "api_service_active": self.api.api_active(),
        }

    def do_GET(self) -> None:
        if not self._authorized():
            self._respond(401, {"error": "unauthorized"})
            return
        if self.path == "/":
            self._respond(
                200,
                {
                    "name": "dark-flash-space-station",
                    "endpoints": ["/api/state", "/api/layout", "/api/telemetry"],
                },
            )
            return
        if self.path == "/api/state":
            self._respond(200, self._state())
            return
        if self.path == "/api/telemetry":
            try:
                payload = self.api.telemetry_state()
            except Exception as exc:  # telemetry sources vary per host
                self._respond(502, {"error": str(exc)})
                return
            self._respond(200, payload)
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._respond(401, {"error": "unauthorized"})
            return
        if self.path != "/api/layout":
            self._respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond(400, {"error": "invalid JSON body"})
            return
        if not isinstance(data, dict):
            self._respond(400, {"error": "invalid JSON body"})
            return
        name = data.get("layout")
        if not isinstance(name, str) or not name.strip():
            self._respond(400, {"error": "layout name required"})
            return
        layouts = self.api.layout_store.load()
        if name not in layouts:
            self._respond(404, {"error": f"unknown layout {name!r}"})
            return
        self.api.layout_store.save_active(name, layouts)
        self.api.apply_callback()
        self._respond(200, self._state())


def _safe_probe(probe: Callable[[], bool]) -> Callable[[], bool]:
    """Report an inactive service instead of failing when systemctl is absent."""

    def probe_safely() -> bool:
        try:
            return probe()
        except (OSError, subprocess.SubprocessError):
            return False

    return probe_safely


class ApiServer:
    """Loopback (or token-protected LAN) HTTP server for Home Assistant."""

    def __init__(
        self,
        *,
        host: str = LOOPBACK,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        gpu_bdf: str | None = None,
        layout_store: LayoutStore | None = None,
        apply_callback: Callable[[], None] | None = None,
        telemetry_state: Callable[[], dict[str, object]] | None = None,
        display_active: Callable[[], bool] | None = None,
        api_active: Callable[[], bool] | None = None,
    ) -> None:
        if host != LOOPBACK and not token:
            raise ValueError("a token is required to bind to a non-loopback address")
        self.host = host
        self.port = port
        self.token = token
        self.layout_store = layout_store or LayoutStore()
        self.apply_callback = apply_callback or (lambda: None)
        self.telemetry_state = telemetry_state or self._live_telemetry_state
        self.display_active = display_active or _safe_probe(service_active)
        self.api_active = api_active or _safe_probe(api_service_active)
        self.gpu_bdf = gpu_bdf or SettingsStore().load().gpu_bdf
        self._cpu: telemetry.CpuUtilization | None = None
        self._network: telemetry.NetworkThroughput | None = None
        self._httpd: ThreadingHTTPServer | None = None

    def _live_telemetry_state(self) -> dict[str, object]:
        """Retained readers keep inter-request deltas for load and throughput."""
        if self._cpu is None:
            self._cpu = telemetry.CpuUtilization()
        if self._network is None:
            self._network = telemetry.NetworkThroughput()
        return telemetry.state(
            self._cpu,
            self.gpu_bdf,
            self._network,
            include_gpu_load=True,
            gpu_bdfs=self.layout_gpu_bdfs(),
        )

    def layout_gpu_bdfs(self) -> list[str]:
        """GPU BDFs referenced by widgets in the active layout."""
        try:
            layout = self.layout_store.active_layout()
        except ValueError:
            return []
        return sorted(
            {
                widget.gpu_bdf
                for widget in layout.widgets
                if widget.gpu_bdf and widget.gpu_bdf != self.gpu_bdf
            }
        )

    def handler_class(self) -> type[ApiHandler]:
        server = self

        class BoundApiHandler(ApiHandler):
            api = server

        return BoundApiHandler

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), self.handler_class())
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def serve_forever(self) -> None:
        self.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            self.stop()
