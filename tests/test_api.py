"""HTTP API behaviour tests (no device, no systemd)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from darkflash_space_station import api as api_module
from darkflash_space_station.api import ApiServer
from darkflash_space_station.layout import Layout, LayoutStore, Widget
from darkflash_space_station.service import install_api_service
from darkflash_space_station.telemetry import TelemetryError


def request(server: ApiServer, path: str, *, token: str | None = None, body: dict | None = None):
    url = f"http://{server.host}:{server.port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def make_server(
    tmp_path: Path,
    *,
    token: str | None = None,
    layouts: dict[str, Layout] | None = None,
    applied: list[str] | None = None,
    telemetry_state=None,
) -> ApiServer:
    store = LayoutStore(tmp_path / "layouts.json")
    if layouts:
        store.save(layouts)
    recorded = applied if applied is not None else []
    server = ApiServer(
        port=0,
        token=token,
        layout_store=store,
        apply_callback=lambda: recorded.append("applied"),
        telemetry_state=telemetry_state,
        display_active=lambda: True,
        api_active=lambda: True,
    )
    server.start()
    return server


def test_state_reports_active_layout(tmp_path: Path) -> None:
    layouts = {
        "Default": Layout("Default", [Widget("cpu-temperature")]),
        "Gaming": Layout("Gaming", [Widget("gpu-load", graph_style="bar")]),
    }
    server = make_server(tmp_path, layouts=layouts)
    try:
        status, state = request(server, "/api/state")
        assert status == 200
        assert state["active_layout"] == "Default"
        assert state["layouts"] == ["Default", "Gaming"]
        assert state["service_active"] is True
    finally:
        server.stop()


def test_post_layout_selects_and_restarts_service(tmp_path: Path) -> None:
    layouts = {"Default": Layout("Default"), "Night": Layout("Night")}
    applied: list[str] = []
    server = make_server(tmp_path, layouts=layouts, applied=applied)
    try:
        status, state = request(server, "/api/layout", body={"layout": "Night"})
        assert status == 200
        assert state["active_layout"] == "Night"
        assert applied == ["applied"]
        assert (server.layout_store.active_path).read_text().strip() == "Night"
    finally:
        server.stop()


def test_unknown_layout_and_bad_body_are_rejected(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    try:
        status, body = request(server, "/api/layout", body={"layout": "Ghost"})
        assert status == 404
        assert "Ghost" in body["error"]
        status, body = request(server, "/api/layout", body={})
        assert status == 400
    finally:
        server.stop()


def test_token_is_required_for_authenticated_requests(tmp_path: Path) -> None:
    server = make_server(tmp_path, token="secret")
    try:
        status, body = request(server, "/api/state")
        assert status == 401
        assert body["error"] == "unauthorized"
        status, state = request(server, "/api/state", token="secret")
        assert status == 200
        status, state = request(server, "/api/layout", token="secret", body={"layout": "Default"})
        assert status == 200
    finally:
        server.stop()


def test_non_loopback_bind_requires_token() -> None:
    with pytest.raises(ValueError, match="token is required"):
        ApiServer(host="0.0.0.0")
    server = ApiServer(host="10.0.0.5", token="secret")
    assert server.host == "10.0.0.5"


def test_telemetry_endpoint_uses_injected_reader(tmp_path: Path) -> None:
    layouts = {
        "Default": Layout(
            "Default",
            [Widget("gpu-load", graph_style="bar", gpu_bdf="0000:01:00.0")],
        )
    }
    seen: dict[str, object] = {}

    def fake_state() -> dict[str, object]:
        seen["gpu_bdfs"] = server.layout_gpu_bdfs()
        return {"cpu": {"load": 7}, "gpus": {}}

    server = make_server(tmp_path, layouts=layouts, telemetry_state=fake_state)
    try:
        status, payload = request(server, "/api/telemetry")
        assert status == 200
        assert payload == {"cpu": {"load": 7}, "gpus": {}}
        assert seen["gpu_bdfs"] == ["0000:01:00.0"]
    finally:
        server.stop()


def test_telemetry_error_is_reported_as_bad_gateway(tmp_path: Path) -> None:
    def failing_state() -> dict[str, object]:
        raise TelemetryError("CPU package temperature is unavailable")

    server = make_server(tmp_path, telemetry_state=failing_state)
    try:
        status, body = request(server, "/api/telemetry")
        assert status == 502
        assert "temperature" in body["error"]
    finally:
        server.stop()


def test_api_service_unit_and_env_are_installed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fake_systemctl(*arguments: str, check: bool = True):
        return None

    monkeypatch.setattr(api_module, "service_active", lambda: False)
    import darkflash_space_station.service as service_module

    monkeypatch.setattr(service_module, "_systemctl", fake_systemctl)

    destination = install_api_service(8791, "10.0.0.5", "secret", "0000:02:00.0")
    env = (tmp_path / ".config" / "darkflash-space-station" / "api.env").read_text()
    assert "PORT=8791" in env
    assert "BIND=10.0.0.5" in env
    assert "TOKEN=secret" in env
    assert "GPU_BDF=0000:02:00.0" in env
    unit = destination.read_text()
    assert "api serve" in unit
    assert destination.name == "darkflash-space-station-api.service"
