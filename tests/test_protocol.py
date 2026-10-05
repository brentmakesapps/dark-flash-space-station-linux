from pathlib import Path
from types import SimpleNamespace

import darkflash_space_station.media as media
from darkflash_space_station.animation import (
    AdaptiveAnimation,
    AnimationCache,
    CACHE_VERSION,
    SPEED_MULTIPLIERS,
    StableSpeedSelector,
    speed_bucket,
)
from darkflash_space_station.cli import resolved_layout_media, restore_saved_animation
from darkflash_space_station.protocol import (
    MEDIA_BLOCK_SIZE,
    REPORT_SIZE,
    connect_message,
    frame,
    media_block,
    state_message,
)
from darkflash_space_station.media import (
    MediaSession,
    _expect_success,
    _graph_values,
    format_temperature,
    image_magick_font,
    render_background_image,
    render_blank_background,
    render_masked_gif_preview,
    render_telemetry_overlay,
)
from darkflash_space_station.openrgb import adjust_mask_color
from darkflash_space_station.controller import _response_json, animation_load
from darkflash_space_station.gui import installed_font_families, restored_gif_sync_state
from darkflash_space_station.layout import (
    Layout,
    LayoutDraft,
    LayoutMedia,
    LayoutStore,
    Widget,
    resize_values,
    snap_to_grid,
    widget_selector_state,
)
from darkflash_space_station.settings import AppSettings, SettingsStore
from darkflash_space_station.service import unit_text
from darkflash_space_station.telemetry import (
    DetectedGpu,
    TelemetryHistory,
    TemperatureAverage,
    _nvtop_device_for_gpu,
    detected_gpus,
    state,
)


def test_installed_font_families_uses_fontconfig(monkeypatch) -> None:
    monkeypatch.setattr(
        "darkflash_space_station.gui.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="Advanced Pixel\\-7\nCubic Core Mono, Cubic Core Mono Medium\nHumanoid\n"
        ),
    )

    assert installed_font_families() == [
        "Advanced Pixel-7",
        "Cubic Core Mono",
        "Cubic Core Mono Medium",
        "DejaVu Sans",
        "Humanoid",
        "Liberation Sans",
        "Monospace",
        "Serif",
    ]


def test_image_magick_font_uses_cubic_core_mono_postscript_name() -> None:
    assert image_magick_font("CubicCoreMono") == "CubicCoreMono-Regular"


def test_snap_to_grid_rounds_to_nearest_grid_line() -> None:
    assert snap_to_grid(19) == 0
    assert snap_to_grid(20) == 40
    assert snap_to_grid(61) == 80


def test_image_magick_font_resolves_fontconfig_file(monkeypatch) -> None:
    image_magick_font.cache_clear()
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="iA Writer Mono S\t/fonts/iAWriterMonoS-Regular.ttf\n"
        ),
    )

    assert image_magick_font("iA Writer Mono S") == "/fonts/iAWriterMonoS-Regular.ttf"
    image_magick_font.cache_clear()


def test_temperature_average_uses_a_five_second_trailing_window() -> None:
    now = [0.0]
    average = TemperatureAverage(clock=lambda: now[0])

    assert average.add(35) == 35
    now[0] = 1
    assert average.add(50) == 42.5
    now[0] = 2
    assert average.add(35) == 40
    now[0] = 5
    assert average.add(50) == 45


def test_animation_speed_buckets_cover_ten_percent_ranges() -> None:
    assert [speed_bucket(load) for load in (0, 10, 11, 20, 21, 90, 91, 100)] == [
        0,
        0,
        1,
        1,
        2,
        8,
        9,
        9,
    ]
    assert SPEED_MULTIPLIERS[0] == 1
    assert SPEED_MULTIPLIERS[-1] == 3
    assert all(
        left < right
        for left, right in zip(SPEED_MULTIPLIERS, SPEED_MULTIPLIERS[1:])
    )


def test_max_animation_load_uses_highest_cpu_or_detected_gpu() -> None:
    payload = {
        "cpu": {"load": 25},
        "gpu": {"load": 15},
        "gpus": {
            "0000:00:02.0": {"load": 15},
            "0000:04:00.0": {"load": 100},
            "0000:85:00.0": {"load": 0},
        },
    }

    assert animation_load(payload, "gpu") == 15
    assert animation_load(payload, "max") == 100


def test_mask_color_tuning_rotates_hue_and_scales_brightness() -> None:
    assert adjust_mask_color("#ff0000", hue_shift=120) == "#00ff00"
    assert adjust_mask_color("#ff0000", brightness_percent=50) == "#800000"
    assert adjust_mask_color("#ff0000", brightness_percent=200) == "#ffffff"
    assert adjust_mask_color("#00ff00", hue_shift=-120, brightness_percent=50) == "#800000"


def test_animation_speed_change_requires_five_stable_seconds() -> None:
    selector = StableSpeedSelector(5)

    assert selector.update(5, 0) == 0
    assert selector.update(75, 1) is None
    assert selector.update(75, 5.9) is None
    assert selector.update(75, 6) == 7
    assert selector.update(5, 7) is None
    assert selector.update(75, 8) is None


def test_animation_cache_builds_ten_linear_speed_variants(
    monkeypatch, tmp_path
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    source = tmp_path / "background.gif"
    source.write_bytes(b"gif")
    calls = []
    monkeypatch.setattr(
        "darkflash_space_station.animation.render_animation",
        lambda selected, color, *, speed: calls.append((selected, color, speed))
        or str(speed).encode(),
    )

    cache = AnimationCache(store)
    cache.rebuild(source, "#ff3c00")

    assert len(calls) == 10
    assert calls[0] == (source, "#ff3c00", 1)
    assert calls[-1] == (source, "#ff3c00", 3)
    assert tuple(call[2] for call in calls) == SPEED_MULTIPLIERS
    assert cache.variant(0) == b"1.0"
    assert cache.variant(9) == b"3.0"
    assert cache.matches("#ff3c00")


def test_adaptive_animation_reloads_mask_tuning_changes(
    monkeypatch, tmp_path
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    source = tmp_path / "background.gif"
    source.write_bytes(b"gif")
    settings = AppSettings(gif_mask_mode="manual", gif_mask_color="#ff0000")
    store.save(settings)
    monkeypatch.setattr(
        "darkflash_space_station.animation.render_animation",
        lambda _source, color, *, speed: f"{color}:{speed}".encode(),
    )
    animation = AdaptiveAnimation(source, settings, AnimationCache(store))
    settings.gif_hue_shift = 120
    store.save(settings)

    assert animation.refresh_color()
    assert animation.color == "#00ff00"
    assert animation.variant(0).startswith(b"#00ff00:")


def test_matches_gpu_to_nvtop_using_pc_monitor_name_strategy() -> None:
    gpu = DetectedGpu("0000:04:00.0", "xe", "Intel Arc Pro B70")
    snapshot = [
        {"device_name": "Battlemage G31 (Arc Pro B70)", "gpu_util": "75%"},
        {"device_name": "Battlemage G21 (Arc Pro B50)", "gpu_util": "25%"},
    ]

    assert _nvtop_device_for_gpu(gpu, snapshot) == snapshot[0]


def test_frame_uses_vendor_length_and_checksum() -> None:
    assert frame(b"abc") == b"Z\x00\x08abc.Z"


def test_frame_matches_captured_vendor_conn_checksum() -> None:
    payload = b"POST conn 1\r\nSeqNumber=0\r\nDate=1790455600425\r\n\r\n"
    assert frame(payload).endswith(b"\x50Z")


def test_connect_message_matches_vendor_command_shape() -> None:
    message = connect_message(0)
    assert b"POST conn 1\r\nSeqNumber=0\r\nDate=" in message
    assert b"ContentType" not in message


def test_state_message_fits_the_hid_report() -> None:
    message = state_message({"cpu": {"temperature": 42}}, 7)
    assert message.startswith(b"Z")
    assert message.endswith(b"Z")
    assert len(message) <= REPORT_SIZE


def test_media_block_matches_captured_header() -> None:
    block = media_block(b"\x89PNG", 8, 0)
    assert block[:24] == bytes.fromhex(
        "5c0019340008000001000000000000000000000000000000"
    )
    assert block[24:28] == b"\x89PNG"
    assert len(block) == REPORT_SIZE
    assert MEDIA_BLOCK_SIZE == 1000


def test_media_session_uses_consecutive_sequence_numbers() -> None:
    session = MediaSession()

    assert [session.next_sequence() for _ in range(5)] == [0, 1, 2, 3, 4]


def test_block_ack_is_accepted_for_media_blocks() -> None:
    _expect_success(b"Z\x00\x1b1 200\r\nAckNumber=0\r\n\r\n(Z", body_required=False)


def test_connection_status_allows_firmware_control_bytes() -> None:
    payload = b'1 200\r\nContentType=json\r\n\r\n{"sn":"BYZL\x01"'
    response = b"Z" + (len(payload) + 5).to_bytes(2, "big") + payload + b"\0Z"

    assert _response_json(response)["sn"] == "BYZL\x01"


def test_detected_gpus_returns_display_controllers(tmp_path) -> None:
    gpu = tmp_path / "0000:85:00.0"
    gpu.mkdir()
    (gpu / "class").write_text("0x030000\n")
    (gpu / "vendor").write_text("0x8086\n")
    (gpu / "device").write_text("0xe212\n")
    other = tmp_path / "0000:00:1f.0"
    other.mkdir()
    (other / "class").write_text("0x060100\n")
    ids = tmp_path / "pci.ids"
    ids.write_text("8086  Intel Corporation\n\te212  Battlemage G21 [Arc Pro B50]\n")

    assert [
        (entry.bdf, entry.driver, entry.label)
        for entry in detected_gpus(tmp_path, ids)
    ] == [
        ("0000:85:00.0", "unbound", "Intel Arc Pro B50")
    ]


def test_telemetry_overlay_is_a_png() -> None:
    overlay = render_telemetry_overlay(
        {"cpu": {"temperature": 42, "load": 17}, "gpu": {"temperature": 35, "load": 4}}
    )

    assert overlay.startswith(b"\x89PNG\r\n\x1a\n")


def test_temperature_formatting_uses_degree_symbol_and_converts_fahrenheit() -> None:
    assert format_temperature(34) == "34°C"
    assert format_temperature(34, "F") == "93°F"


def test_telemetry_renderer_uses_selected_temperature_unit(monkeypatch) -> None:
    command: list[str] = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        return SimpleNamespace(stdout=b"PNG")

    monkeypatch.setattr(media.subprocess, "run", run)

    assert media.render_telemetry_overlay(
        {"cpu": {"temperature": 34, "load": 17}, "gpu": {"temperature": 35, "load": 4}},
        temperature_unit="F",
    ) == b"PNG"
    assert "93°F" in command


def test_telemetry_renderer_uses_openrgb_for_selected_text_widgets(
    monkeypatch,
) -> None:
    command: list[str] = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        return SimpleNamespace(stdout=b"PNG")

    monkeypatch.setattr(media.subprocess, "run", run)
    layout = Layout(
        "OpenRGB",
        [
            Widget(
                "custom-text",
                color="#ffffff",
                color_source="openrgb",
                color_hue_shift=30,
                color_brightness_percent=125,
                text="RGB",
            )
        ],
    )

    assert render_telemetry_overlay(
        {"cpu": {}, "gpu": {}, "network": {}},
        layout,
        openrgb_color="#ff3c00",
    ) == b"PNG"
    assert command[command.index("-fill") + 1] == adjust_mask_color(
        "#ff3c00", 30, 125
    )


def test_telemetry_renderer_uses_each_widgets_gpu_and_temperature_unit(
    monkeypatch,
) -> None:
    command: list[str] = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        return SimpleNamespace(stdout=b"PNG")

    monkeypatch.setattr(media.subprocess, "run", run)
    monkeypatch.setattr(media, "image_magick_font", lambda font: font)
    layout = Layout(
        "GPUs",
        [
            Widget(
                "gpu-temperature",
                gpu_bdf="0000:04:00.0",
                temperature_unit="C",
            ),
            Widget(
                "gpu-temperature",
                gpu_bdf="0000:85:00.0",
                temperature_unit="F",
            ),
        ],
    )

    assert media.render_telemetry_overlay(
        {
            "cpu": {"temperature": 34, "load": 17},
            "gpu": {"temperature": 0, "load": 0},
            "gpus": {
                "0000:04:00.0": {"temperature": 40, "load": 25},
                "0000:85:00.0": {"temperature": 50, "load": 75},
            },
        },
        layout,
    ) == b"PNG"
    assert "40°C" in command
    assert "122°F" in command


def test_state_collects_every_layout_gpu(monkeypatch) -> None:
    monkeypatch.setattr(
        "darkflash_space_station.telemetry.cpu_temperature_c", lambda: 38
    )
    monkeypatch.setattr(
        "darkflash_space_station.telemetry.gpu_temperature_c",
        lambda bdf: {"0000:04:00.0": 45, "0000:85:00.0": 55}[bdf],
    )
    monkeypatch.setattr(
        "darkflash_space_station.telemetry._nvtop_snapshot", lambda: [{}]
    )
    monkeypatch.setattr(
        "darkflash_space_station.telemetry.gpu_utilization_percent",
        lambda bdf, _snapshot: {"0000:04:00.0": 20, "0000:85:00.0": 70}[bdf],
    )

    payload = state(
        SimpleNamespace(read=lambda: 10),
        "0000:04:00.0",
        include_gpu_load=True,
        gpu_bdfs=["0000:85:00.0"],
    )

    assert payload["gpu"]["temperature"] == 45
    assert payload["gpus"]["0000:04:00.0"]["load"] == 20
    assert payload["gpus"]["0000:85:00.0"] == {
        "load": 70,
        "temperature": 55,
        "fan": 0,
        "speed": 0,
        "power": 0,
        "voltage": 0,
        "memoryUsage": 0,
        "dedicated": 0,
        "dedicatedTotal": 0,
    }


def test_gpu_graph_history_uses_widget_gpu_and_temperature_unit() -> None:
    history = TelemetryHistory()
    history.add(
        {
            "gpu": {"temperature": 40, "load": 10},
            "gpus": {
                "0000:04:00.0": {"temperature": 45, "load": 20},
                "0000:85:00.0": {"temperature": 50, "load": 70},
            },
        }
    )
    widget = Widget(
        "gpu-temperature",
        graph_style="line",
        gpu_bdf="0000:85:00.0",
        temperature_unit="F",
    )

    assert _graph_values(widget, {}, history) == [122]


def test_telemetry_renderer_applies_the_panel_alignment_inset(monkeypatch) -> None:
    command: list[str] = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        return SimpleNamespace(stdout=b"PNG")

    monkeypatch.setattr(media.subprocess, "run", run)
    layout = Layout(
        "Inset",
        [
            Widget("cpu-temperature", 0, 0),
            Widget("cpu-load", 0, 0, graph_style="bar", width=20, height=20),
        ],
    )

    assert media.render_telemetry_overlay(
        {"cpu": {"temperature": 34, "load": 17}, "gpu": {}, "network": {}},
        layout,
    ) == b"PNG"
    assert "+24+24" in command
    assert "rectangle 24,41 43,44" in command


def test_telemetry_history_keeps_only_last_sixty_seconds() -> None:
    now = [0.0]
    history = TelemetryHistory(clock=lambda: now[0])
    payload = {
        "cpu": {"temperature": 42, "load": 17},
        "gpu": {"temperature": 35, "load": 4},
        "network": {"upload": 2.4, "download": 15.8},
    }
    history.add(payload)
    now[0] = 30
    payload["cpu"]["load"] = 28
    history.add(payload)
    now[0] = 61

    assert history.values("cpu-load") == [28.0]


def test_telemetry_history_returns_requested_rolling_window() -> None:
    now = [0.0]
    history = TelemetryHistory(120, clock=lambda: now[0])
    payload = {
        "cpu": {"temperature": 42, "load": 17},
        "gpu": {"temperature": 35, "load": 4},
        "network": {"upload": 2.4, "download": 15.8},
    }
    history.add(payload)
    now[0] = 30
    payload["cpu"]["load"] = 28
    history.add(payload)
    now[0] = 61

    assert history.values("cpu-load", 60) == [28.0]
    assert history.values("cpu-load", 120) == [17.0, 28.0]


def test_graph_renderer_uses_each_widgets_history_window() -> None:
    now = [0.0]
    history = TelemetryHistory(120, clock=lambda: now[0])
    payload = {
        "cpu": {"temperature": 42, "load": 17},
        "gpu": {"temperature": 35, "load": 4},
        "network": {"upload": 2.4, "download": 15.8},
    }
    history.add(payload)
    now[0] = 30
    payload["cpu"]["load"] = 28
    history.add(payload)
    now[0] = 61

    assert _graph_values(
        Widget("cpu-load", graph_style="line", history_seconds=60), payload, history
    ) == [28.0]
    assert _graph_values(
        Widget("cpu-load", graph_style="line", history_seconds=120), payload, history
    ) == [17.0, 28.0]


def test_graph_widgets_persist_and_render_all_styles(tmp_path) -> None:
    widgets = [
        Widget("cpu-temperature", 10, 10, graph_style="bar", width=80, height=40, history_seconds=15),
        Widget("cpu-load", 100, 10, graph_style="line", width=80, height=40),
        Widget(
            "gpu-temperature",
            10,
            80,
            graph_style="circular-line",
            width=60,
            height=60,
            gpu_bdf="0000:85:00.0",
            temperature_unit="F",
        ),
        Widget("network-download", 100, 80, graph_style="pie", width=60, height=60),
    ]
    store = LayoutStore(tmp_path / "layouts.json")
    store.save({"Graphs": Layout("Graphs", widgets)})
    layout = store.load()["Graphs"]
    assert layout.widgets[2].gpu_bdf == "0000:85:00.0"
    assert layout.widgets[2].temperature_unit == "F"
    history = TelemetryHistory(clock=lambda: 1)
    history.add(
        {
            "cpu": {"temperature": 42, "load": 17},
            "gpu": {"temperature": 35, "load": 4},
            "network": {"upload": 2.4, "download": 15.8},
        }
    )

    overlay = render_telemetry_overlay(
        {
            "cpu": {"temperature": 42, "load": 17},
            "gpu": {"temperature": 35, "load": 4},
            "network": {"upload": 2.4, "download": 15.8},
        },
        layout,
        history,
    )

    assert [widget.graph_style for widget in layout.widgets] == [
        "bar",
        "line",
        "circular-line",
        "pie",
    ]
    assert [widget.history_seconds for widget in layout.widgets] == [15, 60, 60, 60]
    assert overlay.startswith(b"\x89PNG\r\n\x1a\n")


def test_circular_line_graph_uses_both_resized_dimensions() -> None:
    values = [0, 100, 100, 100]
    narrow = Widget(
        "cpu-load", graph_style="circular-line", width=40, height=40
    )
    wide = Widget(
        "cpu-load", graph_style="circular-line", width=120, height=40
    )

    assert media._graph_draw(narrow, values)[-1] != media._graph_draw(wide, values)[-1]


def test_gauge_graph_styles_render_semicircle_progress_and_full_ring() -> None:
    values = [75]
    semicircle = Widget("cpu-load", graph_style="semicircle-gauge")
    ring = Widget("cpu-load", graph_style="ring-gauge")

    assert "arc 43,43 157,157 180,315.0" in media._graph_draw(semicircle, values)
    assert "#ffffff80" in media._graph_draw(ring, values)
    assert "arc 43,43 157,157 -90,270" in media._graph_draw(ring, values)


def test_old_graph_layout_defaults_history_seconds(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    store.path.write_text(
        '{"Graphs": [{"metric": "cpu-load", "graph_style": "line"}]}'
    )

    assert store.load()["Graphs"].widgets[0].history_seconds == 60
    assert store.load()["Graphs"].media == LayoutMedia(kind="inherit")


def test_layout_media_round_trips_with_gif_settings(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    selected = LayoutMedia(
        kind="gif",
        file="/media/layout.gif",
        gif_mask_mode="openrgb",
        gif_mask_color="#ff3c00",
        gif_speed_source="max",
        gif_speed_gpu_bdf="0000:04:00.0",
        gif_hue_shift=25,
        gif_brightness_percent=135,
    )

    store.save({"Gaming": Layout("Gaming", [Widget("gpu-load")], selected)})

    assert store.load()["Gaming"].media == selected


def test_text_widget_openrgb_color_source_round_trips(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    store.save(
        {
            "RGB": Layout(
                "RGB",
                [
                    Widget(
                        "time",
                        color="#ffffff",
                        color_source="openrgb",
                        color_hue_shift=-15,
                        color_brightness_percent=150,
                    )
                ],
            )
        }
    )

    widget = store.load()["RGB"].widgets[0]
    assert widget.color_source == "openrgb"
    assert widget.color_hue_shift == -15
    assert widget.color_brightness_percent == 150


def test_render_background_image_encodes_a_static_h264_video(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "background.png"
    source.write_bytes(b"png")
    command = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        Path(arguments[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(media.subprocess, "run", run)

    assert render_background_image(source) == b"mp4"
    assert command[:5] == ["ffmpeg", "-v", "error", "-y", "-loop"]
    assert str(source) in command
    assert "scale=320:320:force_original_aspect_ratio=increase,crop=320:320" in command
    assert ["-c:v", "libx264"] == command[
        command.index("-c:v") : command.index("-c:v") + 2
    ]


def test_render_blank_background_uses_a_black_ffmpeg_source(monkeypatch) -> None:
    command = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        Path(arguments[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(media.subprocess, "run", run)

    assert render_blank_background() == b"mp4"
    assert ["-f", "lavfi", "-i", "color=c=black:s=320x320:r=20"] == command[
        command.index("-f") : command.index("-f") + 4
    ]


def test_masked_gif_preview_is_tinted_and_cached(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.gif"
    source.write_bytes(b"gif")
    commands = []

    def run(arguments, **_kwargs):
        commands.append(arguments)
        Path(arguments[-1]).write_bytes(b"masked")

    monkeypatch.setattr(media.subprocess, "run", run)
    directory = tmp_path / "previews"
    directory.mkdir()
    for index in range(35):
        (directory / f"old-{index}.gif").write_bytes(b"old")

    first = render_masked_gif_preview(source, "#ff3c00", directory)
    second = render_masked_gif_preview(source, "#ff3c00", directory)

    assert first == second
    assert first.read_bytes() == b"masked"
    assert len(commands) == 1
    assert "+level-colors" in commands[0]
    assert "#000000,#ff3c00" in commands[0]
    assert len(list(directory.glob("*.gif"))) == media.GIF_PREVIEW_CACHE_LIMIT


def test_resize_values_bounds_text_font_size_and_graph_dimensions() -> None:
    text = Widget("custom-text", size=24)
    assert resize_values(text, 24, 120, 60, 20, 20) == (44, 120, 60)
    assert resize_values(text, 24, 120, 60, -100, -100) == (8, 120, 60)

    graph = Widget("cpu-load", x=280, y=290, graph_style="line", width=40, height=40)
    assert resize_values(graph, 24, 40, 40, 100, 100) == (24, 40, 30)
    assert resize_values(graph, 24, 40, 40, -100, -100) == (24, 20, 20)


def test_circular_graphs_are_square_when_created_or_resized() -> None:
    circular = Widget(
        "cpu-load", x=200, y=200, graph_style="ring-gauge", width=60, height=40
    )

    assert (circular.width, circular.height) == (60, 60)
    assert resize_values(circular, 24, 60, 60, 100, 10) == (24, 120, 120)
    assert resize_values(circular, 24, 60, 60, -10, -40) == (24, 20, 20)


def test_active_layout_is_persisted_and_defaults_to_default(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    layouts = {
        "Gaming": Layout("Gaming"),
        "Default": Layout("Default"),
    }
    store.save(layouts)

    assert store.active_layout().name == "Default"

    store.save_active("Gaming", layouts)
    assert store.active_layout().name == "Gaming"


def test_layout_draft_writes_only_when_explicitly_saved(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    layouts = {
        "Default": Layout("Default"),
        "Gaming": Layout("Gaming"),
    }
    store.save(layouts)
    store.save_active("Default", layouts)
    saved_layouts = store.path.read_text()
    saved_active = store.active_path.read_text()

    draft = LayoutDraft(store)
    draft.active_layout.widgets.append(Widget("cpu-load"))
    draft.select("Gaming")

    assert draft.dirty
    assert store.path.read_text() == saved_layouts
    assert store.active_path.read_text() == saved_active

    draft.save()

    assert not draft.dirty
    assert store.active_layout().name == "Gaming"
    assert store.load()["Default"].widgets[0].metric == "cpu-load"


def test_layout_draft_reset_discards_edits_and_restores_selected_layout(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    layouts = {
        "Default": Layout("Default", [Widget("cpu-temperature")]),
        "Gaming": Layout("Gaming", [Widget("gpu-load")]),
    }
    store.save(layouts)
    store.save_active("Default", layouts)
    draft = LayoutDraft(store)
    draft.active_layout.widgets.append(Widget("cpu-load"))
    draft.select("Gaming")
    draft.active_layout.widgets.append(Widget("gpu-temperature"))

    draft.reset()

    assert not draft.dirty
    assert draft.active_layout.name == "Gaming"
    assert [widget.metric for widget in draft.layouts["Default"].widgets] == [
        "cpu-temperature"
    ]
    assert [widget.metric for widget in draft.active_layout.widgets] == ["gpu-load"]


def test_gui_settings_round_trip_and_ignore_unknown_values(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    saved = AppSettings("0000:85:00.0", 2.5, 120, "Gaming", "F")

    store.save(saved)

    assert store.load() == saved
    store.path.write_text(
        '{"gpu_bdf": "", "refresh_interval": 100, "sleep_timeout": -1,'
        ' "selected_layout": 5, "temperature_unit": "K", "future_setting": true}'
    )
    assert store.load() == AppSettings()


def test_gui_settings_fall_back_for_missing_or_malformed_json(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")

    assert store.load() == AppSettings()
    store.path.write_text("{not valid JSON")
    assert store.load() == AppSettings()


def test_settings_store_retains_the_last_selected_gif(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    first = tmp_path / "first.gif"
    second = tmp_path / "second.gif"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    assert store.save_gif(first) == tmp_path / "media/background.gif"
    assert store.saved_gif().read_bytes() == b"first"

    store.save_gif(second)

    assert store.saved_gif().read_bytes() == b"second"


def test_legacy_layout_media_resolves_to_retained_global_gif(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    source = tmp_path / "selected.gif"
    source.write_bytes(b"gif")
    retained = store.save_gif(source)
    settings = AppSettings(
        gif_mask_mode="openrgb",
        gif_mask_color="#ff3c00",
        gif_speed_source="gpu",
        gif_hue_shift=12,
        gif_brightness_percent=125,
    )

    selected, reload_settings = resolved_layout_media(
        Layout("Legacy"), store, settings
    )

    assert reload_settings
    assert selected == LayoutMedia(
        kind="gif",
        file=str(retained),
        gif_mask_mode="openrgb",
        gif_mask_color="#ff3c00",
        gif_speed_source="gpu",
        gif_speed_gpu_bdf=settings.gpu_bdf,
        gif_hue_shift=12,
        gif_brightness_percent=125,
    )


def test_layout_media_overrides_global_media_settings(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    selected = LayoutMedia(
        kind="image",
        file="/media/layout.png",
        gif_mask_mode="manual",
        gif_mask_color="#123456",
    )

    resolved, reload_settings = resolved_layout_media(
        Layout("Photo", media=selected), store, AppSettings()
    )

    assert resolved is selected
    assert not reload_settings


def test_gui_restores_saved_gif_and_cached_openrgb_color(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    source = tmp_path / "selected.gif"
    source.write_bytes(b"gif")
    saved = store.save_gif(source)
    cache = AnimationCache(store)
    cache.directory.mkdir(parents=True)
    cache.manifest_path.write_text(
        f'{{"version": {CACHE_VERSION}, "mask_color": "#ff3c00"}}\n'
    )

    assert restored_gif_sync_state(
        store, AppSettings(gif_mask_mode="openrgb")
    ) == (saved, "#ff3c00")


def test_restore_saved_animation_applies_the_saved_mask(monkeypatch, tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    source = tmp_path / "selected.gif"
    source.write_bytes(b"gif")
    saved = store.save_gif(source)
    calls = []
    controller = SimpleNamespace(
        show_animation=lambda selected, color: calls.append((selected, color))
    )
    settings = AppSettings(gif_mask_mode="openrgb")
    monkeypatch.setattr(
        "darkflash_space_station.cli.gif_mask_color",
        lambda selected_settings: "#ff3c00",
    )

    assert restore_saved_animation(controller, store, settings)
    assert calls == [(saved, "#ff3c00")]


def test_restore_saved_animation_skips_when_no_gif_is_saved(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    controller = SimpleNamespace(show_animation=lambda *_args: None)

    assert not restore_saved_animation(controller, store, AppSettings())


def test_layout_selection_can_avoid_marking_layout_edits_dirty(tmp_path) -> None:
    store = LayoutStore(tmp_path / "layouts.json")
    store.save({"Default": Layout("Default"), "Gaming": Layout("Gaming")})
    draft = LayoutDraft(store)

    draft.select("Gaming", mark_dirty=False)

    assert draft.active_name == "Gaming"
    assert not draft.dirty


def test_widget_selector_state_rebuilds_and_selects_after_structural_edits() -> None:
    widgets = [Widget("cpu-temperature")]
    widgets.append(Widget("gpu-load"))

    labels, selected = widget_selector_state(widgets, len(widgets) - 1)

    assert labels == ["Cpu Temperature", "Gpu Load"]
    assert selected == 1

    del widgets[selected]
    labels, selected = widget_selector_state(widgets, selected)

    assert labels == ["Cpu Temperature"]
    assert selected == 0
    assert widget_selector_state([], selected) == ([], -1)


def test_background_service_unit_runs_overlay_and_keeps_display_awake(tmp_path) -> None:
    unit = unit_text(tmp_path / "python")

    assert "telemetry --overlay --keep-awake" in unit
    assert "Restart=on-failure" in unit
